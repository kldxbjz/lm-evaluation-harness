#!/bin/bash
#SBATCH --gres=gpu:rtxa6000:1
#SBATCH --time=1-00:00:00
#SBATCH --mem=32gb
#SBATCH --output=out.log
lm_eval --model hf \
    --model_args pretrained=meta-llama/Meta-Llama-3-8B-Instruct \
    --tasks wmdp_fewshot_bio \
    --device cuda:0,1 \
    --batch_size 16 \
    --num_fewshot 0 \
    --suffix adv_tokens.csv