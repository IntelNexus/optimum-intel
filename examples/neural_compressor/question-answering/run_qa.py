#!/usr/bin/env python
# coding=utf-8
#  Copyright 2021 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Fine-tuning the library models for question answering.
"""
# You can also adapt this script on your own question answering task. Pointers for this are left as comments.

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import datasets
import numpy as np
import torch
import transformers
from datasets import load_dataset, load_metric
from torch.utils.data.dataloader import DataLoader
from tqdm.auto import tqdm
from transformers import (
    AutoConfig,
    AutoModelForQuestionAnswering,
    AutoTokenizer,
    DataCollatorWithPadding,
    EvalPrediction,
    HfArgumentParser,
    PreTrainedTokenizerFast,
    TrainingArguments,
    default_data_collator,
    set_seed,
)
from transformers.trainer_utils import get_last_checkpoint
from transformers.utils import check_min_version
from transformers.utils.versions import require_version

from accelerate import Accelerator
from optimum.intel.neural_compressor import (
    IncDistillationConfig,
    IncDistiller,
    IncOptimizer,
    IncPruner,
    IncPruningConfig,
    IncQuantizationConfig,
    IncQuantizationMode,
    IncQuantizer,
)
from optimum.intel.neural_compressor.quantization import IncQuantizedModelForQuestionAnswering
from trainer_qa import QuestionAnsweringIncTrainer
from utils_qa import postprocess_qa_predictions


os.environ["CUDA_VISIBLE_DEVICES"] = ""

# Will error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.17.0")

require_version("datasets>=1.8.0", "To fix: pip install -r examples/question-answering/requirements.txt")

logger = logging.getLogger(__name__)


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    config_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained config name or path if not the same as model_name"}
    )
    tokenizer_name: Optional[str] = field(
        default=None, metadata={"help": "Pretrained tokenizer name or path if not the same as model_name"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Path to directory to store the pretrained models downloaded from huggingface.co"},
    )
    model_revision: str = field(
        default="main",
        metadata={"help": "The specific model version to use (can be a branch name, tag name or commit id)."},
    )
    use_auth_token: bool = field(
        default=False,
        metadata={
            "help": "Will use the token generated when running `transformers-cli login` (necessary to use this script "
            "with private models)."
        },
    )


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.
    """

    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    train_file: Optional[str] = field(default=None, metadata={"help": "The input training data file (a text file)."})
    validation_file: Optional[str] = field(
        default=None,
        metadata={"help": "An optional input evaluation data file to evaluate the perplexity on (a text file)."},
    )
    test_file: Optional[str] = field(
        default=None,
        metadata={"help": "An optional input test data file to evaluate the perplexity on (a text file)."},
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached training and evaluation sets"}
    )
    preprocessing_num_workers: Optional[int] = field(
        default=None,
        metadata={"help": "The number of processes to use for the preprocessing."},
    )
    max_seq_length: int = field(
        default=384,
        metadata={
            "help": "The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."
        },
    )
    pad_to_max_length: bool = field(
        default=True,
        metadata={
            "help": "Whether to pad all samples to `max_seq_length`. "
            "If False, will pad the samples dynamically when batching to the maximum length in the batch (which can "
            "be faster on GPU but will be slower on TPU)."
        },
    )
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    max_predict_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of prediction examples to this "
            "value if set."
        },
    )
    version_2_with_negative: bool = field(
        default=False, metadata={"help": "If true, some of the examples do not have an answer."}
    )
    null_score_diff_threshold: float = field(
        default=0.0,
        metadata={
            "help": "The threshold used to select the null answer: if the best answer has a score that is less than "
            "the score of the null answer minus this threshold, the null answer is selected for this example. "
            "Only useful when `version_2_with_negative=True`."
        },
    )
    doc_stride: int = field(
        default=128,
        metadata={"help": "When splitting up a long document into chunks, how much stride to take between chunks."},
    )
    n_best_size: int = field(
        default=20,
        metadata={"help": "The total number of n-best predictions to generate when looking for an answer."},
    )
    max_answer_length: int = field(
        default=30,
        metadata={
            "help": "The maximum length of an answer that can be generated. This is needed because the start "
            "and end predictions are not conditioned on one another."
        },
    )

    def __post_init__(self):
        if (
            self.dataset_name is None
            and self.train_file is None
            and self.validation_file is None
            and self.test_file is None
        ):
            raise ValueError("Need either a dataset name or a training/validation file/test_file.")
        else:
            if self.train_file is not None:
                extension = self.train_file.split(".")[-1]
                assert extension in ["csv", "json"], "`train_file` should be a csv or a json file."
            if self.validation_file is not None:
                extension = self.validation_file.split(".")[-1]
                assert extension in ["csv", "json"], "`validation_file` should be a csv or a json file."
            if self.test_file is not None:
                extension = self.test_file.split(".")[-1]
                assert extension in ["csv", "json"], "`test_file` should be a csv or a json file."


