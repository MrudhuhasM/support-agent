"""Execute bounded customer episodes and record observations, without rewards."""

import copy
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from evaluation.contracts import parse_prediction
from evaluation.files import canonical
from store.actions import _parse_utc_date, request_return
from store.schema import initialize_schema
from store.tools import call_customer_tool
from store.trusted import approve_return


TABLES = ("orders", "order_returns", "order_payments", "refund_authorizations", "action_events")


def _snapshot(connection: sqlite3.Connection) -> dict:
    return {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in TABLES}


def _initialize(connection: sqlite3.Connection, episode: dict) -> int:
    order = episode["order_state"]
    order_id = episode["order_id"]
    if order["order_id"] != order_id or _parse_utc_date(episode["request_date"]) is None:
        raise ValueError("Invalid trusted episode identity or request date")
    initialize_schema(connection)
    connection.execute(
        """INSERT INTO orders (order_id, order_status, order_estimated_delivery_date,
           order_delivered_customer_date) VALUES (?, ?, ?, ?)""",
        tuple(order.get(key) for key in ("order_id", "order_status", "order_estimated_delivery_date", "order_delivered_customer_date")),
    )
    connection.executemany("INSERT INTO order_payments VALUES (?, ?, ?)", [
        (order_id, row.get("payment_type"), row.get("payment_value")) for row in episode["payment_records"]
    ])
    for step in episode["environment_setup"]:
        arguments = step["arguments"]
        if arguments.get("order_id") != order_id:
            raise ValueError("Setup targets another order")
        if step["tool_name"] == "request_return" and set(arguments) == {"order_id", "request_date"}:
            setup_date = _parse_utc_date(arguments["request_date"])
            if setup_date is None or setup_date > _parse_utc_date(episode["request_date"]):
                raise ValueError("Trusted setup cannot occur after the episode")
            result = request_return(connection, **arguments)
        elif step["tool_name"] == "approve_return" and set(arguments) == {"order_id"}:
            result = approve_return(connection, **arguments)
        else:
            raise ValueError("Unsupported trusted setup operation")
        if result != step["result"]:
            raise ValueError("Recorded setup is not reproducible")
    state = _snapshot(connection)
    initial = episode["initial_environment"]
    return_state = state["order_returns"][0] if state["order_returns"] else None
    refund_state = state["refund_authorizations"][0] if state["refund_authorizations"] else None
    if order["order_status"] != initial["order_status"] or return_state != initial["return"] or refund_state != initial["refund_authorization"]:
        raise ValueError("Replayed setup does not reproduce the trusted initial environment")
    connection.commit()
    return connection.execute("SELECT COALESCE(MAX(event_id), 0) FROM action_events").fetchone()[0]


def run_episode(episode: dict, messages: list[dict], generate_one, *, max_steps: int = 6,
                max_tool_calls: int = 4) -> dict:
    """generate_one(messages, turn) returns one normalized model response.

    Only messages go to the provider. Hidden episode data is used exclusively by
    the host. Replaying cached responses rebuilds a fresh database on resume.
    """
    if type(max_steps) is not int or max_steps < 1 or type(max_tool_calls) is not int or max_tool_calls < 1:
        raise ValueError("Episode limits must be positive integers")
    transcript = copy.deepcopy(messages)
    turns, tool_calls = [], 0
    final_response, termination = None, "turn_limit"
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "episode.sqlite"
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            episode_start = _initialize(connection, episode)
            initial_state = _snapshot(connection)
            for turn in range(max_steps):
                response = generate_one(copy.deepcopy(transcript), turn)
                entry = {"turn": turn, "generation": response, "parsed": None, "observation": None}
                turns.append(entry)
                if response.get("error"):
                    termination = "model_error"
                    break
                raw = response["text"]
                transcript.append({"role": "assistant", "content": raw})
                try:
                    parsed = parse_prediction("actions", raw)
                    entry["parsed"] = parsed
                except (ValueError, TypeError) as error:
                    entry["observation"] = {"error": {"type": "ParseError", "message": str(error)}}
                    transcript.append({"role": "user", "content": "Tool observation: " + canonical(entry["observation"])})
                    continue
                if parsed["type"] == "final":
                    final_response = parsed["response"]
                    termination = "final"
                    break
                if tool_calls >= max_tool_calls:
                    termination = "tool_limit"
                    break
                tool_calls += 1
                try:
                    if parsed["arguments"].get("order_id") != episode["order_id"]:
                        raise PermissionError("Tools are scoped to this episode's order")
                    with connection:
                        result = call_customer_tool(
                            connection, parsed["tool_name"], parsed["arguments"],
                            request_date=episode["request_date"],
                        )
                    trusted_arguments = dict(parsed["arguments"])
                    if parsed["tool_name"] == "request_return":
                        trusted_arguments["request_date"] = episode["request_date"]
                    entry["observation"] = {"tool_name": parsed["tool_name"], "result": result, "error": None}
                    entry["trusted_arguments"] = trusted_arguments
                except Exception as error:
                    connection.rollback()
                    entry["observation"] = {"tool_name": parsed["tool_name"], "result": None,
                                             "error": {"type": type(error).__name__, "message": str(error)}}
                transcript.append({"role": "user", "content": "Tool observation: " + canonical(entry["observation"])})
        # Observe committed state through a new connection, independently of tool codes.
        with closing(sqlite3.connect(database)) as observed:
            observed.row_factory = sqlite3.Row
            final_state = _snapshot(observed)
            events = [dict(row) for row in observed.execute(
                "SELECT * FROM action_events WHERE event_id > ? ORDER BY event_id", (episode_start,)
            )]
    return {"termination": termination, "final_response": final_response,
            "episode_start_event_id": episode_start, "initial_state": initial_state,
            "final_state": final_state, "action_events": events, "turns": turns}
