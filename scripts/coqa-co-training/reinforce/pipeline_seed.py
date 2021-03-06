import argparse
import logging
import os
import subprocess

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


# model
bert_base_model = "../bert-base-uncased.tar.gz"
bert_base_vocab = "../bert-base-uncased-vocab.txt"
# bert_large_model = "../BERT/bert-large-uncased.tar.gz"
# bert_large_vocab = "../BERT/bert-large-uncased-vocab.txt"

train_file = 'data/coqa/coqa-train-v1.0.json'
dev_file = 'data/coqa/coqa-dev-v1.0.json'

task_name = 'coqa'
reader_name = 'coqa'

bert_name = 'hie-hard'

num_train_epochs = 4.0
learning_rate = 3e-5
evidence_lambda = 0.0

output_dir = f'experiments/coqa/reinforce/gumbel-pre-train/v2.0_seed{args.seed}/'

cmd = f"python main_0.6.2.py --bert_model bert-base-uncased --vocab_file {bert_base_vocab} --model_file {bert_base_model} " \
      f"--train_file {train_file} --predict_file {dev_file} --max_seq_length 512 --max_query_length 385 " \
      f"--do_train --do_predict --train_batch_size 6 --predict_batch_size 6 --max_answer_length 15 " \
      f"--num_train_epochs {num_train_epochs} --learning_rate {learning_rate} --max_ctx 2 " \
      f"--bert_name {bert_name} --task_name {task_name} --reader_name {reader_name} " \
      f"--output_dir {output_dir} --predict_dir {output_dir} " \
      f"--evidence_lambda {evidence_lambda} --use_gumbel --freeze_bert --seed {args.seed} "

run_cmd(cmd)


bert_name = 'hie-reinforce'

num_train_epochs = 3.0
learning_rate = 2e-5
evidence_lambda = 0.0
reward_func = 1

output_dir = f'experiments/coqa/reinforce/reinforce-tune/v3.0_seed{args.seed}/'

cmd = f"python main_0.6.2.py --bert_model bert-base-uncased --vocab_file {bert_base_vocab} --model_file {bert_base_model} " \
    f"--train_file {train_file} --predict_file {dev_file} --max_seq_length 512 --max_query_length 385 " \
    f"--do_train --do_predict --train_batch_size 6 --predict_batch_size 6 --max_answer_length 15 " \
    f"--num_train_epochs {num_train_epochs} --learning_rate {learning_rate} --max_ctx 2 " \
    f"--bert_name {bert_name} --task_name {task_name} --reader_name {reader_name} " \
    f"--output_dir {output_dir} --predict_dir {output_dir} " \
    f"--evidence_lambda {evidence_lambda} --sample_steps 10 --reward_func {reward_func} " \
    f"--pretrain experiments/coqa/reinforce/gumbel-pre-train/v2.0_seed{args.seed}/pytorch_model.bin --seed {args.seed} "

run_cmd(cmd)
