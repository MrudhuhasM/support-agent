"""Resumable prediction collection. No scorer, reward, or metric is invoked."""

import copy
import platform
import subprocess
from pathlib import Path

from evaluation.contracts import parse_prediction, validate_gold
from evaluation.data import dataset_paths, load_job
from evaluation.episodes import run_episode
from evaluation.files import (append_jsonl, file_hash, fingerprint, load_json,
                              read_journal, replace_jsonl, write_json)
from evaluation.models import create_adapter
from evaluation.prompts import build_messages, load_few_shots


ROOT = Path(__file__).resolve().parents[1]


def _error(error: Exception) -> dict:
    return {"text": None, "usage": None, "latency_seconds": None,
            "provider_metadata": {}, "error": {"type": type(error).__name__, "message": str(error)}}


def run_predictions(dataset: Path, output: Path, job: str, split: str, config: dict, *,
                    allow_api: bool = False, allow_download: bool = False,
                    allow_draft: bool = False, retry_errors: bool = False,
                    retry_pending_api: bool = False, adapter: dict | None = None,
                    purpose: str = "predictions") -> dict:
    """One writer per run directory. Durable responses make action replay safe.

    Failed completed records are retained and skipped unless retry_errors is
    explicit. An ambiguous started API request additionally needs
    retry_pending_api; exactly-once billing cannot be guaranteed by a client.
    """
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if output.is_relative_to(dataset):
        raise ValueError("Model runs must be outside the frozen dataset")
    inputs, gold, dataset_manifest = load_job(dataset, job, split)
    if type(config.get("seed")) is not int:
        raise ValueError("Configure an integer model-run seed")
    batch_size = config.get("batch_size", 1)
    if type(batch_size) is not int or batch_size < 1:
        raise ValueError("batch_size must be positive")
    for name, default in (("max_steps", 6), ("max_tool_calls", 4)):
        value = config.get(name, default)
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be positive")
    limit = config.get("max_examples")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("max_examples must be positive or null")
    excluded = [row["example_id"] for row in inputs if gold[row["example_id"]]["review"]["status"] == "rejected"]
    inputs = [row for row in inputs if row["example_id"] not in excluded]
    if limit is not None:
        inputs = inputs[:limit]
    for row in inputs:
        reference = gold[row["example_id"]]
        if split == "held_out" and not allow_draft:
            if reference["review"]["status"] != "verified":
                raise ValueError("Held-out labels are drafts; review them or explicitly use --allow-draft for diagnostics")
            validate_gold(reference, verified=True)
    partition = load_json(dataset / "order_splits.json")["orders"]
    held_out_ids = {order_id for order_id, assignment in partition.items() if assignment == "held_out"}
    few_shot_path = config.get("few_shot_file")
    few_shot_path = (ROOT / few_shot_path).resolve() if few_shot_path else None
    few_shots = load_few_shots(few_shot_path, held_out_ids, {order_id for order_id, assignment in partition.items() if assignment == "train"})
    taxonomy = dataset_manifest["taxonomy"]
    for example in few_shots:
        if example["job"] == "triage" and any(
            example["prediction"][key] not in taxonomy[space]
            for key, space in (("intent", "intents"), ("urgency", "urgencies"), ("route", "routes"))
        ):
            raise ValueError("Few-shot labels do not match the prepared taxonomy version")
    if config.get("prompt_style") == "base" and len([row for row in few_shots if row["job"] == job]) < 2:
        raise ValueError("The Base baseline needs at least two declared few-shot examples for this job")
    input_path, gold_path = dataset_paths(dataset, job, split)
    code_files = ("evaluation/runner.py", "evaluation/models.py", "evaluation/episodes.py", "evaluation/data.py",
                  "evaluation/prompts.py", "evaluation/contracts.py", "evaluation/files.py",
                  "store/tools.py", "store/actions.py", "store/schema.py", "store/trusted.py", "store/payments.py", "store/lookup.py")
    identity = {
        "dataset_id": dataset_manifest["dataset_id"], "input_sha256": file_hash(input_path),
        "gold_sha256": file_hash(gold_path), "job": job, "split": split,
        "config": config, "allow_draft": allow_draft, "purpose": purpose,
        "few_shot_sha256": file_hash(few_shot_path) if few_shot_path else None,
        "code_sha256": {name: file_hash(ROOT / name) for name in code_files},
    }
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else None
    if manifest is not None and manifest["identity"] != identity:
        raise ValueError("Run identity changed; use a new output directory")
    if manifest is None:
        if any(output.iterdir()):
            raise ValueError("Existing non-run directory cannot be overwritten")
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False)
        manifest = {"schema_version": 1, "status": "running", "identity": identity,
                    "git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
                    "dataset_directory": str(dataset),
                    "python_version": platform.python_version(), "excluded_rejected_examples": excluded}
        write_json(manifest_path, manifest)
    journal = output / "attempts.jsonl"
    prior = read_journal(journal)
    latest = {row["prediction_id"]: row for row in prior}
    tasks = []
    refresh_predictions = set()
    for row in inputs:
        prediction_id = fingerprint([identity, row["example_id"]])
        old = latest.get(prediction_id)
        if old is not None and (old["status"] == "ok" or not retry_errors):
            continue
        if old is not None and old["status"] in {"parse_error", "turn_limit", "tool_limit"}:
            refresh_predictions.add(prediction_id)
        tasks.append((row, prediction_id))
    if tasks:
        if manifest["python_version"] != platform.python_version():
            raise ValueError("Python runtime changed; create a new model run")
        if adapter is None:
            adapter = create_adapter(config, allow_api=allow_api, allow_download=allow_download)
        if adapter["requires_api"] and not allow_api:
            raise PermissionError("API predictions require explicit cost approval via --allow-api")
        if "model_runtime" in manifest and manifest["model_runtime"] != adapter["metadata"]:
            raise ValueError("Resolved model/runtime changed; create a new run")
        manifest["model_runtime"] = copy.deepcopy(adapter["metadata"])
        manifest["status"] = "running"
        write_json(manifest_path, manifest)

    def cached_generate(requests: list[dict]) -> list[dict]:
        answers = [None] * len(requests)
        pending = []
        for index, request in enumerate(requests):
            response_path = output / "responses" / (request["request_id"] + ".json")
            request_path = output / "requests" / (request["request_id"] + ".json")
            request_hash = fingerprint(request)
            force_refresh = request["request_id"] in refresh_request_ids
            if response_path.exists() and not force_refresh:
                saved = load_json(response_path)
                if saved["request_sha256"] != request_hash:
                    raise ValueError("Cached request differs from reconstructed prompt")
                if saved["response"].get("error") is None or not retry_errors:
                    answers[index] = saved["response"]
                    continue
            elif not response_path.exists() and request_path.exists() and adapter["requires_api"] and not retry_pending_api:
                answers[index] = _error(PermissionError("API request was started without a durable response; check provider usage before --retry-pending-api"))
                continue
            pending.append((index, request, request_path, response_path, request_hash))
        # API responses are persisted individually before another paid request.
        groups = [[item] for item in pending] if adapter["requires_api"] else [pending]
        for group in groups:
            if not group:
                continue
            for _, request, request_path, _, request_hash in group:
                write_json(request_path, {"status": "started", "request": request, "request_sha256": request_hash,
                                          "model": adapter["metadata"]})
            try:
                responses = adapter["generate"]([item[1] for item in group])
                if not isinstance(responses, list) or len(responses) != len(group):
                    raise ValueError("Adapter must return exactly one result per request")
            except Exception as error:
                responses = [_error(error) for _ in group]
            for (index, request, _, response_path, request_hash), response in zip(group, responses):
                if not isinstance(response, dict) or not {"text", "usage", "latency_seconds", "provider_metadata", "error"}.issubset(response):
                    response = _error(ValueError("Invalid adapter response contract"))
                if response["error"] is None and not isinstance(response["text"], str):
                    response = _error(ValueError("Successful adapter responses need text"))
                write_json(response_path, {"request_sha256": request_hash, "response": response})
                append_jsonl(output / "generation.jsonl", {"request_id": request["request_id"], "response": response})
                answers[index] = response
        return answers

    def finish(row: dict, prediction_id: str, response: dict | None, observations: dict | None = None) -> None:
        status, parsed, error = "ok", None, None
        if observations is not None:
            last = observations["turns"][-1]["generation"] if observations["turns"] else None
            response = last
            if observations["termination"] == "final":
                parsed = {"type": "final", "response": observations["final_response"]}
            else:
                status = observations["termination"]
                error = response.get("error") if response else None
        elif response["error"] is not None:
            status, error = "model_error", response["error"]
        else:
            try:
                parsed = parse_prediction(job, response["text"])
            except (ValueError, TypeError) as failure:
                status, error = "parse_error", {"type": type(failure).__name__, "message": str(failure)}
        attempt = sum(item["prediction_id"] == prediction_id for item in prior) + 1
        record = {
            "schema_version": 1, "prediction_id": prediction_id, "example_id": row["example_id"],
            "job": job, "status": status, "raw_output": response["text"] if response else None,
            "parsed_prediction": parsed, "error": error, "observations": observations,
            "dataset_id": dataset_manifest["dataset_id"], "gold_record_sha256": fingerprint(gold[row["example_id"]]),
            "purpose": purpose, "model_output_review_status": "unreviewed",
            "input_record_sha256": fingerprint(row), "attempt": attempt,
            "generation": {"model": manifest.get("model_runtime"), "parameters": config.get("parameters", {}),
                           "seed": int(fingerprint([config["seed"], row["example_id"]])[:8], 16) % (2**31),
                           "response_metadata": response},
        }
        append_jsonl(journal, record)
        prior.append(record)
        latest[prediction_id] = record

    refresh_request_ids = set()
    for prediction_id in refresh_predictions:
        for turn in range(config.get("max_steps", 6) if job == "actions" else 1):
            refresh_request_ids.add(fingerprint([prediction_id, turn]))
    if job == "actions":
        for row, prediction_id in tasks:
            base_seed = int(fingerprint([config["seed"], row["example_id"]])[:8], 16) % (2**31)

            def generate_one(messages, turn):
                return cached_generate([{"request_id": fingerprint([prediction_id, turn]), "job": job,
                                         "messages": messages, "seed": (base_seed + turn) % (2**31)}])[0]

            try:
                observations = run_episode(
                    gold[row["example_id"]]["hidden"]["episode"], build_messages(row, few_shots), generate_one,
                    max_steps=config.get("max_steps", 6), max_tool_calls=config.get("max_tool_calls", 4),
                )
                finish(row, prediction_id, None, observations)
            except Exception as error:
                finish(row, prediction_id, _error(error))
    else:
        for start in range(0, len(tasks), batch_size):
            batch = tasks[start:start + batch_size]
            requests = [{"request_id": fingerprint([prediction_id, 0]), "job": job,
                         "messages": build_messages(row, few_shots),
                         "seed": int(fingerprint([config["seed"], row["example_id"]])[:8], 16) % (2**31)}
                        for row, prediction_id in batch]
            responses = cached_generate(requests)
            for (row, prediction_id), response in zip(batch, responses):
                finish(row, prediction_id, response)
    ordered_ids = [fingerprint([identity, row["example_id"]]) for row in inputs]
    predictions = [latest[prediction_id] for prediction_id in ordered_ids if prediction_id in latest]
    replace_jsonl(output / "predictions.jsonl", predictions)
    manifest.update(status="complete", completed_predictions=len(predictions),
                    predictions_sha256=file_hash(output / "predictions.jsonl"))
    write_json(manifest_path, manifest)
    return manifest
