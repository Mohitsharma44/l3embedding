#!/usr/bin/env bash

sbatch --export=filename=pruned_audio_0.71586.h5 gen_embedding_pruned_melspec2_us8k.sbatch
sleep 1
sbatch --export=filename=pruned_audio_0.75373.h5 gen_embedding_pruned_melspec2_us8k.sbatch
sleep 1
sbatch --export=filename=pruned_audio_0.77128.h5 gen_embedding_pruned_melspec2_us8k.sbatch