@dataclass
class OptimizationArguments:
    """
    Arguments pertaining to what type of optimization we are going to apply on the model.
    """

    apply_quantization: bool = field(
        default=False,
        metadata={"help": "Whether or not to apply quantization."},
    )
    quantization_approach: Optional[str] = field(
        default=None,
        metadata={"help": "Quantization approach. Supported approach are static, dynamic and aware_training."},
    )
    apply_pruning: bool = field(
        default=False,
        metadata={"help": "Whether or not to apply pruning."},
    )
    target_sparsity: Optional[float] = field(
        default=None,
        metadata={"help": "Targeted sparsity when pruning the model."},
    )
    apply_distillation: bool = field(
        default=False,
        metadata={"help": "Whether or not to apply distillation."},
    )
    generate_teacher_logits: bool = field(
        default=False,
        metadata={
            "help": "Whether to compute and save the teacher's outputs to accelerate training when applying distillation."
        },
    )
    teacher_model_name_or_path: str = field(
        default=False, metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    quantization_config: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to the directory containing the YAML configuration file used to control the quantization and "
            "tuning behavior."
        },
    )
    pruning_config: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to the directory containing the YAML configuration file used to control the pruning behavior."
        },
    )
    distillation_config: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to the directory containing the YAML configuration file used to control the distillation"
            "behavior."
        },
    )
    metric: str = field(
        default="eval_f1",
        metadata={"help": "Metric used for the tuning strategy."},
    )
    tolerance_criterion: Optional[float] = field(
        default=None,
        metadata={"help": "Performance tolerance when optimizing the model."},
    )
    verify_loading: bool = field(
        default=False,
        metadata={"help": "Whether or not to verify the loading of the quantized model."},
    )


