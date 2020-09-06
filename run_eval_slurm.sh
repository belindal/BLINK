#!/bin/sh
#SBATCH --output=log/%j.out
#SBATCH --error=log/%j.err
#SBATCH --partition=learnfair
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --signal=USR1
#SBATCH --mem=400000
#SBATCH --gres=gpu:8
#SBATCH --cpus-per-task=24
#SBATCH --time 3000
#SBATCH --constraint=volta32gb

# example usage
# bash run_eval_slurm.sh webqsp_filtered dev 'finetuned_webqsp_all_ents;all_mention_biencoder_all_avg_true_20_true_bert_large_qa_linear' joint 0.25 100 joint_0
# bash run_eval_slurm.sh webqsp_filtered dev 'finetuned_webqsp_all_ents;all_mention_biencoder_all_avg_true_20_true_bert_large_qa_linear' joint 0.25 100 joint_0
# bash run_eval_slurm.sh webqsp_filtered dev 'finetuned_webqsp_all_ents;all_mention_biencoder_all_avg_true_20_true_false_bert_large_qa_linear' joint 0.25 100 joint_0

test_questions=$1  # WebQSP_EL/AIDA-YAGO2/graphquestions_EL
subset=$2  # test/dev/train_only
model_full=$3  # finetuned_webqsp/finetuned_webqsp_all_ents/finetuned_graphqs/webqsp_none_biencoder/zeshel_none_biencoder/pretrain_all_avg_biencoder/
threshold=$4  # -4.5/-2.9/-inf for no pruning
top_k=$5  # 50
threshold_type=$6  # joint / top_entity_by_mention
eval_batch_size=$7  # 64
use_custom_entity_encoding=$8  # true (use ones generated by model)/false (use default ones)
gpu=$9

output_dir="/checkpoint/${USER}/entity_link/saved_preds"

export PYTHONPATH=.


if [ "${eval_batch_size}" = "" ]
then
    eval_batch_size="64"
fi
save_dir_batch=""
if [ "${eval_batch_size}" = "1" ]
then
    save_dir_batch="_realtime_test"
fi

if [[ -d "EL4QA_data/${test_questions}" ]]
then
    mentions_file="EL4QA_data/${test_questions}/tokenized/${subset}.jsonl"
elif [[ -d "all_inference_data/${test_questions}" ]]
then
    mentions_file="all_inference_data/${test_questions}/tokenized/${subset}.jsonl"
else
    echo "mentions files ${test_questions} not found under `EL4QA_data` or `all_inference_data`"
    exit
fi

threshold_args="--threshold=${threshold} --threshold_type ${threshold_type} "
if [[ ${threshold_type} = "top_entity_by_mention" ]]
then
    threshold_args="${threshold_args} --mention_threshold -0.6931"
fi

if [ "${gpu}" = "false" ]
then
    cuda_args=""
else
    cuda_args="--use_cuda"
fi

IFS=';' read -ra MODEL_PARSE <<< "${model_full}"
model_folder=${MODEL_PARSE[1]}
epoch=${MODEL_PARSE[2]}
if [[ $epoch != "" ]]
then
    model_folder=${MODEL_PARSE[1]}/epoch_${epoch}
fi
dir=${MODEL_PARSE[0]}
if [[ ${model} = "finetuned_webqsp" ]]
then
  biencoder_config=models/elq_large_params.txt
  biencoder_model=models/elq_webqsp_large.bin
elif [[ ${model} = "wiki_all_ents" ]]
then
  biencoder_config=models/elq_large_params.txt
  biencoder_model=models/elq_wiki_large.bin
else
  biencoder_config=experiments/${dir}/${MODEL_PARSE[1]}/training_params.txt
  biencoder_model=experiments/${dir}/${model_folder}/pytorch_model.bin
fi

if [[ "${test_questions}" = "nq" ]]
then
    max_context_length_args="--max_context_length 32"
elif [[ "${test_questions}" = "triviaqa" ]]
then
    max_context_length_args="--max_context_length 256"
else
    max_context_length_args="--max_context_length 32"
fi

if [[ "${use_custom_entity_encoding}" != "true" ]]
then
    entity_encoding=models/all_entities_large.t7
else
    entity_encoding=experiments/${dir}/${model_folder}/entity_encoding/all.t7
fi
echo ${mentions_file}

command="python blink/main_dense.py \
    --test_mentions ${mentions_file} \
    --test_entities models/entity.jsonl \
    --entity_catalogue models/entity.jsonl \
    --entity_encoding ${entity_encoding} \
    --biencoder_model ${biencoder_model} \
    --biencoder_config ${biencoder_config} \
    --save_preds_dir ${output_dir}/${test_questions}_${subset}_${model_full}_top${top_k}cands_thresh${threshold}${save_dir_batch} \
    ${threshold_args} --num_cand_mentions ${top_k} --num_cand_entities 10 \
    --eval_batch_size ${eval_batch_size} ${cuda_args} ${max_context_length_args} \
    --faiss_index hnsw --index_path models/faiss_hnsw_index.pkl"

echo "${command}"

${command}

