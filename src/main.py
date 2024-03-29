import argparse
import logging
import os
import shutil
import subprocess
from datetime import datetime

import torch
from torch.utils.tensorboard import SummaryWriter

from algorithm import (
    DSFLPlusSerialClientTrainer,
    DSFLPlusServerHandler,
    DSFLSerialClientTrainer,
    DSFLServerHandler,
    SingleSerialClientTrainer,
    SingleServerHandler,
)
from dataset import PartitionedDataset
from model import CNN_MNIST, CNN_FashionMNIST, ResNet18_CIFAR10, ResNet18_CIFAR100
from pipeline import DSFLPipeline, DSFLPlusPipeline, SinglePipeline
from utils import seed_everything


def main(args, logger, date_time, writer):
    seed_everything(args.seed)

    # data
    dataset_root = f"data/{args.task}"
    dataset_path = os.path.join(dataset_root, "partitions", date_time)
    partitioned_dataset = PartitionedDataset(
        root=dataset_root,
        path=dataset_path,
        num_clients=args.total_clients,
        partition=args.partition,
        num_shards_per_client=args.num_shards_per_client,
        dir_alpha=args.dir_alpha,
        task=args.task,
        public_private_split=args.public_private_split,
        public_size=args.public_size,
        private_size=args.private_size,
    )
    # test data
    test_loader = partitioned_dataset.get_dataloader(
        type="test", batch_size=args.test_batch_size
    )
    # data statistics
    client_stats = partitioned_dataset.get_client_stats()
    client_stats.to_csv(f"./logs/{date_time}.csv")

    # model
    match args.task:
        case "cifar10":
            model = ResNet18_CIFAR10()
            server_model = ResNet18_CIFAR10()
        case "cifar100":
            model = ResNet18_CIFAR100()
            server_model = ResNet18_CIFAR100()
        case "mnist":
            model = CNN_MNIST()
            server_model = CNN_MNIST()
        case "fmnist":
            model = CNN_FashionMNIST()
            server_model = CNN_FashionMNIST()
        case _:
            raise ValueError(f"Invalid task name: {args.task}")

    # server handler, client trainer and pipeline
    state_dict_dir = f"/tmp/{date_time}"
    cuda = torch.cuda.is_available()
    if args.algorithm == "dsfl":
        handler = DSFLServerHandler(
            model=server_model,
            global_round=args.com_round,
            sample_ratio=args.sample_ratio,
            cuda=cuda,
            temperature=args.temperature,
            public_size_per_round=args.public_size_per_round,
            logger=logger,
        )
        trainer = DSFLSerialClientTrainer(
            model=model,
            num_clients=args.total_clients,
            cuda=cuda,
            state_dict_dir=state_dict_dir,
            logger=logger,
        )
        handler.setup_kd_optim(args.kd_epochs, args.kd_batch_size, args.kd_lr)
        trainer.setup_optim(args.epochs, args.batch_size, args.lr)
        trainer.setup_kd_optim(args.kd_epochs, args.kd_batch_size, args.kd_lr)
        handler.setup_dataset(dataset=partitioned_dataset)
        trainer.setup_dataset(dataset=partitioned_dataset)

        standalone_pipeline = DSFLPipeline(
            handler=handler,
            trainer=trainer,
            test_loader=test_loader,
            logger=logger,
            writer=writer,
        )
    elif args.algorithm == "dsflplus":
        handler = DSFLPlusServerHandler(
            model=server_model,
            global_round=args.com_round,
            sample_ratio=args.sample_ratio,
            cuda=cuda,
            temperature=args.temperature,
            public_size_per_round=args.public_size_per_round,
            logger=logger,
        )

        trainer = DSFLPlusSerialClientTrainer(
            model=model,
            num_clients=args.total_clients,
            cuda=cuda,
            state_dict_dir=state_dict_dir,
            logger=logger,
            ood_detection_score=args.ood_detection_score,
            ood_detection_threshold_delta=args.ood_detection_threshold_delta,
        )
        handler.setup_kd_optim(args.kd_epochs, args.kd_batch_size, args.kd_lr)
        trainer.setup_optim(args.epochs, args.batch_size, args.lr)
        trainer.setup_kd_optim(args.kd_epochs, args.kd_batch_size, args.kd_lr)
        handler.setup_dataset(dataset=partitioned_dataset)
        trainer.setup_dataset(dataset=partitioned_dataset)
        trainer.setup_datetime(date_time)

        standalone_pipeline = DSFLPlusPipeline(
            handler=handler,
            trainer=trainer,
            test_loader=test_loader,
            logger=logger,
            writer=writer,
        )
    elif args.algorithm == "single":
        handler = SingleServerHandler(
            model=model,
            global_round=args.com_round,
            sample_ratio=args.sample_ratio,
            cuda=cuda,
            logger=logger,
        )
        trainer = SingleSerialClientTrainer(
            model=model,
            num_clients=args.total_clients,
            cuda=cuda,
            state_dict_dir=state_dict_dir,
            logger=logger,
        )
        trainer.setup_optim(args.epochs, args.batch_size, args.lr)
        trainer.setup_dataset(dataset=partitioned_dataset)

        standalone_pipeline = SinglePipeline(
            handler=handler,
            trainer=trainer,
            test_loader=test_loader,
            logger=logger,
            writer=writer,
        )
    else:
        raise ValueError(f"Invalid algorithm name: {args.algorithm}")

    standalone_pipeline.main()


