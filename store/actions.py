"""State-changing store actions governed by the store policy."""

import sqlite3


CANCELLABLE_STATUSES = {"created", "approved", "invoiced", "processing"}


def _record_action(
    connection: sqlite3.Connection, order_id: str, result: str
) -> str:
    connection.execute(
        """
        INSERT INTO action_events (tool_name, order_id, result)
        VALUES (?, ?, ?)
        """,
        ("cancel_order", order_id, result),
    )
    return result


def cancel_order(connection: sqlite3.Connection, order_id: str) -> str:
    """Cancel an eligible order and return its policy result code.

    The caller supplies the connection so it controls the database and
    transaction. The working database must be initialized with
    ``store.schema.initialize_schema``. Each result is recorded in
    ``action_events`` in the same transaction as any order status change.
    """
    if not isinstance(order_id, str) or not order_id.strip():
        return _record_action(connection, str(order_id), "INVALID_REQUEST")

    row = connection.execute(
        "SELECT order_status FROM orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        return _record_action(connection, order_id, "ORDER_NOT_FOUND")

    current_status = row[0]
    if current_status in CANCELLABLE_STATUSES:
        connection.execute(
            "UPDATE orders SET order_status = ? WHERE order_id = ?",
            ("canceled", order_id),
        )
        return _record_action(connection, order_id, "CANCELED")
    if current_status == "canceled":
        return _record_action(connection, order_id, "ALREADY_CANCELED")
    if current_status == "unavailable":
        return _record_action(connection, order_id, "ESCALATION_REQUIRED")
    if current_status in {"shipped", "delivered"}:
        return _record_action(connection, order_id, "DENIED")
    return _record_action(connection, order_id, "UNSUPPORTED_STATUS")
