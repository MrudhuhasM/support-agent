"""Evaluation I/O only: parsing, validation, joins, and serialization; no scoring."""

import json
from pathlib import Path
from typing import TypedDict

from evaluation.files import fingerprint, read_jsonl, write_jsonl


JOBS = ("triage", "reply", "actions")
PUBLIC_ORDER_FIELDS = (
    "order_id", "order_status", "order_purchase_timestamp", "order_approved_at",
    "order_delivered_carrier_date", "order_delivered_customer_date",
    "order_estimated_delivery_date",
)


class TriagePrediction(TypedDict):
    intent: str
    urgency: str
    route: str


class ReplyPrediction(TypedDict):
    response: str


class ToolCall(TypedDict):
    type: str
    tool_name: str
    arguments: dict


class FinalResponse(TypedDict):
    type: str
    response: str


class GoldRecord(TypedDict):
    example_id: str
    job: str
    split: str
    labels: dict
    evaluation_context: dict
    hidden: dict
    review: dict


class PredictionRecord(TypedDict):
    prediction_id: str
    example_id: str
    job: str
    status: str
    raw_output: str | None
    parsed_prediction: dict | None
    observations: dict | None
    generation: dict


class EvaluationResult(TypedDict):
    """The owner supplies result fields and scorer identity; nothing computes them."""
    prediction_id: str
    example_id: str
    job: str
    scorer: str
    scorer_version: str
    result: dict


def parse_prediction(job: str, raw: str) -> dict:
    """Strict JSON output contract; malformed text remains available in raw logs."""
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Prediction must be a JSON object")
    if job == "triage":
        keys = {"intent", "urgency", "route"}
    elif job == "reply":
        keys = {"response"}
    elif job == "actions" and value.get("type") == "tool_call":
        keys = {"type", "tool_name", "arguments"}
        if not isinstance(value.get("arguments"), dict):
            raise ValueError("Tool arguments must be a JSON object")
    elif job == "actions" and value.get("type") == "final":
        keys = {"type", "response"}
    else:
        raise ValueError(f"Unknown job or action response type: {job}")
    if set(value) != keys:
        raise ValueError(f"Expected exactly these prediction fields: {sorted(keys)}")
    for key in keys - {"arguments"}:
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"Prediction field {key} must be a nonempty string")
    return value


def validate_model_input(record: dict) -> None:
    if set(record) != {"schema_version", "example_id", "job", "input"}:
        raise ValueError("Model input envelope contains unexpected fields")
    job, inputs = record["job"], record["input"]
    if record["schema_version"] != 1 or not isinstance(record["example_id"], str) or not record["example_id"]:
        raise ValueError("Invalid model-input schema version or example ID")
    expected = {
        "triage": {"customer_message", "request_date", "task_definition"},
        "reply": {"customer_message", "request_date", "order_facts", "policy_context"},
        "actions": {"customer_message", "request_date", "permitted_tools", "policy_context"},
    }
    if job not in JOBS or not isinstance(inputs, dict) or set(inputs) != expected[job]:
        raise ValueError("Unexpected or hidden fields in model-visible inputs")
    if not isinstance(inputs["customer_message"], str) or not inputs["customer_message"].strip():
        raise ValueError("Customer message must be nonempty")
    from store.actions import _parse_utc_date
    if _parse_utc_date(inputs["request_date"]) is None:
        raise ValueError("Model input needs the trusted request date")
    if job == "triage":
        definition = inputs["task_definition"]
        if not isinstance(definition, dict) or set(definition) != {"intents", "urgencies", "routes", "rule"}:
            raise ValueError("Task definition may contain generic label spaces/rules only")
    elif not isinstance(inputs["policy_context"], str) or not inputs["policy_context"].strip():
        raise ValueError("Policy context must be present")
    if job == "actions":
        from store.tools import TOOL_SCHEMAS
        if inputs["permitted_tools"] != TOOL_SCHEMAS:
            raise ValueError("Model inputs cannot expand the customer tool allowlist")
    elif job == "reply":
        facts = inputs["order_facts"]
        if not isinstance(facts, dict) or set(facts) != {"order", "return", "refund_authorization", "payments"}:
            raise ValueError("Unexpected reply facts")
        if set(facts["order"]) != set(PUBLIC_ORDER_FIELDS):
            raise ValueError("Reply order facts contain hidden or unknown fields")
        for key, allowed in (
            ("return", {"order_id", "return_status", "return_request_date"}),
            ("refund_authorization", {"order_id", "refund_status", "cash_cents", "voucher_cents"}),
        ):
            if facts[key] is not None and set(facts[key]) != allowed:
                raise ValueError("Reply state contains hidden or unknown fields")
        if not isinstance(facts["payments"], list) or any(set(row) != {"payment_type", "payment_value"} for row in facts["payments"]):
            raise ValueError("Reply payments contain hidden or unknown fields")


