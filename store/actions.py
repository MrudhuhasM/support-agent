"""State-changing store actions governed by the store policy."""

import sqlite3


CANCELLABLE_STATUSES = {"created", "approved", "invoiced", "processing"}


def cancel_order(connection: sqlite3.Connection, order_id: str) -> str:
    """Cancel an eligible order and return its policy result code.

    The caller supplies the connection so it controls the database and
    transaction. Only ``orders.order_status`` is changed on a successful
    cancellation.
    """
    if not isinstance(order_id, str) or not order_id.strip():
        return "INVALID_REQUEST"

    row = connection.execute(
        "SELECT order_status FROM orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if row is None:
        return "ORDER_NOT_FOUND"

    current_status = row[0]
    if current_status in CANCELLABLE_STATUSES:
        connection.execute(
            "UPDATE orders SET order_status = ? WHERE order_id = ?",
            ("canceled", order_id),
        )
        return "CANCELED"
    if current_status == "canceled":
        return "ALREADY_CANCELED"
    if current_status == "unavailable":
        return "ESCALATION_REQUIRED"
    if current_status in {"shipped", "delivered"}:
        return "DENIED"
    return "UNSUPPORTED_STATUS"
