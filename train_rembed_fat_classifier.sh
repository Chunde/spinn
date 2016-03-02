#!/bin/bash

### Generic job script for all experiments.

# Usage example:
# export REMBED_FLAGS="--learning_rate 0.01 --batch_size 256"; export DEVICE=gpu2; export DEVICE=gpu0; qsub -v REMBED_FLAGS,DEVICE train_rembed_classifier.sh -l host=jagupard10

# Change to the submission directory.
cd $PBS_O_WORKDIR
echo Lauching from working directory: $PBS_O_WORKDIR
echo Flags: $REMBED_FLAGS
echo Device: $DEVICE

# Log what we're running and where.
echo $PBS_JOBID - `hostname` - $DEVICE - at `git log --pretty=format:'%h' -n 1` - $REMBED_FLAGS >> ~/rembed_machine_assignments.txt

# Use Jon's Theano install.
source /u/nlp/packages/anaconda/bin/activate conda-common
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
export PYTHONPATH=/scr/jgauthie/tmp/theano-nshrdlu:$PYTHONPATH

THEANO_FLAGS=allow_gc=False,cuda.root=/usr/bin/cuda,warn_float64=warn,device=$DEVICE,floatX=float32 python -m rembed.models.fat_classifier $REMBED_FLAGS
