#!/usr/bin/env python3

import argparse
import torch
import torch.nn as nn
import numpy as np
import os
import glob
import re
import sys
import json

from datetime import datetime
from torch.nn.utils.rnn import pack_padded_sequence
from torchvision import transforms

# (Needed to handle Vocabulary pickle)
from vocabulary import Vocabulary, get_vocab
from data_loader import get_loader, DatasetParams
from model import ModelParams, EncoderDecoder, SpatialAttentionEncoderDecoder, SoftAttentionEncoderDecoder
from infer import caption_ids_to_words

torch.manual_seed(42)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False

# Device configuration now in main()
device = None


def feats_to_str(feats):
    return '+'.join(feats.internal + [os.path.splitext(os.path.basename(f))[0]
                                      for f in feats.external])


# This is to print the float without exponential-notation, and without trailing zeros.
# Normal formatting, e.g.: '{:f}'.format(0.01) produces "0.010000"
def f2s(f):
    return '{:0.16f}'.format(f).rstrip('0')


def get_model_name(args, params):
    """Create model name"""

    if args.model_name is not None:
        model_name = args.model_name
    elif args.load_model:
        model_name = os.path.split(os.path.dirname(args.load_model))[-1]

    else:
        bn = args.model_basename

        feat_spec = feats_to_str(params.features)
        if params.has_persist_features():
            feat_spec += '-' + feats_to_str(params.persist_features)

        model_name = ('{}-{}-{}-{}-{}-{}-{}-{}-{}-{}-{}'.
                      format(bn, params.embed_size, params.hidden_size, params.num_layers,
                             params.batch_size, args.optimizer, f2s(params.learning_rate),
                             f2s(args.weight_decay), params.dropout, params.encoder_dropout,
                             feat_spec))
    return model_name


def save_model(args, params, encoder, decoder, optimizer, epoch, vocab):
    model_name = get_model_name(args, params)

    state = {
        'epoch': epoch + 1,
        # Attention models can in principle be trained without an encoder:
        'encoder': encoder.state_dict() if encoder is not None else None,
        'decoder': decoder.state_dict(),
        'optimizer': optimizer.state_dict(),
        'embed_size': params.embed_size,
        'hidden_size': params.hidden_size,
        'num_layers': params.num_layers,
        'batch_size': params.batch_size,
        'learning_rate': params.learning_rate,
        'dropout': params.dropout,
        'encoder_dropout': params.encoder_dropout,
        'features': params.features,
        'persist_features': params.persist_features,
        'attention': params.attention,
        'vocab': vocab
    }

    file_name = 'ep{}.model'.format(epoch + 1)

    model_path = os.path.join(args.model_path, model_name, file_name)
    os.makedirs(os.path.dirname(model_path), exist_ok=True)

    torch.save(state, model_path)
    print('Saved model as {}'.format(model_path))
    if args.verbose:
        print(params)


def stats_filename(args, params, postfix):
    model_name = get_model_name(args, params)
    model_dir = os.path.join(args.model_path, model_name)

    if postfix is None:
        json_name = 'train_stats.json'
    else:
        json_name = 'train_stats-{}.json'.format(postfix)

    return os.path.join(model_dir, json_name)


def init_stats(args, params, postfix=None):
    filename = stats_filename(args, params, postfix)
    if os.path.exists(filename):
        with open(filename, 'r') as fp:
            return json.load(fp)
    else:
        return dict()


def save_stats(args, params, all_stats, postfix=None):
    filename = stats_filename(args, params, postfix)
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    with open(filename, 'w') as outfile:
        json.dump(all_stats, outfile, indent=2)
    print('Wrote stats to {}.'.format(filename))


def find_matching_model(args, params):
    """Get a model file matching the parameters given with the latest trained epoch"""
    print('Attempting to resume from latest epoch matching supplied '
          'parameters...')
    # Get a matching filename without the epoch part
    model_name = get_model_name(args, params)

    # Files matching model:
    full_path_prefix = os.path.join(args.model_path, model_name)
    matching_files = glob.glob(full_path_prefix + '*.model')

    print("Looking for: {}".format(full_path_prefix + '*.model'))

    # get a file name with a largest matching epoch:
    file_regex = full_path_prefix + '/ep([0-9]*).model'
    r = re.compile(file_regex)
    last_epoch = 0

    for file in matching_files:
        m = r.match(file)
        if m:
            matched_epoch = int(m.group(1))
            if matched_epoch > last_epoch:
                last_epoch = matched_epoch

    model_file_path = None
    if last_epoch:
        model_file_name = 'ep{}.model'.format(last_epoch)
        model_file_path = os.path.join(full_path_prefix, model_file_name)
        print('Found matching model: {}'.format(args.load_model))
    else:
        print("Warning: Failed to intelligently resume...")

    return model_file_path