def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments, OptimizationArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args, optim_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1])
        )
    else:
        model_args, data_args, training_args, optim_args = parser.parse_args_into_dataclasses()

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}"
        + f"distributed training: {bool(training_args.local_rank != -1)}, 16-bits training: {training_args.fp16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")

    # Detecting last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len(os.listdir(training_args.output_dir)) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )

    if optim_args.apply_quantization and optim_args.quantization_approach == "static":
        training_args.do_train = True

    # Set seed before initializing model.
    set_seed(training_args.seed)

    # Get the datasets: you can either provide your own CSV/JSON/TXT training and evaluation files (see below)
    # or just provide the name of one of the public datasets available on the hub at https://huggingface.co/datasets/
    # (the dataset will be downloaded automatically from the datasets Hub).
    #
    # For CSV/JSON files, this script will use the column called 'text' or the first column if no column called
    # 'text' is found. You can easily tweak this behavior (see below).
    #
    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.
    if data_args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        raw_datasets = load_dataset(
            data_args.dataset_name, data_args.dataset_config_name, cache_dir=model_args.cache_dir
        )
    else:
        data_files = {}
        if data_args.train_file is not None:
            data_files["train"] = data_args.train_file
            extension = data_args.train_file.split(".")[-1]
        if data_args.validation_file is not None:
            data_files["validation"] = data_args.validation_file
            extension = data_args.validation_file.split(".")[-1]
        if data_args.test_file is not None:
            data_files["test"] = data_args.test_file
            extension = data_args.test_file.split(".")[-1]
        raw_datasets = load_dataset(extension, data_files=data_files, field="data", cache_dir=model_args.cache_dir)
    # See more about loading any type of standard or custom dataset (from files, python dict, pandas DataFrame, etc) at
    # https://huggingface.co/docs/datasets/loading_datasets.html.

    # Load pretrained model and tokenizer
    #
    # Distributed training:
    # The .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.
    config = AutoConfig.from_pretrained(
        model_args.config_name if model_args.config_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.tokenizer_name if model_args.tokenizer_name else model_args.model_name_or_path,
        cache_dir=model_args.cache_dir,
        use_fast=True,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )
    model = AutoModelForQuestionAnswering.from_pretrained(
        model_args.model_name_or_path,
        from_tf=bool(".ckpt" in model_args.model_name_or_path),
        config=config,
        cache_dir=model_args.cache_dir,
        revision=model_args.model_revision,
        use_auth_token=True if model_args.use_auth_token else None,
    )

    # Tokenizer check: this script requires a fast tokenizer.
    if not isinstance(tokenizer, PreTrainedTokenizerFast):
        raise ValueError(
            "This example script only works for models that have a fast tokenizer. Checkout the big table of models "
            "at https://huggingface.co/transformers/index.html#supported-frameworks to find the model types that meet this "
            "requirement"
        )

    # Preprocessing the datasets.
    # Preprocessing is slighlty different for training and evaluation.
    if training_args.do_train:
        column_names = raw_datasets["train"].column_names
    elif training_args.do_eval:
        column_names = raw_datasets["validation"].column_names
    else:
        raise ValueError("--do_train or --do_eval are both set to False")
    question_column_name = "question" if "question" in column_names else column_names[0]
    context_column_name = "context" if "context" in column_names else column_names[1]
    answer_column_name = "answers" if "answers" in column_names else column_names[2]

    # Padding side determines if we do (question|context) or (context|question).
    pad_on_right = tokenizer.padding_side == "right"

    if data_args.max_seq_length > tokenizer.model_max_length:
        logger.warning(
            f"The max_seq_length passed ({data_args.max_seq_length}) is larger than the maximum length for the"
            f"model ({tokenizer.model_max_length}). Using max_seq_length={tokenizer.model_max_length}."
        )
    max_seq_length = min(data_args.max_seq_length, tokenizer.model_max_length)

    # Data collator
    # We have already padded to max length if the corresponding flag is True, otherwise we need to pad in the data
    # collator.
    data_collator = (
        default_data_collator
        if data_args.pad_to_max_length
        else DataCollatorWithPadding(tokenizer, pad_to_multiple_of=8 if training_args.fp16 else None)
    )

    class QAModel(torch.nn.Module):
        def __init__(self, model):
            super(QAModel, self).__init__()
            self.model = model

        def forward(self, *args, **kwargs):
            outputs = self.model(*args, **kwargs)
            outputs_reshaped = torch.vstack(
                [torch.vstack([sx, ex]) for sx, ex in zip(outputs["start_logits"], outputs["end_logits"])]
            )
            return outputs_reshaped

    # Training preprocessing
    def prepare_train_features(examples):
        # Some of the questions have lots of whitespace on the left, which is not useful and will make the
        # truncation of the context fail (the tokenized question will take a lots of space). So we remove that
        # left whitespace
        examples[question_column_name] = [q.lstrip() for q in examples[question_column_name]]

        # Tokenize our examples with truncation and maybe padding, but keep the overflows using a stride. This results
        # in one example possible giving several features when a context is long, each of those features having a
        # context that overlaps a bit the context of the previous feature.
        tokenized_examples = tokenizer(
            examples[question_column_name if pad_on_right else context_column_name],
            examples[context_column_name if pad_on_right else question_column_name],
            truncation="only_second" if pad_on_right else "only_first",
            max_length=max_seq_length,
            stride=data_args.doc_stride,
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
            padding="max_length" if data_args.pad_to_max_length else False,
        )

        # Since one example might give us several features if it has a long context, we need a map from a feature to
        # its corresponding example. This key gives us just that.
        sample_mapping = tokenized_examples.pop("overflow_to_sample_mapping")
        # The offset mappings will give us a map from token to character position in the original context. This will
        # help us compute the start_positions and end_positions.
        offset_mapping = tokenized_examples.pop("offset_mapping")

        # Let's label those examples!
        tokenized_examples["start_positions"] = []
        tokenized_examples["end_positions"] = []

        for i, offsets in enumerate(offset_mapping):
            # We will label impossible answers with the index of the CLS token.
            input_ids = tokenized_examples["input_ids"][i]
            cls_index = input_ids.index(tokenizer.cls_token_id)

            # Grab the sequence corresponding to that example (to know what is the context and what is the question).
            sequence_ids = tokenized_examples.sequence_ids(i)

            # One example can give several spans, this is the index of the example containing this span of text.
            sample_index = sample_mapping[i]
            answers = examples[answer_column_name][sample_index]
            # If no answers are given, set the cls_index as answer.
            if len(answers["answer_start"]) == 0:
                tokenized_examples["start_positions"].append(cls_index)
                tokenized_examples["end_positions"].append(cls_index)
            else:
                # Start/end character index of the answer in the text.
                start_char = answers["answer_start"][0]
                end_char = start_char + len(answers["text"][0])

                # Start token index of the current span in the text.
                token_start_index = 0
                while sequence_ids[token_start_index] != (1 if pad_on_right else 0):
                    token_start_index += 1

                # End token index of the current span in the text.
                token_end_index = len(input_ids) - 1
                while sequence_ids[token_end_index] != (1 if pad_on_right else 0):
                    token_end_index -= 1

                # Detect if the answer is out of the span (in which case this feature is labeled with the CLS index).
                if not (offsets[token_start_index][0] <= start_char and offsets[token_end_index][1] >= end_char):
                    tokenized_examples["start_positions"].append(cls_index)
                    tokenized_examples["end_positions"].append(cls_index)
                else:
                    # Otherwise move the token_start_index and token_end_index to the two ends of the answer.
                    # Note: we could go after the last offset if the answer is the last word (edge case).
                    while token_start_index < len(offsets) and offsets[token_start_index][0] <= start_char:
                        token_start_index += 1
                    tokenized_examples["start_positions"].append(token_start_index - 1)
                    while offsets[token_end_index][1] >= end_char:
                        token_end_index -= 1
                    tokenized_examples["end_positions"].append(token_end_index + 1)

        return tokenized_examples

    if training_args.do_train:
        if "train" not in raw_datasets:
            raise ValueError("--do_train requires a train dataset")
        train_dataset = raw_datasets["train"]
        if data_args.max_train_samples is not None:
            # We will select sample from whole data if argument is specified
            train_dataset = train_dataset.select(range(data_args.max_train_samples))
        # Create train feature from dataset
        with training_args.main_process_first(desc="train dataset map pre-processing"):
            train_dataset = train_dataset.map(
                prepare_train_features,
                batched=True,
                num_proc=data_args.preprocessing_num_workers,
                remove_columns=column_names,
                load_from_cache_file=not data_args.overwrite_cache,
                desc="Running tokenizer on train dataset",
            )
        if data_args.max_train_samples is not None:
            # Number of samples might increase during Feature Creation, We select only specified max samples
            train_dataset = train_dataset.select(range(data_args.max_train_samples))

    accelerator = Accelerator(cpu=training_args.no_cuda)

    def move_input_to_device(input, device):
        if isinstance(input, torch.Tensor):
            return input if input.device == device else input.to(device)
        elif isinstance(input, tuple):
            return tuple([move_input_to_device(ele, device) for ele in input])
        elif isinstance(input, list):
            return [move_input_to_device(ele, device) for ele in input]
        elif isinstance(input, dict):
            return {key: move_input_to_device(input[key], device) for key in input}
        else:
            raise TypeError("Only inputs types torch.Tensor, tuple, list and dict are supported")

    # get logits of teacher model
    # declare teacher config and model for distillation
    teacher_config = None
    teacher_model = None
    if optim_args.generate_teacher_logits:
        if not data_args.pad_to_max_length:
            raise ValueError("To computes teacher logits, pad_to_max_length must be set to True")
        teacher_config = AutoConfig.from_pretrained(optim_args.teacher_model_name_or_path)
        teacher_model = AutoModelForQuestionAnswering.from_pretrained(
            optim_args.teacher_model_name_or_path,
            from_tf=bool(".ckpt" in optim_args.teacher_model_name_or_path),
            config=teacher_config,
        )
        teacher_model_qa = QAModel(teacher_model)
        teacher_model_qa = accelerator.prepare(teacher_model_qa)
        num_param = lambda model: sum(p.numel() for p in model.parameters())
        logger.info(
            "***** Number of teacher model parameters: {:.2f}M *****".format(num_param(teacher_model_qa) / 10**6)
        )
        logger.info("***** Number of student model parameters: {:.2f}M *****".format(num_param(model) / 10**6))

        def get_logits(teacher_model_qa, train_dataset):
            logger.info("***** Getting logits of teacher model *****")
            logger.info(f"  Num examples = {len(train_dataset) }")
            logger.info(f"  Batch Size = {training_args.per_device_eval_batch_size }")

            sampler = None
            if accelerator.num_processes > 1:
                from transformers.trainer_pt_utils import ShardSampler

                sampler = ShardSampler(
                    train_dataset,
                    batch_size=training_args.per_device_eval_batch_size,
                    num_processes=accelerator.num_processes,
                    process_index=accelerator.process_index,
                )
            train_dataloader = DataLoader(
                train_dataset,
                collate_fn=data_collator,
                sampler=sampler,
                batch_size=training_args.per_device_eval_batch_size,
            )
            train_dataloader = tqdm(train_dataloader, desc="Evaluating")
            teacher_logits = []
            for step, batch in enumerate(train_dataloader):
                batch = move_input_to_device(batch, next(teacher_model_qa.parameters()).device)
                outputs = teacher_model_qa(**batch).cpu().detach().numpy()
                if accelerator.num_processes > 1:
                    outputs_list = [None for i in range(accelerator.num_processes)]
                    torch.distributed.all_gather_object(outputs_list, outputs)
                    outputs = np.concatenate(outputs_list, axis=0)
                teacher_logits += [[s, e] for s, e in zip(outputs[0::2], outputs[1::2])]
            if accelerator.num_processes > 1:
                teacher_logits = teacher_logits[: len(train_dataset)]

            return train_dataset.add_column("teacher_logits", teacher_logits)

        with torch.no_grad():
            train_dataset = get_logits(teacher_model_qa, train_dataset)

    # Validation preprocessing
    def prepare_validation_features(examples):
        # Some of the questions have lots of whitespace on the left, which is not useful and will make the
        # truncation of the context fail (the tokenized question will take a lots of space). So we remove that
        # left whitespace
        examples[question_column_name] = [q.lstrip() for q in examples[question_column_name]]

        # Tokenize our examples with truncation and maybe padding, but keep the overflows using a stride. This results
        # in one example possible giving several features when a context is long, each of those features having a
        # context that overlaps a bit the context of the previous feature.
        tokenized_examples = tokenizer(
            examples[question_column_name if pad_on_right else context_column_name],
            examples[context_column_name if pad_on_right else question_column_name],
            truncation="only_second" if pad_on_right else "only_first",
            max_length=max_seq_length,
            stride=data_args.doc_stride,
            return_overflowing_tokens=True,
            return_offsets_mapping=True,
            padding="max_length" if data_args.pad_to_max_length else False,
        )

        # Since one example might give us several features if it has a long context, we need a map from a feature to
        # its corresponding example. This key gives us just that.
        sample_mapping = tokenized_examples.pop("overflow_to_sample_mapping")

        # For evaluation, we will need to convert our predictions to substrings of the context, so we keep the
        # corresponding example_id and we will store the offset mappings.
        tokenized_examples["example_id"] = []

        for i in range(len(tokenized_examples["input_ids"])):
            # Grab the sequence corresponding to that example (to know what is the context and what is the question).
            sequence_ids = tokenized_examples.sequence_ids(i)
            context_index = 1 if pad_on_right else 0

            # One example can give several spans, this is the index of the example containing this span of text.
            sample_index = sample_mapping[i]
            tokenized_examples["example_id"].append(examples["id"][sample_index])

            # Set to None the offset_mapping that are not part of the context so it's easy to determine if a token
            # position is part of the context or not.
            tokenized_examples["offset_mapping"][i] = [
                (o if sequence_ids[k] == context_index else None)
                for k, o in enumerate(tokenized_examples["offset_mapping"][i])
            ]

        return tokenized_examples

    if training_args.do_eval:
        if "validation" not in raw_datasets:
            raise ValueError("--do_eval requires a validation dataset")
        eval_examples = raw_datasets["validation"]
        if data_args.max_eval_samples is not None:
            # We will select sample from whole data
            eval_examples = eval_examples.select(range(data_args.max_eval_samples))
        # Validation Feature Creation
        with training_args.main_process_first(desc="validation dataset map pre-processing"):
            eval_dataset = eval_examples.map(
                prepare_validation_features,
                batched=True,
                num_proc=data_args.preprocessing_num_workers,
                remove_columns=column_names,
                load_from_cache_file=not data_args.overwrite_cache,
                desc="Running tokenizer on validation dataset",
            )
        if data_args.max_eval_samples is not None:
            # During Feature creation dataset samples might increase, we will select required samples again
            eval_dataset = eval_dataset.select(range(data_args.max_eval_samples))

    # Post-processing:
    def post_processing_function(examples, features, predictions, stage="eval"):
        # Post-processing: we match the start logits and end logits to answers in the original context.
        predictions = postprocess_qa_predictions(
            examples=examples,
            features=features,
            predictions=predictions,
            version_2_with_negative=data_args.version_2_with_negative,
            n_best_size=data_args.n_best_size,
            max_answer_length=data_args.max_answer_length,
            null_score_diff_threshold=data_args.null_score_diff_threshold,
            output_dir=training_args.output_dir,
            log_level=log_level,
            prefix=stage,
        )
        # Format the result to the format the metric expects.
        if data_args.version_2_with_negative:
            formatted_predictions = [
                {"id": k, "prediction_text": v, "no_answer_probability": 0.0} for k, v in predictions.items()
            ]
        else:
            formatted_predictions = [{"id": k, "prediction_text": v} for k, v in predictions.items()]

        references = [{"id": ex["id"], "answers": ex[answer_column_name]} for ex in examples]
        return EvalPrediction(predictions=formatted_predictions, label_ids=references)

    metric = load_metric("squad_v2" if data_args.version_2_with_negative else "squad")

    def compute_metrics(p: EvalPrediction):
        return metric.compute(predictions=p.predictions, references=p.label_ids)

    # Initialize our Trainer
    trainer = QuestionAnsweringIncTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset if training_args.do_train else None,
        eval_dataset=eval_dataset if training_args.do_eval else None,
        eval_examples=eval_examples if training_args.do_eval else None,
        tokenizer=tokenizer,
        data_collator=data_collator,
        post_process_function=post_processing_function,
        compute_metrics=compute_metrics,
    )

    resume_from_checkpoint = training_args.resume_from_checkpoint
    metric_name = optim_args.metric

    def take_eval_steps(model, trainer, metric_name, save_metrics=False):
        trainer.model = model
        metrics = trainer.evaluate()
        if save_metrics:
            trainer.save_metrics("eval", metrics)
        logger.info("{}: {}".format(metric_name, metrics.get(metric_name)))
        return metrics[metric_name]

    def eval_func(model):
        return take_eval_steps(model, trainer, metric_name)

    def take_train_steps(model, trainer, resume_from_checkpoint, last_checkpoint):
        trainer.model_wrapped = model
        trainer.model = model
        trainer._signature_columns = None
        checkpoint = None
        if resume_from_checkpoint is not None:
            checkpoint = resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint
        train_result = trainer.train(agent, resume_from_checkpoint=checkpoint)
        metrics = train_result.metrics
        trainer.save_model()  # Saves the tokenizer too for easy upload
        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()
        return trainer.model

    def train_func(model):
        return take_train_steps(model, trainer, resume_from_checkpoint, last_checkpoint)

    quantizer = None
    pruner = None
    distiller = None

    if not optim_args.apply_quantization and not optim_args.apply_pruning and not optim_args.apply_distillation:
        raise ValueError("No optimization activated.")

    result_baseline_model = take_eval_steps(model, trainer, metric_name)

    default_config = os.path.join(os.path.abspath(os.path.join(__file__, os.path.pardir, os.path.pardir)), "config")

    if optim_args.apply_quantization:

        if not training_args.do_eval:
            raise ValueError("do_eval must be set to True for quantization.")

        q8_config = IncQuantizationConfig.from_pretrained(
            optim_args.quantization_config if optim_args.quantization_config is not None else default_config,
            config_file_name="quantization.yml",
            cache_dir=model_args.cache_dir,
        )

        # Set metric tolerance if specified
        if optim_args.tolerance_criterion is not None:
            q8_config.set_tolerance(optim_args.tolerance_criterion)

        # Set quantization approach if specified
        if optim_args.quantization_approach is not None:
            supported_approach = {"static", "dynamic", "aware_training"}
            if optim_args.quantization_approach not in supported_approach:
                raise ValueError(
                    "Unknown quantization approach. Supported approach are " + ", ".join(supported_approach)
                )
            quant_approach = getattr(IncQuantizationMode, optim_args.quantization_approach.upper()).value
            q8_config.set_config("quantization.approach", quant_approach)

        quant_approach = IncQuantizationMode(q8_config.get_config("quantization.approach"))
        # torch FX used for post-training quantization and quantization aware training
        # dynamic quantization will be added when torch FX is more mature
        if quant_approach != IncQuantizationMode.DYNAMIC:
            if not training_args.do_train:
                raise ValueError("do_train must be set to True for quantization aware training.")

            q8_config.set_config("model.framework", "pytorch_fx")

        calib_dataloader = trainer.get_train_dataloader() if quant_approach != IncQuantizationMode.DYNAMIC else None
        quantizer = IncQuantizer(
            q8_config, eval_func=eval_func, train_func=train_func, calib_dataloader=calib_dataloader
        )

    if optim_args.apply_pruning:

        if not training_args.do_train:
            raise ValueError("do_train must be set to True for pruning.")

        pruning_config = IncPruningConfig.from_pretrained(
            optim_args.pruning_config if optim_args.pruning_config is not None else default_config,
            config_file_name="prune.yml",
            cache_dir=model_args.cache_dir,
        )

        # Set targeted sparsity if specified
        if optim_args.target_sparsity is not None:
            pruning_config.set_config(
                "pruning.approach.weight_compression.target_sparsity", optim_args.target_sparsity
            )

        pruning_start_epoch = pruning_config.get_config("pruning.approach.weight_compression.start_epoch")
        pruning_end_epoch = pruning_config.get_config("pruning.approach.weight_compression.end_epoch")

        if pruning_start_epoch > training_args.num_train_epochs - 1:
            logger.warning(
                f"Pruning end epoch {pruning_start_epoch} is higher than the total number of training epoch "
                f"{training_args.num_train_epochs}. No pruning will be applied."
            )

        if pruning_end_epoch > training_args.num_train_epochs - 1:
            logger.warning(
                f"Pruning end epoch {pruning_end_epoch} is higher than the total number of training epoch "
                f"{training_args.num_train_epochs}. The target sparsity will not be reached."
            )

        # Creation Pruning object used for IncTrainer training loop
        pruner = IncPruner(pruning_config, eval_func=eval_func, train_func=train_func)

    if optim_args.apply_distillation:

        if optim_args.teacher_model_name_or_path is None:
            raise ValueError("A teacher model is needed to apply distillation.")

        if not training_args.do_train:
            raise ValueError("do_train must be set to True for distillation.")

        teacher_tokenizer = AutoTokenizer.from_pretrained(optim_args.teacher_model_name_or_path, use_fast=True)
        if teacher_config is None:
            teacher_config = AutoConfig.from_pretrained(optim_args.teacher_model_name_or_path)
        if teacher_model is None:
            teacher_model = AutoModelForQuestionAnswering.from_pretrained(
                optim_args.teacher_model_name_or_path,
                from_tf=bool(".ckpt" in optim_args.teacher_model_name_or_path),
                config=teacher_config,
            )

        teacher_model.to(training_args.device)

        if teacher_tokenizer.vocab != tokenizer.vocab:
            raise ValueError("Teacher model and student model should have same tokenizer.")

        distillation_config = IncDistillationConfig.from_pretrained(
            optim_args.distillation_config if optim_args.distillation_config is not None else default_config,
            config_file_name="distillation.yml",
            cache_dir=model_args.cache_dir,
        )

        # Creation Distillation object used for IncTrainer training loop
        distiller = IncDistiller(
            teacher_model=teacher_model, config=distillation_config, eval_func=eval_func, train_func=train_func
        )

    optimizer = IncOptimizer(
        model,
        quantizer=quantizer,
        pruner=pruner,
        distiller=distiller,
        one_shot_optimization=True,
        eval_func=eval_func,
        train_func=train_func,
    )

    agent = optimizer.get_agent()
    optimized_model = optimizer.fit()
    result_optimized_model = take_eval_steps(optimized_model, trainer, metric_name, save_metrics=True)

    # Save the resulting model and its corresponding configuration in the given directory
    optimizer.save_pretrained(training_args.output_dir)
    # Compute the model's sparsity
    sparsity = optimizer.get_sparsity()

    logger.info(
        f"Optimized model with {metric_name} of {result_optimized_model} and sparsity of {round(sparsity, 2)}% "
        f"saved to: {training_args.output_dir}. Original model had an {metric_name} of {result_baseline_model}."
    )

    if optim_args.apply_quantization and optim_args.verify_loading:

        # Load the model obtained after Intel Neural Compressor quantization
        loaded_model = IncQuantizedModelForQuestionAnswering.from_pretrained(training_args.output_dir)
        loaded_model.eval()
        result_loaded_model = take_eval_steps(loaded_model, trainer, metric_name)

        if result_loaded_model != result_optimized_model:
            logger.error("The quantized model was not successfully loaded.")
        else:
            logger.info(f"The quantized model was successfully loaded.")


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()