def clean_up(args, date_time, writer):
    """Clean up temporary files."""
    writer.flush()
    writer.close()
    state_dict_path = f"/tmp/{date_time}"
    if os.path.exists(state_dict_path):
        shutil.rmtree(state_dict_path)
    dataset_path = f"./data/{args.task}/partitions/{date_time}"
    if os.path.exists(dataset_path):
        shutil.rmtree(dataset_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # algorithm
    parser.add_argument(
        "--algorithm",
        type=str,
        required=True,
        choices=["dsfl", "dsflplus", "single"],
        help="Federated Learning Algorithm to use.",
    )
    # dataset
    parser.add_argument(
        "--task",
        type=str,
        default="cifar10",
        choices=["mnist", "fmnist", "cifar10", "cifar100"],
        help="Dataset for the Federated Learning task.",
    )
    parser.add_argument(
        "--partition",
        type=str,
        default="shards",
        choices=["shards", "hetero_dir", "client_inner_dirichlet"],
        help="Partition strategy for the dataset.",
    )
    parser.add_argument(
        "--num_shards_per_client",
        type=int,
        default=2,
        help="Number of shards per client.",
    )
    parser.add_argument(
        "--dir_alpha",
        type=float,
        default=0.5,
        help="Parameter for Dirichlet distribution.",
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Seed for reproducibility."
    )
    parser.add_argument(
        "--public_private_split",
        type=str,
        default="even_class",
        choices=["even_class", "random_sample"],
        help="Strategy for splitting data into public and private sets.",
    )
    parser.add_argument(
        "--private_size", type=int, default=40000, help="Size of the private dataset."
    )
    parser.add_argument(
        "--public_size", type=int, default=10000, help="Size of the public dataset."
    )
    parser.add_argument(
        "--public_size_per_round",
        type=int,
        default=1000,
        help="Size of the public data used per round.",
    )
    # server
    parser.add_argument(
        "--sample_ratio", type=float, default=1.0, help="Sampling ratio for clients."
    )
    parser.add_argument(
        "--com_round", type=int, default=500, help="Number of communication rounds."
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Temperature for Entropy Reduction Algorithm.",
    )
    # client
    parser.add_argument(
        "--total_clients", type=int, default=100, help="Total number of clients."
    )
    parser.add_argument(
        "--batch_size", type=int, default=100, help="Batch size for local training."
    )
    parser.add_argument(
        "--epochs", type=int, default=5, help="Number of epochs for local training."
    )
    parser.add_argument(
        "--lr", type=float, default=0.1, help="Learning rate for local training."
    )
    parser.add_argument(
        "--kd_epochs",
        type=int,
        default=5,
        help="Number of epochs for Knowledge Distillation.",
    )
    parser.add_argument(
        "--kd_batch_size",
        type=int,
        default=100,
        help="Batch size for Knowledge Distillation.",
    )
    parser.add_argument(
        "--kd_lr",
        type=float,
        default=0.1,
        help="Learning rate for Knowledge Distillation.",
    )
    parser.add_argument(
        "--ood_detection_score",
        type=str,
        default=None,
        choices=[
            "energy",
            "msp",
            "maxlogit",
            "gen",
            "random",
        ],
        help="Score function for Out-of-Distribution detection.",
    )
    parser.add_argument(
        "--ood_detection_threshold_delta",
        type=float,
        default=0.01,
        help="Threshold delta for Out-of-Distribution detection.",
    )
    # others
    parser.add_argument(
        "--test_batch_size", type=int, default=500, help="Batch size for testing."
    )
    parser.add_argument(
        "--comment", type=str, default="", help="Additional comments or notes."
    )

    args = parser.parse_args()

    date_time = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    os.makedirs(f"tmp/{date_time}", exist_ok=True)

    # logging
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger()
    file_handler = logging.FileHandler(f"./logs/{date_time}.log")
    file_handler.setFormatter(
        logging.Formatter("{asctime} [{levelname:.4}] {message}", style="{")
    )
    logger.addHandler(file_handler)

    logger.info(
        "args:\n"
        + "\n".join(
            [f"--{k}={v} \\" for k, v in args.__dict__.items() if v is not None]
        )
    )
    cmd = "git rev-parse --short HEAD"
    logger.info(
        f"git commit hash: {subprocess.check_output(cmd.split()).strip().decode('utf-8')}"
    )
    if torch.cuda.is_available():
        logger.info(f"Running on {os.uname()[1]} ({torch.cuda.get_device_name()})")

    writer = SummaryWriter()

    try:
        main(args, logger, date_time, writer)
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt")
    except Exception as e:
        logging.exception(e)
    finally:
        clean_up(args, date_time, writer)
