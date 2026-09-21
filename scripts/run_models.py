"""Collect predictions or draft references; no evaluation scores are computed."""

import argparse
import json
from pathlib import Path

from evaluation.data import load_job
from evaluation.files import load_json
from evaluation.runner import run_predictions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job", choices=["triage", "reply", "actions"], required=True)
    parser.add_argument("--split", choices=["train", "held_out"], default="held_out")
    parser.add_argument("--allow-draft", action="store_true", help="Diagnostic runs only; does not verify labels")
    parser.add_argument("--allow-api", action="store_true", help="Explicit paid/API approval after checking the call/token budget")
    parser.add_argument("--allow-download", action="store_true", help="Explicitly allow model/tokenizer downloads")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--retry-pending-api", action="store_true", help="Retry an ambiguous request only after checking provider usage")
    parser.add_argument("--purpose", choices=["predictions", "reference_draft"], default="predictions")
    parser.add_argument("--plan", action="store_true", help="Inspect the call budget without creating an adapter or making requests")
    args = parser.parse_args()
    config = load_json(args.config)
    if args.purpose == "reference_draft" and args.job != "reply":
        parser.error("Reference drafts use the reply job")
    if args.plan:
        inputs, gold, _ = load_job(args.dataset, args.job, args.split)
        eligible = [row for row in inputs if gold[row["example_id"]]["review"]["status"] != "rejected"]
        if config.get("max_examples"):
            eligible = eligible[:config["max_examples"]]
        print(json.dumps({"examples": len(eligible), "maximum_model_calls": len(eligible) * (config.get("max_steps", 6) if args.job == "actions" else 1),
                          "parameters": config.get("parameters", {}), "model_id": config.get("model_id"), "requests_made": 0}, indent=2))
        return
    result = run_predictions(
        args.dataset, args.output, args.job, args.split, config,
        allow_api=args.allow_api, allow_download=args.allow_download,
        allow_draft=args.allow_draft, retry_errors=args.retry_errors,
        retry_pending_api=args.retry_pending_api, purpose=args.purpose,
    )
    print(json.dumps({"status": result["status"], "completed_predictions": result["completed_predictions"],
                      "predictions": str(args.output / "predictions.jsonl")}, indent=2))


if __name__ == "__main__":
    main()
