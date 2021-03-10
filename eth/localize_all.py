
import argparse

import os

import subprocess

import types
methods = ["sift","superpoint"]

#methods = ["sift"]
datasets = ["delivery_area", "terrace", "meadow", "electro", "kicker", "office", "relief", "relief_2", "terrains", "facade","pipes", "playground", "courtyard"]
#datasets=["terrace"]

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        '--colmap_path', type=str, required=True,
        help='path to the COLMAP executable folder'
    )

    parser.add_argument(
        '--levels', type=int, default=None,
        help='path to the output results file'
    )

    parser.add_argument(
        '--method', type=str, default="all",
        help='path to the output results file'
    )

    args = parser.parse_args()
    return args

if __name__ == '__main__':
    args = parse_args()

    methods = methods if args.method == "all" else [args.method]

    for method_name in methods:
        for dataset_name in datasets:
            subprocess.call(["python","eth/benchmark_localization.py", 
            "--colmap_path", args.colmap_path, 
            "--dataset_name", dataset_name,  
            "--method_name", method_name,
            "--levels", str(args.levels)])
