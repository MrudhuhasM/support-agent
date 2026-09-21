"""Export human review sidecars or apply them as a new dataset version."""

import argparse
import copy
import json
from pathlib import Path

from evaluation.review import apply_reviews, export_review
from evaluation.data import load_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["export", "apply", "inspect"])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--reference-predictions", type=Path)
    parser.add_argument("--split", choices=["train", "held_out"], default="held_out")
    parser.add_argument("--job", choices=["triage", "reply", "actions"], default="triage")
    parser.add_argument("--example-id")
    args = parser.parse_args()
    if args.operation == "inspect":
        inputs, gold, _ = load_job(args.dataset, args.job, args.split)
        selected = [row for row in inputs if not args.example_id or row["example_id"] == args.example_id]
        for row in selected:
            displayed = copy.deepcopy(row)
            if "policy_context" in displayed["input"]:
                displayed["input"]["policy_context"] = "See " + str(args.dataset / "policy.md")
            print(json.dumps({"model_input": displayed, "gold_for_human_review": gold[row["example_id"]]}, indent=2, ensure_ascii=False))
        return
    if args.output is None:
        parser.error("export/apply need --output")
    if args.operation == "export":
        export_review(args.dataset, args.output, reference_predictions=args.reference_predictions, split=args.split)
    else:
        if args.reviews is None:
            parser.error("apply needs --reviews")
        apply_reviews(args.dataset, args.reviews, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
