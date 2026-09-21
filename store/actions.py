"""State-changing store actions governed by the store policy."""

import sqlite3
from datetime import date, datetime, timedelta, timezone


CANCELLABLE_STATUSES = {"created", "approved", "invoiced", "processing"}


def _record_action(
    connection: sqlite3.Connection,
    order_id: str,
    result: str,
    tool_name: str = "cancel_order",
) -> str:
    connection.execute(
        """
        INSERT INTO action_events (tool_name, order_id, result)
        VALUES (?, ?, ?)
        """,
        (tool_name, order_id, result),
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


def _parse_utc_date(value: str) -> date | None:
    """Parse an ISO date or timestamp and return its UTC calendar date."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def request_return(
    connection: sqlite3.Connection,
    order_id: str,
    request_date: str,
) -> str:
    """Create an eligible return request without changing order status.

    The caller controls the transaction. The return row and trusted action
    event are written in that same transaction.
    """
    tool_name = "request_return"
    if not isinstance(order_id, str) or not order_id.strip():
        return _record_action(connection, str(order_id), "INVALID_REQUEST", tool_name)

    normalized_request_date = _parse_utc_date(request_date)
    if normalized_request_date is None:
        return _record_action(connection, order_id, "INVALID_REQUEST", tool_name)

    order = connection.execute(
        """
        SELECT order_status, order_delivered_customer_date
        FROM orders
        WHERE order_id = ?
        """,
        (order_id,),
    ).fetchone()
    if order is None:
        return _record_action(connection, order_id, "ORDER_NOT_FOUND", tool_name)

    existing_return = connection.execute(
        "SELECT 1 FROM order_returns WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    if existing_return is not None:
        return _record_action(
            connection, order_id, "RETURN_ALREADY_REQUESTED", tool_name
        )

    order_status, delivery_value = order
    if order_status != "delivered":
        return _record_action(
            connection, order_id, "RETURN_DENIED_NOT_DELIVERED", tool_name
        )
    if delivery_value is None or not delivery_value.strip():
        return _record_action(
            connection, order_id, "RETURN_DENIED_MISSING_DATE", tool_name
        )

    delivery_date = _parse_utc_date(delivery_value)
    if delivery_date is None or normalized_request_date < delivery_date:
        return _record_action(
            connection, order_id, "RETURN_DENIED_INVALID_DATE", tool_name
        )
    if normalized_request_date > delivery_date + timedelta(days=30):
        return _record_action(
            connection, order_id, "RETURN_DENIED_WINDOW", tool_name
        )

    connection.execute(
        """
        INSERT INTO order_returns (order_id, return_status, return_request_date)
        VALUES (?, ?, ?)
        """,
        (order_id, "created", request_date),
    )
    return _record_action(connection, order_id, "RETURN_CREATED", tool_name)