def validate_gold(record: dict, *, verified: bool = False) -> None:
    if record.get("job") not in JOBS or not isinstance(record.get("labels"), dict):
        raise ValueError("Invalid gold record")
    if record.get("schema_version") != 1 or record.get("split") not in {"train", "held_out"} or record.get("review", {}).get("status") not in {"draft", "verified", "rejected"}:
        raise ValueError("Invalid gold schema/split/review state")
    labels = record["labels"]
    job = record["job"]
    if job == "triage":
        parse_prediction("triage", json.dumps(labels))
    elif job == "reply":
        if set(labels) != {"reference_response", "reference_status", "rubric_status"}:
            raise ValueError("Invalid reply labels")
        response = labels["reference_response"]
        if response is not None and (not isinstance(response, str) or not response.strip()):
            raise ValueError("Reference response must be nonempty or null")
        if verified and response is not None and labels["reference_status"] != "reviewed":
            raise ValueError("A present reference must be manually reviewed")
    else:
        if set(labels) != {"expected_outcome", "required_behavior"}:
            raise ValueError("Invalid action labels")
        behavior = labels["required_behavior"]
        if behavior is not None:
            if not isinstance(behavior, dict) or set(behavior) != {"mode", "tool_name", "notes"}:
                raise ValueError("Invalid required_behavior contract")
            if behavior["mode"] not in {"tool_required", "reply_only", "either"} or not isinstance(behavior["notes"], str) or not behavior["notes"].strip():
                raise ValueError("Required behavior needs an explicit mode and decision note")
            from store.tools import CUSTOMER_TOOLS
            if behavior["tool_name"] is not None and behavior["tool_name"] not in CUSTOMER_TOOLS:
                raise ValueError("Required behavior cannot use a privileged/unknown tool")
            if behavior["mode"] == "tool_required" and behavior["tool_name"] is None:
                raise ValueError("Tool-required behavior needs a tool_name")
            if behavior["mode"] == "reply_only" and behavior["tool_name"] is not None:
                raise ValueError("Reply-only behavior cannot require a tool")
        if verified and (behavior is None or not isinstance(labels["expected_outcome"], dict)):
            raise ValueError("Action labels require a reviewed behavior choice and outcome")


def evaluation_pairs(predictions: Path, gold: Path, *, require_verified: bool = True,
                     allow_gold_revision: bool = False):
    """Join files for an owner-written scorer. No correctness comparison occurs."""
    gold_rows = read_jsonl(gold)
    gold_by_id = {row["example_id"]: row for row in gold_rows}
    if len(gold_by_id) != len(gold_rows):
        raise ValueError("Duplicate gold example IDs")
    seen = set()
    for prediction in read_jsonl(predictions):
        example_id = prediction["example_id"]
        if example_id in seen:
            raise ValueError("Duplicate completed predictions")
        seen.add(example_id)
        row = gold_by_id[example_id]
        if prediction["job"] != row["job"]:
            raise ValueError("Prediction/gold jobs do not match")
        if not allow_gold_revision and prediction.get("gold_record_sha256") != fingerprint(row):
            raise ValueError("Prediction was collected against different gold; explicitly opt into a reviewed gold revision")
        if require_verified and row["split"] == "held_out" and row["review"]["status"] != "verified":
            raise ValueError("Held-out gold requires manual verification before scoring")
        validate_gold(row, verified=require_verified and row["split"] == "held_out")
        if not isinstance(row.get("evaluation_context"), dict):
            raise ValueError("Gold lacks self-contained evaluation context; prepare a new dataset version")
        yield prediction, row


def save_evaluation_results(path: Path, results: list[EvaluationResult]) -> None:
    required = {"prediction_id", "example_id", "job", "scorer", "scorer_version", "result"}
    for result in results:
        if set(result) != required or not isinstance(result["result"], dict) or result["job"] not in JOBS:
            raise ValueError("Invalid owner-supplied evaluation result envelope")
    write_jsonl(path, results)
