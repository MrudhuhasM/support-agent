"""Create whitelisted job inputs and separate draft gold from frozen tickets."""

import argparse
import json
from pathlib import Path

from evaluation.data import prepare_dataset
from evaluation.files import load_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    manifest = prepare_dataset(args.tickets, args.output, load_json(args.config))
    print(json.dumps({"dataset_id": manifest["dataset_id"], "counts": manifest["counts"], "review_status": "draft"}, indent=2))


if __name__ == "__main__":
    main()
