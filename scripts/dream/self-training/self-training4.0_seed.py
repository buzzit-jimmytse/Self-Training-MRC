import argparse
import subprocess
import time
import logging
import os

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, required=True)
args = parser.parse_args()


def run_cmd(command: str):
    logger.info(command)
    subprocess.check_call(command, shell=True)


def wait_for_file(file: str):
    if not os.path.exists(file):
        logger.info(f'Could not find file {file}. Waiting...')
        minute_cnt = 0
        while not os.path.exists(file):
            print(f'The {minute_cnt}th minute...')
            time.sleep(60)
            minute_cnt += 1
        time.sleep(60)
        logger.info(f'Find file {file} after waiting for {minute_cnt} minutes')


# model
bert_base_model = "../BERT/bert-base-uncased.tar.gz"
bert_base_vocab = "../BERT/bert-base-uncased-vocab.txt"
bert_large_model = "../BERT/bert-large-uncased.tar.gz"
bert_large_vocab = "../BERT/bert-large-uncased-vocab.txt"

train_file = '/home/jiaofangkai/dream/data/train_in_race.json'
dev_file = '/home/jiaofangkai/dream/data/dev_in_race.json'
test_file = '/home/jiaofangkai/dream/data/test_in_race.json'

task_name = 'dream'
reader_name = 'multiple-race'
bert_name = 'topk-hie-race'

metric = 'accuracy'

k = 3000
label_threshold = 0.8
weight_threshold = 0.5
recurrent_times = 10
num_train_epochs = [5] * 10
num_evidence = 4
sentence_id_file = None

root_dir = f'experiments/dream/topk-evidence/self-training/v4.0_acc_top{k}_seed{args.seed}'
os.makedirs(root_dir, exist_ok=True)

f_handler = logging.FileHandler(os.path.join(root_dir, f'output.log'))
f_handler.setLevel(logging.INFO)
f_handler.setFormatter(logging.Formatter(fmt="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
                                         datefmt='%m/%d/%Y %H:%M:%S'))
logger.addHandler(f_handler)

logger.info('Self-training parameters:')
logger.info(f'k: {k}')
logger.info(f'label_threshold: {label_threshold}')
logger.info(f'weight_threshold: {weight_threshold}')
logger.info(f'recurrent_times: {recurrent_times}')

# initial_model_dir = 'experiments/dream/topk-evidence/self-training/v2.2_acc_top1500/recurrent0'

learning_rate = 2e-5
for i in range(recurrent_times):
    logger.info(f'Start running for the {i}th times.')
    output_dir = f'{root_dir}/recurrent{i}'

    if i == 0:
        # output_dir = initial_model_dir
        # predict_dir = f'{root_dir}/recurrent{i}'
        evidence_lambda = 0.0
    else:
        # output_dir = f'{root_dir}/recurrent{i}'
        # predict_dir = output_dir
        evidence_lambda = 0.8

    logger.info(f'num_evidence: {num_evidence}')

    cmd = f'python main_multi_choice_top_k_evidence.py --bert_model bert-base-uncased ' \
          f'--vocab_file {bert_base_vocab} --model_file {bert_base_model} --output_dir {output_dir} --predict_dir {output_dir} ' \
          f'--train_file {train_file} --predict_file {dev_file} --test_file {test_file} ' \
          f'--max_seq_length 512 --train_batch_size 24 --predict_batch_size 4 ' \
          f'--learning_rate {learning_rate} --num_train_epochs {num_train_epochs[i]} ' \
          f'--fp16 --gradient_accumulation_steps 6 --per_eval_step 3000 ' \
          f'--bert_name {bert_name} --task_name {task_name} --reader_name {reader_name} ' \
          f'--evidence_lambda {evidence_lambda} --num_choices 3 ' \
          f'--do_label --only_correct --label_threshold {label_threshold} --weight_threshold {weight_threshold} ' \
          f'--metric {metric} --num_evidence {num_evidence} --seed {args.seed} '

    # if i > 0:
    cmd += ' --do_train --do_predict '

    if i == 0:
        pass
    else:
        if i > 1:
            origin_sentence_id_file = f'{root_dir}/recurrent{i - 1}/sentence_id_file.json'

            merge_cmd = f'python general_util/race/merge_multiple.py ' \
                        f'--predict1 {root_dir}/recurrent0/sentence_id_file_recurrent{i - 1}.json.merge ' \
                        f'--predict2 {origin_sentence_id_file} ' \
                        f'--output_file {root_dir}/recurrent0/sentence_id_file_recurrent{i}.json.merge ' \
                        f'--label_threshold {label_threshold} --k {k} '
            run_cmd(merge_cmd)

        sentence_id_file = f'{root_dir}/recurrent0/sentence_id_file_recurrent{i}.json.merge'
        cmd += f'--sentence_id_file {sentence_id_file}'

    run_cmd(cmd)

    if i == 0:
        run_cmd(f'python general_util/race/top_k_multiple.py --predict {root_dir}/recurrent0/sentence_id_file.json '
                f'--k {k} --label_threshold {label_threshold}')

        run_cmd(f'mv {root_dir}/recurrent0/sentence_id_file.json-top{k}-{label_threshold} '
                f'{root_dir}/recurrent0/sentence_id_file_recurrent1.json.merge')
        logger.info('=' * 50)
