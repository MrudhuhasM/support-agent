"""Prepare separate model-input and gold files from frozen ticket runs."""

import copy
import random
import shutil
from pathlib import Path

from evaluation.contracts import JOBS, PUBLIC_ORDER_FIELDS, validate_gold, validate_model_input
from evaluation.files import file_hash, fingerprint, load_json, read_jsonl, write_json, write_jsonl
from store.actions import _parse_utc_date
from store.tools import TOOL_SCHEMAS


SPLITS = ("train", "held_out")
TAXONOMY = {
    "version": "proposed-v1", "approved": False,
    "intents": ["cancellation", "delivery_issue", "return", "refund"],
    "urgencies": ["routine", "time_sensitive"],
    "routes": ["order_support", "returns_refunds"],
    "rule": "Delivery issues are time_sensitive; other intents are routine. Cancellation and delivery issues route to order_support; return and refund requests route to returns_refunds.",
    "labels_by_intent": {
        "cancellation": {"urgency": "routine", "route": "order_support"},
        "delivery_issue": {"urgency": "time_sensitive", "route": "order_support"},
        "return": {"urgency": "routine", "route": "returns_refunds"},
        "refund": {"urgency": "routine", "route": "returns_refunds"},
    },
}


def dataset_paths(directory: Path, job: str, split: str) -> tuple[Path, Path]:
    if job not in JOBS or split not in SPLITS:
        raise ValueError("Unknown job or split")
    directory = Path(directory)
    return directory / "inputs" / job / f"{split}.jsonl", directory / "gold" / job / f"{split}.jsonl"


def verify_artifacts(directory: Path) -> dict:
    directory = Path(directory)
    manifest = load_json(directory / "manifest.json")
    if manifest.get("status") != "complete":
        raise ValueError("Dataset/run is not complete")
    for name, expected in manifest.get("artifacts_sha256", {}).items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or file_hash(path) != expected:
            raise ValueError(f"Artifact fingerprint mismatch: {name}")
    return manifest


def _public_facts(ticket: dict) -> dict:
    initial = ticket["initial_environment"]
    return {
        "order": {key: ticket["order_state"].get(key) for key in PUBLIC_ORDER_FIELDS},
        "return": {key: initial["return"].get(key) for key in ("order_id", "return_status", "return_request_date")} if initial["return"] else None,
        "refund_authorization": {key: initial["refund_authorization"].get(key) for key in ("order_id", "refund_status", "cash_cents", "voucher_cents")} if initial["refund_authorization"] else None,
        "payments": [{key: row.get(key) for key in ("payment_type", "payment_value")} for row in ticket["payment_records"]],
    }


def _records(ticket: dict, job: str, policy: str, source_id: str, taxonomy: dict) -> tuple[dict, dict]:
    example_id = ticket["ticket_id"] + "-" + job
    visible = {"customer_message": ticket["customer_message"], "request_date": ticket["request_date"]}
    review_reasons = ["Human review has not occurred"]
    if job == "triage":
        visible["task_definition"] = {key: copy.deepcopy(taxonomy[key]) for key in ("intents", "urgencies", "routes", "rule")}
        intent = ticket["intent"]
        labels = {
            "intent": intent,
            **taxonomy["labels_by_intent"][intent],
        }
        review_reasons.append("Individual labels still require review; taxonomy approval: " + str(taxonomy["approved"]))
        hidden = {}
    elif job == "reply":
        visible.update(order_facts=_public_facts(ticket), policy_context=policy)
        labels = {"reference_response": None, "reference_status": "absent", "rubric_status": "owner_required"}
        hidden = {"source_intent": ticket["intent"]}
        review_reasons.append("Owner will define reply rubric; references are optional drafts")
    else:
        visible.update(permitted_tools=copy.deepcopy(TOOL_SCHEMAS), policy_context=policy)
        labels = {"expected_outcome": copy.deepcopy(ticket["expected_environment"]), "required_behavior": None}
        hidden = {
            "episode": {key: copy.deepcopy(ticket[key]) for key in (
                "order_id", "request_date", "order_state", "payment_records",
                "initial_environment", "environment_setup",
            )},
            "policy_simulation": {key: copy.deepcopy(ticket[key]) for key in (
                "expected_action", "expected_arguments", "expected_result_code",
                "expected_environment", "expected_action_events", "episode_start_event_id",
            )},
        }
        review_reasons.append("A valid tool result does not establish that this tool must be called")
        if ticket["intent"] == "delivery_issue":
            review_reasons.append("Delivery resolution is not defined by the store policy")
    inputs = {"schema_version": 1, "example_id": example_id, "job": job, "input": visible}
    gold = {
        "schema_version": 1, "example_id": example_id, "job": job,
        "split": ticket["split"], "order_id": ticket["order_id"],
        "labels": labels, "evaluation_context": copy.deepcopy(visible), "hidden": hidden,
        "review": {"status": "draft", "reviewer": None, "notes": None, "reasons": review_reasons},
        "provenance": {
            "source_dataset_id": source_id, "ticket_id": ticket["ticket_id"],
            "ticket_sha256": fingerprint(ticket), "generation": copy.deepcopy(ticket["generation"]),
            "source_policy_gaps": ticket["policy_gaps"],
        },
    }
    validate_model_input(inputs)
    validate_gold(gold)
    return inputs, gold


