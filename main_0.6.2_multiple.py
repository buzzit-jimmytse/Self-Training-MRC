# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Run BERT on SQuAD."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import json
import logging
import os
import pickle
import random
import sys
from typing import Tuple

import numpy as np
import torch
from allennlp.training.metrics import CategoricalAccuracy
from pytorch_pretrained_bert.optimization import BertAdam, WarmupLinearSchedule
from pytorch_pretrained_bert.tokenization import BertTokenizer
from tensorboardX import SummaryWriter
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from bert_model import initialize_model, prepare_model_params
from data.data_instance import RawResultChoice, WeightResultChoice, ModelState, RawOutput
from reader import prepare_read_params
from general_util.utils import AverageMeter
from general_util.logger import setting_logger
from reader import initialize_reader

# def warmup_linear(x, warmup=0.002):
#     if x < warmup:
#         return x / warmup
#     return 1.0 - x
#

"""
This script has several usages:
    - Train a bert model from scratch.
    - Train a bert model from a pretrained model.
    - Train a bert model stage-wised.
        - Read labeling sentence file.
        - Predict sentence label.
"""


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-base-multilingual, bert-base-chinese.")
    parser.add_argument("--vocab_file", default='bert-base-uncased-vocab.txt', type=str, required=True)
    parser.add_argument("--model_file", default='bert-base-uncased.tar.gz', type=str, required=True)
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model checkpoints and predictions will be written.")
    parser.add_argument("--predict_dir", default=None, type=str, required=True,
                        help="The output directory where the predictions will be written.")

    # Other parameters
    parser.add_argument("--train_file", default=None, type=str, help="SQuAD json for training. E.g., train-v1.1.json")
    parser.add_argument("--predict_file", default=None, type=str,
                        help="SQuAD json for predictions. E.g., dev-v1.1.json or test-v1.1.json")
    parser.add_argument("--test_file", default=None, type=str)
    parser.add_argument("--max_seq_length", default=384, type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. Sequences "
                             "longer than this will be truncated, and sequences shorter than this will be padded.")
    parser.add_argument("--doc_stride", default=128, type=int,
                        help="When splitting up a long document into chunks, how much stride to take between chunks.")
    parser.add_argument("--max_query_length", default=64, type=int,
                        help="The maximum number of tokens for the question. Questions longer than this will "
                             "be truncated to this length.")
    parser.add_argument("--do_train", default=False, action='store_true', help="Whether to run training.")
    parser.add_argument("--do_predict", default=False, action='store_true', help="Whether to run eval on the dev set.")
    parser.add_argument("--train_batch_size", default=32, type=int, help="Total batch size for training.")
    parser.add_argument("--predict_batch_size", default=8, type=int, help="Total batch size for predictions.")
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs", default=2.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion", default=0.1, type=float,
                        help="Proportion of training to perform linear learning rate warmup for. E.g., 0.1 = 10% "
                             "of training.")
    parser.add_argument("--n_best_size", default=20, type=int,
                        help="The total number of n-best predictions to generate in the nbest_predictions.json "
                             "output file.")
    parser.add_argument("--max_answer_length", default=30, type=int,
                        help="The maximum length of an answer that can be generated. This is needed because the start "
                             "and end predictions are not conditioned on one another.")
    parser.add_argument("--verbose_logging", default=False, action='store_true',
                        help="If true, all of the warnings related to data processing will be printed. "
                             "A number of warnings are expected for a normal SQuAD evaluation.")
    parser.add_argument("--no_cuda",
                        default=False,
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--view_id',
                        type=int,
                        default=1,
                        help="view id of multi-view co-training(two-view)")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--do_lower_case",
                        default=True,
                        action='store_true',
                        help="Whether to lower case the input text. True for uncased models, False for cased models.")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--fp16',
                        default=False,
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")

    # Base setting
    parser.add_argument('--pretrain', type=str, default=None)
    parser.add_argument('--max_ctx', type=int, default=2)
    parser.add_argument('--task_name', type=str, default='coqa_yesno')
    parser.add_argument('--bert_name', type=str, default='baseline')
    parser.add_argument('--reader_name', type=str, default='coqa')
    parser.add_argument('--per_eval_step', type=int, default=10000000)
    # model parameters
    parser.add_argument('--evidence_lambda', type=float, default=0.8)
    parser.add_argument('--tf_layers', type=int, default=1)
    parser.add_argument('--tf_inter_size', type=int, default=3072)
    # Parameters for running labeling model
    parser.add_argument('--do_label', default=False, action='store_true')
    parser.add_argument('--sentence_id_file', type=str, default=None)
    parser.add_argument('--weight_threshold', type=float, default=0.0)
    parser.add_argument('--only_correct', default=False, action='store_true')
    parser.add_argument('--label_threshold', type=float, default=0.0)
    parser.add_argument('--use_gumbel', default=False, action='store_true')
    parser.add_argument('--sample_steps', type=int, default=10)
    parser.add_argument('--reward_func', type=int, default=0)
    parser.add_argument('--freeze_bert', default=False, action='store_true')
    parser.add_argument('--num_evidence', default=1, type=int)
    parser.add_argument('--power_length', default=1., type=float)

    args = parser.parse_args()

    logger = setting_logger(args.output_dir)
    logger.info('================== Program start. ========================')

    model_params = prepare_model_params(args)
    read_params = prepare_read_params(args)

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = int(args.train_batch_size / args.gradient_accumulation_steps)

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_predict and not args.do_label:
        raise ValueError("At least one of `do_train` or `do_predict` or `do_label` must be True.")

    if args.do_train:
        if not args.train_file:
            raise ValueError(
                "If `do_train` is True, then `train_file` must be specified.")
    if args.do_predict:
        if not args.predict_file:
            raise ValueError(
                "If `do_predict` is True, then `predict_file` must be specified.")

    if args.do_train:
        if os.path.exists(args.output_dir) and os.listdir(args.output_dir):
            raise ValueError("Output directory () already exists and is not empty.")
        os.makedirs(args.output_dir, exist_ok=True)

    if args.do_predict:
        os.makedirs(args.predict_dir, exist_ok=True)

    tokenizer = BertTokenizer.from_pretrained(args.vocab_file)

    data_reader = initialize_reader(args.reader_name)

    num_train_steps = None
    if args.do_train or args.do_label:
        train_examples = data_reader.read(input_file=args.train_file, **read_params)

        cached_train_features_file = args.train_file + '_{0}_{1}_{2}_{3}_{4}_{5}'.format(
            args.bert_model, str(args.max_seq_length), str(args.doc_stride), str(args.max_query_length), str(args.max_ctx),
            str(args.task_name))

        try:
            with open(cached_train_features_file, "rb") as reader:
                train_features = pickle.load(reader)
        except FileNotFoundError:
            train_features = data_reader.convert_examples_to_features(examples=train_examples, tokenizer=tokenizer,
                                                                      max_seq_length=args.max_seq_length)
            if args.local_rank == -1 or torch.distributed.get_rank() == 0:
                logger.info("  Saving train features into cached file %s", cached_train_features_file)
                with open(cached_train_features_file, "wb") as writer:
                    pickle.dump(train_features, writer)

        num_train_steps = int(len(train_features) / args.train_batch_size / args.gradient_accumulation_steps * args.num_train_epochs)

    # Prepare model
    if args.pretrain is not None:
        logger.info('Load pretrained model from {}'.format(args.pretrain))
        model_state_dict = torch.load(args.pretrain, map_location='cuda:0')
        model = initialize_model(args.bert_name, args.model_file, state_dict=model_state_dict, **model_params)
    else:
        model = initialize_model(args.bert_name, args.model_file, **model_params)

    if args.fp16:
        model.half()
    model.to(device)
    if args.local_rank != -1:
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        model = DDP(model)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())

    # Remove frozen parameters
    param_optimizer = [n for n in param_optimizer if n[1].requires_grad]

    # hack to remove pooler, which is not used
    # thus it produce None grad that break apex
    param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]

    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]

    t_total = num_train_steps if num_train_steps else -1
    if args.local_rank != -1:
        t_total = t_total // torch.distributed.get_world_size()
    if args.fp16:
        try:
            from apex.optimizers import FP16_Optimizer
            from apex.optimizers import FusedAdam
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        optimizer = FusedAdam(optimizer_grouped_parameters,
                              lr=args.learning_rate,
                              bias_correction=False,
                              max_grad_norm=1.0)
        if args.loss_scale == 0:
            optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
        else:
            optimizer = FP16_Optimizer(optimizer, static_loss_scale=args.loss_scale)
        warmup_linear = WarmupLinearSchedule(warmup=args.warmup_proportion, t_total=t_total)
        logger.info(f"warm up linear: warmup = {warmup_linear.warmup}, t_total = {warmup_linear.t_total}.")
    else:
        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=t_total)

    # Prepare data
    eval_examples = data_reader.read(input_file=args.predict_file, **read_params)
    eval_features = data_reader.convert_examples_to_features(examples=eval_examples, tokenizer=tokenizer,
                                                             max_seq_length=args.max_seq_length)

    eval_tensors = data_reader.data_to_tensors(eval_features)
    eval_data = TensorDataset(*eval_tensors)
    eval_sampler = SequentialSampler(eval_data)
    eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.predict_batch_size)

    if args.do_train:

        if args.do_label:
            logger.info('Training in State Wise.')
            sentence_id_file = args.sentence_id_file
            if sentence_id_file is not None:
                # for file in sentence_id_file_list:
                #     train_features = data_reader.generate_features_sentence_ids(train_features, file)
                train_features = data_reader.generate_features_sentence_ids(train_features, sentence_id_file)
            else:
                # train_features = data_reader.mask_all_sentence_ids(train_features)
                logger.info('No sentence id supervision is found.')
        else:
            logger.info('Training in traditional way.')

        logger.info("***** Running training *****")
        logger.info("  Num orig examples = %d", len(train_examples))
        logger.info("  Num split examples = %d", len(train_features))
        logger.info("  Batch size = %d", args.train_batch_size)
        train_loss = AverageMeter()
        summary_writer = SummaryWriter(log_dir=args.output_dir)
        global_step = 0
        eval_loss = AverageMeter()
        best_metric = 0.0
        eval_epoch = 0
        eval_acc = CategoricalAccuracy()

        train_tensors = data_reader.data_to_tensors(train_features)
        train_data = TensorDataset(*train_tensors)
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        for epoch in range(int(args.num_train_epochs)):
            logger.info(f'Running at Epoch {epoch}')
            # Train
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration", dynamic_ncols=True)):
                model.train()
                if n_gpu == 1:
                    batch = batch_to_device(batch, device)  # multi-gpu does scattering it-self
                inputs = data_reader.generate_inputs(batch, train_features, model_state=ModelState.Train)
                loss = model(**inputs)['loss']
                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                if args.fp16:
                    optimizer.backward(loss)
                else:
                    loss.backward()
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    # modify learning rate with special warm up BERT uses
                    # if args.fp16 is False, BertAdam is used and handles this automatically
                    if args.fp16:
                        lr_this_step = args.learning_rate * warmup_linear.get_lr(global_step)
                        for param_group in optimizer.param_groups:
                            param_group['lr'] = lr_this_step
                        summary_writer.add_scalar('lr', lr_this_step, global_step)
                    else:
                        summary_writer.add_scalar('lr', optimizer.get_lr()[0], global_step)

                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1

                    train_loss.update(loss.item(), 1)
                    summary_writer.add_scalar('train_loss', train_loss.avg, global_step)

                if (step + 1) % args.per_eval_step == 0 or step == len(train_dataloader) - 1:
                    # Evaluation
                    model.eval()
                    all_results = []
                    logger.info("Start evaluating")
                    for _, eval_batch in enumerate(tqdm(eval_dataloader, desc="Evaluating", dynamic_ncols=True)):
                        if n_gpu == 1:
                            eval_batch = batch_to_device(eval_batch, device)  # multi-gpu does scattering it-self
                        inputs = data_reader.generate_inputs(eval_batch, eval_features, model_state=ModelState.Evaluate)
                        with torch.no_grad():
                            output_dict = model(**inputs)
                            loss, batch_choice_logits = output_dict['loss'], output_dict['yesno_logits']
                            eval_acc(batch_choice_logits, inputs["answer_choice"])
                            eval_loss.update(loss.item(), 1)

                        example_indices = eval_batch[-1]
                        for i, example_index in enumerate(example_indices):
                            choice_logits = batch_choice_logits[i].detach().cpu().tolist()

                            eval_feature = eval_features[example_index.item()]
                            unique_id = int(eval_feature.unique_id)
                            # print(unique_id)
                            all_results.append(RawResultChoice(unique_id=unique_id, choice_logits=choice_logits))

                    eval_epoch_loss = eval_loss.avg
                    summary_writer.add_scalar('eval_loss', eval_epoch_loss, eval_epoch)
                    eval_loss.reset()

                    _, metric, save_metric = data_reader.write_predictions(eval_examples, eval_features, all_results, None)
                    logger.info(f"Eval epoch: {eval_epoch}")
                    for k, v in metric.items():
                        logger.info(f"{k}: {v}")
                        summary_writer.add_scalar(f'eval_{k}', v, eval_epoch)
                    print(f"Eval accuracy: {eval_acc.get_metric(reset=True)}")
                    torch.cuda.empty_cache()

                    if save_metric[1] > best_metric:
                        best_metric = save_metric[1]
                        model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
                        output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
                        torch.save(model_to_save.state_dict(), output_model_file)
                    logger.info('Eval Epoch: %d, %s: %f (Best %s: %f)' % (
                        eval_epoch, save_metric[0], save_metric[1], save_metric[0], best_metric))
                    eval_epoch += 1

        summary_writer.close()

    # Loading trained model.
    output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
    model_state_dict = torch.load(output_model_file, map_location='cuda:0')
    model = initialize_model(args.bert_name, args.model_file, state_dict=model_state_dict, **model_params)
    model.to(device)

    # Write Yes/No predictions
    if args.do_predict and (args.local_rank == -1 or torch.distributed.get_rank() == 0):

        test_examples = eval_examples
        test_features = eval_features

        test_tensors = data_reader.data_to_tensors(test_features)
        test_data = TensorDataset(*test_tensors)
        test_sampler = SequentialSampler(test_data)
        test_dataloader = DataLoader(test_data, sampler=test_sampler, batch_size=args.predict_batch_size)

        logger.info("***** Running predictions *****")
        logger.info("  Num orig examples = %d", len(test_examples))
        logger.info("  Num split examples = %d", len(test_features))
        logger.info("  Batch size = %d", args.predict_batch_size)

        model.eval()
        all_results = []
        logger.info("Start predicting yes/no on Dev set.")
        for batch in tqdm(test_dataloader, desc="Testing", dynamic_ncols=True):
            if n_gpu == 1:
                batch = batch_to_device(batch, device)  # multi-gpu does scattering it-self
            inputs = data_reader.generate_inputs(batch, test_features, model_state=ModelState.Test)
            with torch.no_grad():
                batch_choice_logits = model(**inputs)['yesno_logits']
            example_indices = batch[-1]
            for i, example_index in enumerate(example_indices):
                choice_logits = batch_choice_logits[i].detach().cpu().tolist()

                test_feature = test_features[example_index.item()]
                unique_id = int(test_feature.unique_id)

                all_results.append(RawResultChoice(unique_id=unique_id, choice_logits=choice_logits))

        output_prediction_file = os.path.join(args.predict_dir, 'predictions.json')
        _, metric, _ = data_reader.write_predictions(eval_examples, eval_features, all_results, output_prediction_file)
        for k, v in metric.items():
            logger.info(f'{k}: {v}')

    # Labeling sentence id.
    if args.do_label and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        def softmax(x):
            """Compute softmax values for each sets of scores in x."""
            e_x = np.exp(x - np.max(x))
            return e_x / e_x.sum()

        def beam_search(sentence_sim, beam_num=10):
            '''
            sentence_sim(numpy)
            '''
            max_length = args.num_evidence
            sentence_sim = np.pad(sentence_sim, (1, 0), 'constant', constant_values=(0,))
            sentences = [{'sim': sentence_sim, 'sentences': [], 'value': 0.}]
            while sentences[0]['sentences'] == [] or sentences[0]['sentences'][-1] != 0:
                new_sentences = []
                for sentence in sentences:
                    if sentence['sentences'] != [] and sentence['sentences'][-1] == 0:
                        new_sentences.append(sentence)
                        continue
                    scores = softmax(sentence['sim'])
                    for i in range(len(sentence['sim'])):
                        if i == 0 and sentence['sentences'] == []:
                            continue
                        if len(sentence['sentences']) > max_length:
                            continue
                        if len(sentence['sentences']) == max_length and i != 0:
                            continue
                        if i in sentence['sentences']:
                            continue
                        if max_length == 1 and i == 0:
                            value = sentence['value']
                        else:
                            value = sentence['value'] + np.log(scores[i])
                        # `i - 1` refers to original sentence id
                        new_sentence = {'sim': np.copy(sentence['sim']), 'sentences': sentence['sentences'] + [i], 'value': value}
                        new_sentence['sim'][i] = -1e15
                        new_sentences.append(new_sentence)
                sentences = sorted(new_sentences, key=lambda x: x['value'] / np.power(len(x['sentences']), args.power_length),
                                   reverse=True)[:beam_num]
            sentence = sentences[0]
            sentence['value'] = sentence['value'] / np.power(len(sentence['sentences']), args.power_length)
            return sentence

        def batch_beam_search(sentence_sim, sentence_mask, beam_num=10):
            sentence_sim = sentence_sim[:, 0].cpu().numpy() + 1e-15
            sentence_mask = sentence_mask.cpu().numpy()
            sentence_ids = [beam_search(_sim[:int(sum(_mask))], beam_num) for _sim, _mask in zip(sentence_sim, sentence_mask)]
            return sentence_ids

        test_examples = train_examples
        test_features = train_features

        test_tensors = data_reader.data_to_tensors(test_features)
        test_data = TensorDataset(*test_tensors)
        test_sampler = SequentialSampler(test_data)
        test_dataloader = DataLoader(test_data, sampler=test_sampler, batch_size=args.predict_batch_size)

        logger.info("***** Running labeling *****")
        logger.info("  Num orig examples = %d", len(test_examples))
        logger.info("  Num split examples = %d", len(test_features))
        logger.info("  Batch size = %d", args.predict_batch_size)

        model.eval()
        all_results = []
        logger.info("Start labeling.")
        for batch in tqdm(test_dataloader, desc="Testing"):
            if n_gpu == 1:
                batch = batch_to_device(batch, device)
            inputs = data_reader.generate_inputs(batch, test_features, model_state=ModelState.Test)
            with torch.no_grad():
                output_dict = model(**inputs)
                batch_choice_logits = output_dict['yesno_logits']
                batch_beam_result = batch_beam_search(output_dict['sentence_sim'], output_dict['sentence_mask'])
            example_indices = batch[-1]
            for i, example_index in enumerate(example_indices):
                choice_logits = batch_choice_logits[i].detach().cpu().tolist()
                # max_weight_index = batch_max_weight_indexes[i].detach().cpu().tolist()
                # max_weight = batch_max_weight[i].detach().cpu().tolist()
                evidence = batch_beam_result[i]

                test_feature = test_features[example_index.item()]
                unique_id = int(test_feature.unique_id)

                all_results.append(RawOutput(unique_id=unique_id, model_output={
                    "choice_logits": choice_logits,
                    "evidence": evidence
                }))
                # all_results.append(
                #     WeightResultChoice(unique_id=unique_id, choice_logits=choice_logits, max_weight_index=max_weight_index,
                #                        max_weight=max_weight))

        output_prediction_file = os.path.join(args.predict_dir, 'sentence_id_file.json')
        data_reader.predict_sentence_ids(test_examples, test_features, all_results,
                                         output_prediction_file, weight_threshold=args.weight_threshold,
                                         only_correct=args.only_correct, label_threshold=args.label_threshold)


def batch_to_device(batch: Tuple[torch.Tensor], device):
    # batch[-1] don't move to gpu.
    output = []
    for t in batch[:-1]:
        output.append(t.to(device))
    output.append(batch[-1])
    return output


if __name__ == "__main__":
    main()
