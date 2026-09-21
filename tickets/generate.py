"""Prepare tickets from source orders without writing to the Olist database.

Policy actions are simulated in isolated temporary environments to obtain draft
labels. This is data preparation, not an evaluation scorer. Teacher providers
write customer messages only and cannot supply or replace ground-truth labels.
"""

import hashlib
import json
import random
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from store.actions import _parse_utc_date, authorize_refund, cancel_order, request_return
from store.schema import initialize_schema
from store.trusted import approve_return


ROOT = Path(__file__).resolve().parents[1]
INTENTS = ("cancellation", "delivery_issue", "return", "refund")
SPLITS = ("train", "held_out")
ORDER_COLUMNS = (
    "order_id", "order_status", "order_estimated_delivery_date",
    "order_delivered_customer_date",
)
EVENT_DATES = (
    "order_purchase_timestamp", "order_approved_at",
    "order_delivered_carrier_date", "order_delivered_customer_date",
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seed(*parts: object) -> int:
    return int(hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()[:16], 16)


def validate_config(config: dict) -> None:
    """Require explicit sampling choices; never infer project label definitions."""
    for name in ("seed", "split_seed", "cancellation_after_days", "delivery_lateness_days",
                 "refund_return_after_days", "refund_request_after_days"):
        if type(config.get(name)) is not int or config[name] < 0:
            raise ValueError(f"Configure a nonnegative integer for {name}")
    fraction = config.get("held_out_fraction")
    if type(fraction) not in (int, float) or not 0 < fraction < 1:
        raise ValueError("Configure held_out_fraction between 0 and 1")
    if config["delivery_lateness_days"] == 0:
        raise ValueError("delivery_lateness_days must be positive for delivery-issue tickets")
    if not 0 <= config["refund_return_after_days"] <= 30:
        raise ValueError("refund_return_after_days must lie within the return window")
    if config["refund_request_after_days"] < config["refund_return_after_days"]:
        raise ValueError("Refund requests must follow their simulated return request")
    days = config.get("return_request_days")
    if not isinstance(days, list) or not days or any(type(day) is not int or day < 0 for day in days):
        raise ValueError("Configure a nonempty list of nonnegative return_request_days")
    states = config.get("refund_return_states")
    if not isinstance(states, list) or not states or any(state not in ("none", "created", "approved") for state in states):
        raise ValueError("refund_return_states must use none, created, or approved")
    for split in SPLITS:
        counts = config.get("tickets_per_split", {}).get(split, {})
        if set(counts) != set(INTENTS) or any(type(count) is not int or count < 0 for count in counts.values()):
            raise ValueError(f"Configure explicit nonnegative ticket counts for every intent in {split}")
    for intent in INTENTS:
        labels = config.get("triage_labels", {}).get(intent, {})
        if set(labels) != {"urgency", "route"} or any(
            value is not None and (not isinstance(value, str) or not value.strip())
            for value in labels.values()
        ):
            raise ValueError(f"Configure urgency/route for {intent}; use null to flag a design gap")
    generation = config.get("message_generation", {})
    if generation.get("mode") not in ("template", "provider"):
        raise ValueError("message_generation.mode must be template or provider")
    if not isinstance(generation.get("model"), str) or not generation["model"].strip():
        raise ValueError("Record a message_generation.model identity")
    if not isinstance(generation.get("parameters"), dict):
        raise ValueError("Record message_generation.parameters, even when empty")
    if generation["mode"] == "template" and generation["model"] != "offline-template-v1":
        raise ValueError("Template messages must be identified as offline-template-v1")


def _order_splits(ids: list[str], source_hash: str, path: Path, config: dict) -> dict:
    """Create a permanent order partition before selecting any tickets."""
    identity = {
        "source_sha256": source_hash,
        "split_seed": config["split_seed"],
        "held_out_fraction": config["held_out_fraction"],
    }
    if len(ids) < 2:
        raise ValueError("At least two source orders are needed for disjoint splits")
    shuffled = sorted(ids)
    random.Random(config["split_seed"]).shuffle(shuffled)
    held_out_count = max(1, min(len(ids) - 1, int(len(ids) * config["held_out_fraction"])))
    held_out_ids = set(shuffled[:held_out_count])
    manifest = {
        **identity,
        "orders": {order_id: "held_out" if order_id in held_out_ids else "train" for order_id in sorted(ids)},
    }
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != manifest:
            raise ValueError("Existing order split differs from the source or deterministic split settings; it cannot be overwritten")
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(_json(manifest) + "\n")
    return manifest


def _date_facts(order: dict) -> dict | None:
    dates = {}
    for column in EVENT_DATES:
        value = order.get(column)
        if value is None or value.strip() == "":
            continue
        parsed = _parse_utc_date(value)
        if parsed is None:
            return None
        dates[column] = parsed
    if not dates:
        return None
    delivered = dates.get("order_delivered_customer_date")
    if delivered is not None:
        if order["order_status"] != "delivered" or delivered < max(dates.values()):
            return None
    return {"latest": max(dates.values()), "delivered": delivered}


def _request_context(order: dict, intent: str, config: dict, seed: int) -> dict | None:
    dates = _date_facts(order)
    if dates is None:
        return None
    rng = random.Random(seed)
    context = {"refund_return_state": None, "return_request_date": None}
    if intent == "cancellation":
        request_date = dates["latest"] + timedelta(days=config["cancellation_after_days"])
    elif intent == "delivery_issue":
        estimated = _parse_utc_date(order["order_estimated_delivery_date"])
        if order["order_status"] not in {"created", "approved", "invoiced", "processing", "shipped"} or estimated is None:
            return None
        request_date = max(dates["latest"], estimated + timedelta(days=config["delivery_lateness_days"]))
    elif intent == "return":
        request_date = (dates["delivered"] or dates["latest"]) + timedelta(days=rng.choice(config["return_request_days"]))
    else:
        if order["order_status"] != "delivered" or dates["delivered"] is None:
            return None
        context["refund_return_state"] = rng.choice(config["refund_return_states"])
        context["return_request_date"] = (
            dates["delivered"] + timedelta(days=config["refund_return_after_days"])
        ).isoformat()
        request_date = dates["delivered"] + timedelta(days=config["refund_request_after_days"])
    context["request_date"] = request_date.isoformat()
    return context


def _environment_state(connection: sqlite3.Connection, order_id: str) -> dict:
    return_row = connection.execute(
        "SELECT * FROM order_returns WHERE order_id = ?", (order_id,)
    ).fetchone()
    refund = connection.execute(
        "SELECT * FROM refund_authorizations WHERE order_id = ?", (order_id,)
    ).fetchone()
    return {
        "order_status": connection.execute("SELECT order_status FROM orders WHERE order_id = ?", (order_id,)).fetchone()[0],
        "return": dict(return_row) if return_row else None,
        "refund_authorization": dict(refund) if refund else None,
    }


def _policy_label(order: dict, payments: list[dict], intent: str, context: dict) -> dict:
    """Apply the policy implementation to an isolated copy of just this task."""
    order_id = order["order_id"]
    with closing(sqlite3.connect(":memory:")) as environment:
        environment.row_factory = sqlite3.Row
        initialize_schema(environment)
        environment.execute(
            """INSERT INTO orders (order_id, order_status,
               order_estimated_delivery_date, order_delivered_customer_date)
               VALUES (?, ?, ?, ?)""", tuple(order[column] for column in ORDER_COLUMNS)
        )
        environment.executemany(
            "INSERT INTO order_payments VALUES (?, ?, ?)",
            [(order_id, payment["payment_type"], payment["payment_value"]) for payment in payments],
        )
        setup = []
        if context["refund_return_state"] in ("created", "approved"):
            arguments = {"order_id": order_id, "request_date": context["return_request_date"]}
            result = request_return(environment, **arguments)
            if result != "RETURN_CREATED":
                raise ValueError(f"Cannot prepare simulated return for {order_id}: {result}")
            setup.append({"tool_name": "request_return", "arguments": arguments, "result": result, "trusted_back_office": False})
            if context["refund_return_state"] == "approved":
                result = approve_return(environment, order_id)
                setup.append({"tool_name": "approve_return", "arguments": {"order_id": order_id}, "result": result, "trusted_back_office": True})
        environment.commit()
        initial = _environment_state(environment, order_id)
        episode_start = environment.execute("SELECT COALESCE(MAX(event_id), 0) FROM action_events").fetchone()[0]
        tool_name = {"cancellation": "cancel_order", "return": "request_return", "refund": "authorize_refund"}.get(intent)
        arguments = {"order_id": order_id}
        if intent == "return":
            arguments["request_date"] = context["request_date"]
        tools = {"cancel_order": cancel_order, "request_return": request_return, "authorize_refund": authorize_refund}
        result = tools[tool_name](environment, **arguments) if tool_name else None
        final = _environment_state(environment, order_id) if tool_name else None
        events = [dict(row) for row in environment.execute(
            "SELECT * FROM action_events WHERE event_id > ? ORDER BY event_id", (episode_start,)
        )]
    return {
        "environment_setup": setup,
        "initial_environment": initial,
        "episode_start_event_id": episode_start,
        "expected_action": tool_name,
        "expected_arguments": arguments if tool_name else None,
        "expected_result_code": result,
        "expected_environment": final,
        "expected_action_events": events,
        "label_provenance": "store_policy_tools_in_isolated_environment" if tool_name else "unresolved_policy_gap",
    }


def _template_message(order: dict, intent: str, context: dict) -> str:
    order_id, request_date = order["order_id"], context["request_date"]
    if intent == "cancellation":
        return f"Please cancel my order {order_id}. I am making this request on {request_date}."
    if intent == "delivery_issue":
        estimated = _parse_utc_date(order["order_estimated_delivery_date"]).isoformat()
        return f"Order {order_id} has not arrived as of {request_date}. Its estimated delivery date was {estimated}. Can you check it?"
    if intent == "return":
        return f"I would like to request a return for order {order_id} as of {request_date}."
    return f"I would like a refund for order {order_id} as of {request_date}."


def generate_tickets(
    source_database: Path,
    output_directory: Path,
    split_manifest: Path,
    config: dict,
    *,
    message_generator: Callable | None = None,
    allow_api: bool = False,
    policy_path: Path = ROOT / "policies" / "store_policy.md",
    prompt_path: Path = ROOT / "prompts" / "customer_ticket.txt",
) -> dict:
    """Write new train/held-out JSONL and provenance, without overwriting a run.

    An optional teacher callable has signature ``generator(prompt, *, model,
    parameters, seed) -> str``. Any external provider requires ``allow_api=True``;
    callers must obtain cost approval before opting in. Offline templates are
    the default and make no network requests.
    """
    validate_config(config)
    generation = config["message_generation"]
    if generation["mode"] == "provider" and (not allow_api or message_generator is None):
        raise ValueError("Provider generation requires an adapter and explicit API opt-in after cost approval")
    if generation["mode"] == "template" and message_generator is not None:
        raise ValueError("An external message generator requires provider mode and explicit API opt-in")
    source_database = Path(source_database).resolve()
    output_directory = Path(output_directory).resolve()
    split_manifest = Path(split_manifest).resolve()
    if output_directory.exists():
        raise FileExistsError(f"Refusing to overwrite dataset directory: {output_directory}")
    source_hash = _sha256(source_database)
    config_hash = hashlib.sha256(_json(config).encode("utf-8")).hexdigest()
    policy_bytes = Path(policy_path).read_bytes()
    prompt_bytes = Path(prompt_path).read_bytes()
    prompt_template = prompt_bytes.decode("utf-8")
    tickets = {split: [] for split in SPLITS}
    inventory = {}
    with closing(sqlite3.connect(source_database.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("PRAGMA query_only = ON")
        source.execute("BEGIN")
        columns = {row[1] for row in source.execute("PRAGMA table_info(orders)")}
        missing = set(ORDER_COLUMNS) - columns
        if missing:
            raise ValueError(f"Source orders lack required Olist columns: {sorted(missing)}")
        orders = [dict(row) for row in source.execute("SELECT * FROM orders ORDER BY order_id")]
        ids = [order["order_id"] for order in orders]
        if any(not isinstance(order_id, str) or not order_id.strip() for order_id in ids) or len(set(ids)) != len(ids):
            raise ValueError("Source order identifiers must be nonempty and unique")
        partition = _order_splits(ids, source_hash, split_manifest, config)
        for split in SPLITS:
            split_orders = [order for order in orders if partition["orders"][order["order_id"]] == split]
            inventory[split] = {}
            for intent in INTENTS:
                candidates = []
                for order in split_orders:
                    seed = _seed(config["seed"], split, intent, order["order_id"])
                    context = _request_context(order, intent, config, seed)
                    if context is not None:
                        candidates.append((order, context, seed))
                inventory[split][intent] = {"eligible_orders": len(candidates), "excluded_orders": len(split_orders) - len(candidates)}
                count = config["tickets_per_split"][split][intent]
                if count > len(candidates):
                    raise ValueError(f"Requested {count} {split}/{intent} tickets but only {len(candidates)} consistent source orders qualify")
                selection = random.Random(_seed(config["seed"], split, intent)).sample(candidates, count)
                for order, context, seed in selection:
                    payments = [dict(row) for row in source.execute(
                        "SELECT * FROM order_payments WHERE order_id = ? ORDER BY rowid", (order["order_id"],)
                    )]
                    label = _policy_label(order, payments, intent, context)
                    labels = config["triage_labels"][intent]
                    gaps = [name for name, value in labels.items() if value is None]
                    if intent == "delivery_issue":
                        gaps.append("delivery_issue_resolution_not_defined_in_store_policy")
                    ticket_id = hashlib.sha256(_json((source_hash, config_hash, split, intent, order["order_id"])).encode("utf-8")).hexdigest()[:24]
                    facts = {"order": order, "request_date": context["request_date"], "intent": intent, "initial_environment": label["initial_environment"]}
                    tickets[split].append({
                        "ticket_id": "ticket-" + ticket_id,
                        "order_id": order["order_id"],
                        "intent": intent,
                        "urgency": labels["urgency"],
                        "route": labels["route"],
                        "split": split,
                        "request_date": context["request_date"],
                        "request_date_provenance": "simulated_utc_date_consistent_with_fixed_olist_snapshot",
                        "order_state": order,
                        "payment_records": payments,
                        **label,
                        "policy_gaps": gaps,
                        "label_status": "requires_design_decision" if gaps else "draft_policy_derived",
                        "manual_verification_required": split == "held_out",
                        "verification_status": "pending_manual_verification" if split == "held_out" else "not_evaluated",
                        "message_verification_required": generation["mode"] == "provider",
                        "generation": {"prompt": prompt_template.format(facts=_json(facts)), "model": generation["model"], "parameters": generation["parameters"], "seed": seed, "config_sha256": config_hash},
                        "_template_message": _template_message(order, intent, context),
                    })

    if _sha256(source_database) != source_hash:
        raise RuntimeError("Source database changed during task preparation")
    output_directory.mkdir(parents=True, exist_ok=False)
    artifact_hashes = {}
    for name, content in (
        ("config.json", _json(config)), ("order_splits.json", _json(partition)),
    ):
        path = output_directory / name
        path.write_text(content + "\n", encoding="utf-8", newline="\n")
        artifact_hashes[name] = _sha256(path)
    for name, content in (("policy.md", policy_bytes), ("prompt_template.txt", prompt_bytes)):
        path = output_directory / name
        path.write_bytes(content)
        artifact_hashes[name] = _sha256(path)
    manifest_path = output_directory / "manifest.json"
    manifest_path.write_text(_json({
        "status": "incomplete", "source_sha256": source_hash, "config": config,
        "reason": "Generation has not completed; these files are not a dataset",
    }) + "\n", encoding="utf-8", newline="\n")

    # Preserve requests and responses even if a later provider call fails.
    attempts_path = output_directory / "generation_attempts.jsonl"
    # Provider responses are used only as prose; all policy labels already exist.
    for records in tickets.values():
        for ticket in records:
            generation_record = ticket["generation"]
            message = ticket.pop("_template_message")
            if generation["mode"] == "provider":
                with attempts_path.open("a", encoding="utf-8", newline="\n") as log:
                    log.write(_json({"status": "request", "ticket_id": ticket["ticket_id"], **generation_record}) + "\n")
                try:
                    message = message_generator(
                        generation_record["prompt"], model=generation["model"],
                        parameters=dict(generation["parameters"]), seed=generation_record["seed"],
                    )
                except Exception as error:
                    with attempts_path.open("a", encoding="utf-8", newline="\n") as log:
                        log.write(_json({"status": "error", "ticket_id": ticket["ticket_id"], "error_type": type(error).__name__}) + "\n")
                    raise
                with attempts_path.open("a", encoding="utf-8", newline="\n") as log:
                    log.write(_json({"status": "response", "ticket_id": ticket["ticket_id"], "message": message if isinstance(message, str) else None}) + "\n")
            if not isinstance(message, str) or not message.strip() or ticket["order_id"] not in message:
                raise ValueError("A generated message must be nonempty English prose containing the exact order ID; review factuality manually")
            ticket["customer_message"] = message.strip()

    if _sha256(source_database) != source_hash:
        raise RuntimeError("Source database changed during generation; run remains marked incomplete")
    if attempts_path.exists():
        artifact_hashes[attempts_path.name] = _sha256(attempts_path)
    for split, records in tickets.items():
        path = output_directory / f"{split}.jsonl"
        with path.open("x", encoding="utf-8", newline="\n") as output:
            for ticket in records:
                output.write(_json(ticket) + "\n")
        artifact_hashes[path.name] = _sha256(path)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    manifest = {
        "format_version": 1, "status": "complete", "source_database": str(source_database),
        "source_sha256": source_hash,
        "policy_sha256": hashlib.sha256(policy_bytes).hexdigest(),
        "prompt_template_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "config": config, "config_sha256": config_hash,
        "git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "code_sha256": {name: _sha256(ROOT / name) for name in (
            "tickets/generate.py", "store/actions.py", "store/payments.py",
            "store/schema.py", "store/trusted.py", "store/tools.py",
        )},
        "counts": {split: len(records) for split, records in tickets.items()},
        "candidate_inventory": inventory, "artifacts_sha256": artifact_hashes,
        "unresolved_design_rules": sorted({gap for records in tickets.values() for ticket in records for gap in ticket["policy_gaps"]}),
        "held_out_labels_require_manual_verification_before_evaluation": True,
        "provider_messages_require_factual_review": generation["mode"] == "provider",
    }
    manifest_path.write_text(_json(manifest) + "\n", encoding="utf-8", newline="\n")
    return manifest