def get_teacher_prob(k, i, beta=1):
    """Inverse sigmoid sampling scheduler determines the probability
    with which teacher forcing is turned off, more info here:
    https://arxiv.org/pdf/1506.03099.pdf"""
    if k == 0:
        return 1.0

    i = i * beta
    p = k / (k + np.exp(i / k))

    return p


# Simple gradient clipper from tutorial, can be replaced with torch's own
# using it now to stay close to reference Attention implementation
def clip_gradients(optimizer, grad_clip):
    """
    Clips gradients computed during backpropagation to avoid explosion of gradients.
    :param optimizer: optimizer with the gradients to be clipped
    :param grad_clip: clip value
    """
    for group in optimizer.param_groups:
        for param in group['params']:
            if param.grad is not None:
                param.grad.data.clamp_(-grad_clip, grad_clip)


def do_validate(model, valid_loader, criterion, scorers, vocab, teacher_p, args, params,
                stats, epoch):
    begin = datetime.now()
    model.eval()

    gts = {}
    res = {}

    total_loss = 0
    num_batches = 0
    for i, (images, captions, lengths, image_ids, features) in enumerate(valid_loader):
        if len(scorers) > 0:
            for j in range(captions.shape[0]):
                jid = image_ids[j]
                if jid not in gts:
                    gts[jid] = []
                gts[jid].append(caption_ids_to_words(captions[j, :], vocab))

        # Set mini-batch dataset
        images = images.to(device)
        captions = captions.to(device)
        targets = pack_padded_sequence(captions, lengths,
                                       batch_first=True)[0]
        init_features = features[0].to(device)if len(features) > 0 and \
            features[0] is not None else None
        persist_features = features[1].to(device) if len(features) > 1 and \
            features[1] is not None else None

        with torch.no_grad():
            if args.attention is None:
                outputs = model(images, init_features, captions, lengths,
                                persist_features, teacher_p, args.teacher_forcing)
            else:
                outputs, alphas = model(images, init_features, captions,
                                        lengths, persist_features, teacher_p,
                                        args.teacher_forcing)

            if len(scorers) > 0:
                # Generate a caption from the image
                if params.attention is None:
                    sampled_ids_batch = model.sample(images, init_features,
                                                     persist_features,
                                                     max_seq_length=20)
                else:
                    sampled_ids_batch, _ = model.sample(images, init_features,
                                                        persist_features,
                                                        max_seq_length=20)

        loss = criterion(outputs, targets)

        if args.attention is not None and args.regularize_attn:
            loss += ((1. - alphas.sum(dim=1)) ** 2).mean()

        total_loss += loss.item()
        num_batches += 1

        if len(scorers) > 0:
            for j in range(sampled_ids_batch.shape[0]):
                jid = image_ids[j]
                res[jid] = [caption_ids_to_words(sampled_ids_batch[j], vocab)]

        # Used for testing:
        if i + 1 == args.num_batches:
            break

    model.train()

    end = datetime.now()

    for score_name, scorer in scorers.items():
        score = scorer.compute_score(gts, res)[0]
        print('Validation', score_name, score)
        stats['validation_' + score_name.lower()] = score

    val_loss = total_loss / num_batches
    stats['validation_loss'] = val_loss
    print('Epoch {} validation duration: {}, validation average loss: {:.4f}.'.format(
        epoch + 1, end - begin, val_loss))
    return val_loss