def prepare_dataset(source: Path, output: Path, config: dict) -> dict:
    """Create a new draft version, keeping the source partition unchanged."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.is_relative_to(source):
        raise ValueError("Prepared output must be outside the frozen source dataset")
    if output.exists():
        raise FileExistsError("Refusing to overwrite a prepared dataset")
    if type(config.get("seed")) is not int:
        raise ValueError("Preparation seed must be explicit")
    limit = config.get("per_intent_limit")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("per_intent_limit must be positive or null for the full dataset")
    taxonomy = copy.deepcopy(config.get("triage_taxonomy", TAXONOMY))
    if set(taxonomy.get("intents", [])) != set(TAXONOMY["intents"]) or not isinstance(taxonomy.get("approved"), bool) or not isinstance(taxonomy.get("version"), str) or not isinstance(taxonomy.get("rule"), str) or not taxonomy["version"].strip() or not taxonomy["rule"].strip():
        raise ValueError("Configure a versioned taxonomy for the four supported intents")
    for intent in TAXONOMY["intents"]:
        labels = taxonomy.get("labels_by_intent", {}).get(intent, {})
        if set(labels) != {"urgency", "route"} or labels["urgency"] not in taxonomy.get("urgencies", []) or labels["route"] not in taxonomy.get("routes", []):
            raise ValueError("Taxonomy rules must use the declared urgency/routing label spaces")
    source_manifest = verify_artifacts(source)
    partition = load_json(source / "order_splits.json")
    source_id = fingerprint({
        "manifest": file_hash(source / "manifest.json"),
        "train": file_hash(source / "train.jsonl"), "held_out": file_hash(source / "held_out.jsonl"),
    })
    policy = (source / "policy.md").read_text(encoding="utf-8")
    source_records = {split: read_jsonl(source / f"{split}.jsonl") for split in SPLITS}
    ids = {split: {row["order_id"] for row in rows} for split, rows in source_records.items()}
    if ids["train"] & ids["held_out"]:
        raise ValueError("Order leakage in source ticket files")
    seen = set()
    for split, rows in source_records.items():
        for row in rows:
            if row["ticket_id"] in seen or row["split"] != split or partition["orders"].get(row["order_id"]) != split:
                raise ValueError("Source ticket ID or order split is inconsistent")
            seen.add(row["ticket_id"])
            if row["intent"] not in TAXONOMY["intents"] or _parse_utc_date(row["request_date"]) is None:
                raise ValueError("Unsupported intent or invalid trusted request date")
    selected = {}
    for split, rows in source_records.items():
        selected[split] = []
        for intent in TAXONOMY["intents"]:
            candidates = sorted((row for row in rows if row["intent"] == intent), key=lambda row: row["ticket_id"])
            if limit is not None:
                seed = int(fingerprint([config["seed"], split, intent])[:16], 16)
                candidates = random.Random(seed).sample(candidates, min(limit, len(candidates)))
            selected[split].extend(candidates)
        selected[split].sort(key=lambda row: row["ticket_id"])
    output.mkdir(parents=True, exist_ok=False)
    artifacts = {}
    counts = {}
    for job in JOBS:
        counts[job] = {}
        for split in SPLITS:
            pairs = [_records(ticket, job, policy, source_id, taxonomy) for ticket in selected[split]]
            input_path, gold_path = dataset_paths(output, job, split)
            write_jsonl(input_path, [pair[0] for pair in pairs])
            write_jsonl(gold_path, [pair[1] for pair in pairs])
            for path in (input_path, gold_path):
                artifacts[path.relative_to(output).as_posix()] = file_hash(path)
            counts[job][split] = len(pairs)
    for name in ("order_splits.json", "policy.md"):
        shutil.copyfile(source / name, output / name)
        artifacts[name] = file_hash(output / name)
    write_json(output / "source_manifest.json", source_manifest)
    artifacts["source_manifest.json"] = file_hash(output / "source_manifest.json")
    manifest = {
        "schema_version": 1, "status": "complete", "dataset_id": fingerprint([source_id, config, taxonomy, artifacts]),
        "source_dataset_id": source_id, "source_sha256": source_manifest["source_sha256"],
        "config": config, "taxonomy": taxonomy, "counts": counts,
        "code_sha256": {name: file_hash(Path(__file__).parent / name) for name in ("data.py", "contracts.py")},
        "artifacts_sha256": artifacts, "certification": "All prepared examples are drafts until human review",
    }
    write_json(output / "manifest.json", manifest)
    return manifest


def load_job(directory: Path, job: str, split: str) -> tuple[list[dict], dict[str, dict], dict]:
    manifest = verify_artifacts(directory)
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported prepared dataset schema")
    input_path, gold_path = dataset_paths(directory, job, split)
    inputs, gold_rows = read_jsonl(input_path), read_jsonl(gold_path)
    gold = {row["example_id"]: row for row in gold_rows}
    input_ids = [row["example_id"] for row in inputs]
    if len(set(input_ids)) != len(input_ids) or len(gold) != len(gold_rows) or set(input_ids) != set(gold):
        raise ValueError("Inputs and gold must have unique matching example IDs")
    for row in inputs:
        validate_model_input(row)
        validate_gold(gold[row["example_id"]])
        if row["job"] != job or gold[row["example_id"]]["job"] != job or gold[row["example_id"]]["split"] != split:
            raise ValueError("Job/split metadata mismatch")
    return inputs, gold, manifest
