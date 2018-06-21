#!/bin/bash

if [ -z "$*" ]; then
    echo "Usage: $0 model1.ckpt model2.ckpt ..."
    exit 1
fi

set -o verbose
for MODEL in "$@"
do
    sbatch --time=0-5 submit.sh eval.py --model ${MODEL}
    sleep 1
done