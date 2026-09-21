"""Privileged environment operations; do not expose as agent-callable tools."""

import sqlite3


def _record_action(
    connection: sqlite3.Connection, order_id: str, result: str
) -> str:
    connection.execute(
        """
        INSERT INTO action_events (tool_name, order_id, result)
        VALUES (?, ?, ?)
        """,
        ("approve_return", order_id, result),
    )
    return result


def approve_return(connection: sqlite3.Connection, order_id: str) -> str:
    """Approve a return as a trusted environment operation.

    This operation must not be registered as a customer-facing agent tool.
    It changes only the return status and records its result in the caller's
    transaction.
    """
    if not isinstance(order_id, str) or not order_id.strip():
        return _record_action(connection, str(order_id), "INVALID_REQUEST")

    order = connection.execute(
        "SELECT 1 FROM orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if order is None:
        return _record_action(connection, order_id, "ORDER_NOT_FOUND")

    return_row = connection.execute(
        "SELECT return_status FROM order_returns WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if return_row is None:
        return _record_action(connection, order_id, "RETURN_NOT_FOUND")

    return_status = return_row[0]
    if return_status == "created":
        connection.execute(
            "UPDATE order_returns SET return_status = ? WHERE order_id = ?",
            ("approved", order_id),
        )
        return _record_action(connection, order_id, "RETURN_APPROVED")
    if return_status == "approved":
        return _record_action(connection, order_id, "RETURN_ALREADY_APPROVED")
    return _record_action(connection, order_id, "RETURN_APPROVAL_DENIED")