def main(args):
    global device
    device = torch.device('cuda' if torch.cuda.is_available() and
                          not args.cpu else 'cpu')

    if args.validate is None and args.lr_scheduler:
        print('ERROR: you need to enable validation in order to use the lr_scheduler')
        print('Hint: use something like --validate=coco:val2017')
        sys.exit(1)

    # Create model directory
    if not os.path.exists(args.model_path):
        os.makedirs(args.model_path)

    # Image preprocessing, normalization for the pretrained resnet
    transform = transforms.Compose([
        # transforms.Resize((256, 256)),
        transforms.RandomCrop(args.crop_size),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406),
                             (0.229, 0.224, 0.225))])

    scorers = {}
    if args.validation_scoring is not None:
        for s in args.validation_scoring.split(','):
            s = s.lower().strip()
            if s == 'cider':
                from eval.cider import Cider
                scorers['CIDEr'] = Cider()

    state = None

    # Get dataset parameters:
    dataset_configs = DatasetParams(args.dataset_config_file)

    if args.dataset is None and not args.validate_only:
        print('ERROR: No dataset selected!')
        print('Please supply a training dataset with the argument --dataset DATASET')
        print('The following datasets are configured in {}:'.format(args.dataset_config_file))
        for ds, _ in dataset_configs.config.items():
            if ds not in ('DEFAULT', 'generic'):
                print(' ', ds)
        sys.exit(1)

    if args.validate_only:
        if args.load_model is None:
            print('ERROR: for --validate_only you need to specify a model to evaluate using '
                  '--load_model MODEL')
            sys.exit(1)
    else:
        dataset_params = dataset_configs.get_params(args.dataset)

        for i in dataset_params:
            i.config_dict['no_tokenize'] = args.no_tokenize
            i.config_dict['show_tokens'] = args.show_tokens

    if args.validate is not None:
        validation_dataset_params = dataset_configs.get_params(args.validate)
        for i in validation_dataset_params:
            i.config_dict['no_tokenize'] = args.no_tokenize
            i.config_dict['show_tokens'] = args.show_tokens

    params = ModelParams.fromargs(args)
    start_epoch = 0

    # Intelligently resume from the newest trained epoch matching
    # supplied configuration:
    if args.resume:
        args.load_model = find_matching_model(args, params)

    if args.load_model:
        state = torch.load(args.load_model)
        external_features = params.features.external
        params = ModelParams(state)
        if len(external_features) > 0 and params.features.external != external_features:
            print('WARNING: external features changed: ',
                  params.features.external, external_features)
            print('Updating feature paths...')
            params.update_ext_features(external_features)
        start_epoch = state['epoch']
        print('Loading model {} at epoch {}.'.format(args.load_model,
                                                     start_epoch))
    print(params)

    # Load the vocabulary. For pre-trained models attempt to obtain
    # saved vocabulary from the model itself:
    if args.load_model and params.vocab is not None:
        print("Loading vocabulary from the model file:")
        vocab = params.vocab
    else:
        if args.vocab is None:
            print("Error: You must specify the vocabulary to be used for training using "
                  "--vocab flag.\nTry --vocab AUTO if you want the vocabulary to be "
                  "either generated from the training dataset or loaded from cache.")
            sys.exit(1)
        print("Loading / generating vocabulary:")
        vocab = get_vocab(args, dataset_params)

    print('Size of the vocabulary is {}'.format(len(vocab)))

    if False:
        vocl = vocab.get_list()
        with open('vocab-dump.txt', 'w') as vocf:
            print('\n'.join(vocl), file=vocf)

    if args.force_epoch:
        start_epoch = args.force_epoch - 1

    ext_feature_sets = [params.features.external, params.persist_features.external]

    # Build data loader
    if not args.validate_only:
        print('Loading dataset: {} with {} workers'.format(args.dataset, args.num_workers))
        data_loader, ef_dims = get_loader(dataset_params, vocab, transform, args.batch_size,
                                          shuffle=True, num_workers=args.num_workers,
                                          ext_feature_sets=ext_feature_sets,
                                          skip_images=not params.has_internal_features(),
                                          verbose=args.verbose)

    if args.validate is not None:
        valid_loader, ef_dims = get_loader(validation_dataset_params, vocab, transform,
                                           args.batch_size, shuffle=True,
                                           num_workers=args.num_workers,
                                           ext_feature_sets=ext_feature_sets,
                                           skip_images=not params.has_internal_features(),
                                           verbose=args.verbose)

    # Build the models
    if args.attention is None:
        _Model = EncoderDecoder
    elif args.attention == 'spatial':
        _Model = SpatialAttentionEncoderDecoder
    elif args.attention == 'soft':
        _Model = SoftAttentionEncoderDecoder
    else:
        print("Error: Invalid attention model specified")
        sys.exit(1)

    model = _Model(params, device, len(vocab), state, ef_dims)

    opt_params = model.get_opt_params()

    # Loss and optimizer
    criterion = nn.CrossEntropyLoss()

    default_lr = 0.001
    if args.optimizer == 'adam':
        optimizer = torch.optim.Adam(opt_params, lr=default_lr,
                                     weight_decay=args.weight_decay)
    elif args.optimizer == 'rmsprop':
        optimizer = torch.optim.RMSprop(opt_params, lr=default_lr,
                                        weight_decay=args.weight_decay)
    else:
        print('ERROR: unknown optimizer:', args.optimizer)
        sys.exit(1)

    if state:
        optimizer.load_state_dict(state['optimizer'])

    if args.learning_rate:  # override lr if set explicitly in arguments
        for param_group in optimizer.param_groups:
            param_group['lr'] = args.learning_rate
        params.learning_rate = args.learning_rate
    else:
        params.learning_rate = default_lr

    if args.validate is not None and args.lr_scheduler:
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', verbose=True,
                                                               patience=2)

    stats_postfix = None
    if args.validate_only:
        stats_postfix = args.validate

    # Train the models
    if args.load_model:
        all_stats = init_stats(args, params, postfix=stats_postfix)
    else:
        all_stats = {}

    if not args.validate_only:
        total_step = len(data_loader)
        print('Start training with num_epochs={:d} num_batches={:d} ...'.
              format(args.num_epochs, args.num_batches))

    if args.teacher_forcing != 'always':
        print('\t k: {}'.format(args.teacher_forcing_k))
        print('\t beta: {}'.format(args.teacher_forcing_beta))
    print('Optimizer:', optimizer)

    if args.validate_only:
        stats = {}
        teacher_p = 1.0
        if args.teacher_forcing != 'always':
            print('WARNING: teacher_forcing!=always, not yet implemented for '
                  '--validate_only mode')

        epoch = start_epoch-1
        val_loss = do_validate(model, valid_loader, criterion, scorers, vocab, teacher_p, args,
                               params, stats, epoch)
        all_stats[epoch+1] = stats
        save_stats(args, params, all_stats, postfix=stats_postfix)
    else:
        for epoch in range(start_epoch, args.num_epochs):
            stats = {}
            begin = datetime.now()
            total_loss = 0
            num_batches = 0
            vocab_counts = { 'cnt':0, 'max':0, 'min':9999,
                             'sum':0, 'unk_cnt':0, 'unk_sum':0 }
            for i, (images, captions, lengths, _, features) in enumerate(data_loader):
                #print(captions.shape)
                #print(captions)
                if epoch==0:
                    unk = vocab('<unk>')
                    for j in range(captions.shape[0]):
                        xl = captions[j,:]
                        xw = xl>unk
                        xu = xl==unk
                        xwi = sum(xw).item()
                        xui = sum(xu).item()
                        vocab_counts['cnt']     += 1;
                        vocab_counts['sum']     += xwi;
                        vocab_counts['max']      = max(vocab_counts['max'], xwi)
                        vocab_counts['min']      = min(vocab_counts['min'], xwi)
                        vocab_counts['unk_cnt'] += xui>0
                        vocab_counts['unk_sum'] += xui

                # Set mini-batch dataset
                images = images.to(device)
                captions = captions.to(device)
                targets = pack_padded_sequence(captions, lengths,
                                               batch_first=True)[0]
                #print(features[0].shape)
                #print(features[0])
                #exit(1)
                init_features = features[0].to(device) if len(features) > 0 and \
                    features[0] is not None else None
                persist_features = features[1].to(device) if len(features) > 1 and \
                    features[1] is not None else None

                # Forward, backward and optimize
                # Calculate the probability whether to use teacher forcing or not:

                # Iterate over batches:
                iteration = (epoch - start_epoch) * len(data_loader) + i

                teacher_p = get_teacher_prob(args.teacher_forcing_k, iteration,
                                             args.teacher_forcing_beta)

                if args.attention is None:
                    outputs = model(images, init_features, captions, lengths, persist_features,
                                    teacher_p, args.teacher_forcing)
                else:
                    outputs, alphas = model(images, init_features, captions, lengths,
                                            persist_features, teacher_p, args.teacher_forcing)

                loss = criterion(outputs, targets)

                # Attention regularizer
                if args.attention is not None and args.regularize_attn:
                    loss += ((1. - alphas.sum(dim=1)) ** 2).mean()

                model.zero_grad()
                loss.backward()

                # Clip gradients if desired:
                if args.grad_clip is not None:
                    # grad_norms = [x.grad.data.norm(2) for x in opt_params]
                    # batch_max_grad = np.max(grad_norms)
                    # if batch_max_grad > 10.0:
                    #     print('WARNING: gradient norms larger than 10.0')

                    # torch.nn.utils.clip_grad_norm_(decoder.parameters(), 0.1)
                    # torch.nn.utils.clip_grad_norm_(encoder.parameters(), 0.1)
                    clip_gradients(optimizer, args.grad_clip)

                # Update weights:
                optimizer.step()

                total_loss += loss.item()
                num_batches += 1

                # Print log info
                if (i + 1) % args.log_step == 0:
                    print('Epoch [{}/{}], Step [{}/{}], Loss: {:.4f}, '
                          'Perplexity: {:5.4f}'.
                          format(epoch + 1, args.num_epochs, i + 1, total_step, loss.item(),
                                 np.exp(loss.item())))
                    sys.stdout.flush()

                if i + 1 == args.num_batches:
                    break

            end = datetime.now()

            stats['training_loss'] = total_loss / num_batches
            print('Epoch {} duration: {}, average loss: {:.4f}.'.format(
                epoch + 1, end - begin, stats['training_loss']))
            save_model(args, params, model.encoder, model.decoder, optimizer, epoch, vocab)

            if epoch == 0:
                vocab_counts['avg'] = vocab_counts['sum']/vocab_counts['cnt']
                vocab_counts['unk_cnt_per'] = 100*vocab_counts['unk_cnt']/vocab_counts['cnt']
                vocab_counts['unk_sum_per'] = 100*vocab_counts['unk_sum']/vocab_counts['sum']
                # print(vocab_counts)
                print(('Training data contains {sum} words in {cnt} captions (avg. {avg:.1f} w/c)'+
                       ' with {unk_sum} <unk>s ({unk_sum_per:.1f}%)'+
                       ' in {unk_cnt} ({unk_cnt_per:.1f}%) captions').format(**vocab_counts))

            if args.validate is not None and (epoch + 1) % args.validation_step == 0:
                val_loss = do_validate(model, valid_loader, criterion, scorers, vocab,
                                       teacher_p, args, params, stats, epoch)

                if args.lr_scheduler:
                    scheduler.step(val_loss)

            all_stats[epoch + 1] = stats
            save_stats(args, params, all_stats)


