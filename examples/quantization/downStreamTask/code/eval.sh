temp=0.2
pred_num=10
output_path=$1

echo 'Output path: '$output_path
/root/model/miniconda3/envs/qat/bin/python process_humaneval.py --path ${output_path} --out_path ${output_path}.jsonl --add_prompt

/root/model/miniconda3/envs/qat/bin/evaluate_functional_correctness ${output_path}.jsonl