if __name__ == '__main__':
    # default_dataset = 'coco:train2014'
    default_features = 'resnet152'

    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=str, help='which dataset to use')
    parser.add_argument('--dataset_config_file', type=str,
                        default='datasets/datasets.conf',
                        help='location of dataset configuration file')
    parser.add_argument('--load_model', type=str, nargs='+',
                        help='existing model, for continuing training')
    parser.add_argument('--model_name', type=str)
    parser.add_argument('--model_basename', type=str, default='model',
                        help='base name for model snapshot filenames')
    parser.add_argument('--model_path', type=str, default='models/',
                        help='path for saving trained models')
    parser.add_argument('--crop_size', type=int, default=224,
                        help='size for randomly cropping images')
    parser.add_argument('--tmp_dir_prefix', type=str,
                        default='image_captioning',
                        help='where in /tmp folder to store project data')
    parser.add_argument('--log_step', type=int, default=10,
                        help='step size for printing log info')
    parser.add_argument('--resume', action="store_true",
                        help="Resume from largest epoch checkpoint matching \
                        current parameters")
    parser.add_argument('--verbose', action="store_true", help="Increase verbosity")
    parser.add_argument('--profiler', action="store_true", help="Run in profiler")
    parser.add_argument('--cpu', action="store_true",
                        help="Use CPU even when GPU is available")

    # Vocabulary configuration:
    parser.add_argument('--vocab', type=str, default=None,
                        help='Vocabulary directive or path. '
                        'Directives are all-caps, no special characters. '
                        'Vocabulary file formats supported - *.{pkl,txt}.\n'
                        'AUTO: If vocabulary corresponding to current training set '
                        'combination exits in the vocab/ folder load it. '
                        'If not, generate a new vocabulary file\n'
                        'REGEN: Regenerate a new vocabulary file and place it in '
                        'vocab/ folder\n'
                        'path/to/vocab.\{pkl,txt\}: path to Pickled or plain-text '
                        'vocabulary file\n')
    parser.add_argument('--vocab_root', type=str, default='vocab_cache',
                        help='Cached vocabulary files folder')
    parser.add_argument('--no_tokenize', action='store_true')
    parser.add_argument('--show_tokens', action='store_true')
    parser.add_argument('--vocab_threshold', type=int, default=4,
                        help='minimum word count threshold')
    parser.add_argument('--show_vocab_stats', action="store_true",
                        help='show generated vocabulary word counts')

    # Model parameters:
    parser.add_argument('--features', type=str, default=default_features,
                        help='features to use as the initial input for the '
                        'caption generator, given as comma separated list, '
                        'multiple features are concatenated, '
                        'features ending with .npy are assumed to be '
                        'precalculated features read from the named npy file, '
                        'example: "resnet152,foo.npy"')
    parser.add_argument('--persist_features', type=str,
                        help='features accessible in all caption generation '
                        'steps, given as comma separated list')
    parser.add_argument('--attention', type=str,
                        help='type of attention mechanism to use '
                        ' currently supported types: None, spatial')
    parser.add_argument('--regularize_attn', action='store_true',
                        help='when training attention models, toggle one attention '
                             'reguralizer for the loss')
    parser.add_argument('--embed_size', type=int, default=256,
                        help='dimension of word embedding vectors')
    parser.add_argument('--hidden_size', type=int, default=512,
                        help='dimension of lstm hidden states')
    parser.add_argument('--num_layers', type=int, default=1,
                        help='number of layers in lstm')
    parser.add_argument('--dropout', type=float, default=0.0,
                        help='dropout for the LSTM')
    parser.add_argument('--encoder_dropout', type=float, default=0.0,
                        help='dropout for the encoder FC layer')

    # Training parameters
    parser.add_argument('--force_epoch', type=int, default=0,
                        help='Force start epoch (for broken model files...)')
    parser.add_argument('--num_epochs', type=int, default=5)
    parser.add_argument('--num_batches', type=int, default=0)
    parser.add_argument('--batch_size', type=int, default=128)
    parser.add_argument('--num_workers', type=int, default=2)
    parser.add_argument('--learning_rate', type=float)
    parser.add_argument('--grad_clip', type=float,
                        help='Value at which to clip weight gradients. Disabled by default')
    parser.add_argument('--validate', type=str,
                        help='Dataset to validate against after each epoch')
    parser.add_argument('--validation_step', type=int, default=1,
                        help='After how many epochs to perform validation, default=1')
    parser.add_argument('--validation_scoring', type=str)
    parser.add_argument('--validate_only', action='store_true',
                        help='Just perform validation with given model, no training')
    parser.add_argument('--optimizer', type=str, default="rmsprop")
    parser.add_argument('--weight_decay', type=float, default=1e-6)
    parser.add_argument('--lr_scheduler', action='store_true')

    # For teacher forcing schedule see - https://arxiv.org/pdf/1506.03099.pdf
    parser.add_argument('--teacher_forcing', type=str, default='always',
                        help='Type of teacher forcing to use for training the Decoder RNN: \n'
                             'always: always use groundruth as LSTM input when training'
                             'sampled: follow a sampling schedule detemined by the value '
                             'of teacher_forcing_parameter\n'
                             'additive: use the sampling schedule formula to determine weight '
                             'ratio between the teacher and model inputs\n'
                             'additive_sampled: combines two of the above modes')
    parser.add_argument('--teacher_forcing_k', type=float, default=6500,
                        help='value of the sampling schedule parameter k. '
                        'Good values can be found in a range between 400 - 20000'
                        'small values = start using model output quickly, large values -'
                        ' wait for a while before start using model output')
    parser.add_argument('--teacher_forcing_beta', type=float, default=1,
                        help='sample scheduling parameter that determins the slope of '
                        'the middle segment of the sigmoid')

    args = parser.parse_args()

    begin = datetime.now()
    print('Started training at {}.'.format(begin))

    models = args.load_model
    if models is None:
        models = [None]
    for load_model in models:
        args.load_model = load_model
        if args.profiler:
            import cProfile
            cProfile.run('main(args=args)', filename='train.prof')
        else:
            main(args=args)

    end = datetime.now()
    print('Training ended at {}. Total training time: {}.'.format(end, end - begin))
