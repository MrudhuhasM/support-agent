"""Read-only store lookup tools."""

import sqlite3


def get_order(connection: sqlite3.Connection, order_id: str) -> dict[str, str | None] | None:
    """Return the requested Olist order fields, or ``None`` if it is missing."""
    row = connection.execute(
        """
        SELECT
            order_id,
            order_status,
            order_estimated_delivery_date,
            order_delivered_customer_date
        FROM orders
        WHERE order_id = ?
        """,
        (order_id,),
    ).fetchone()
    if row is None:
        return None

    return {
        "order_id": row[0],
        "order_status": row[1],
        "order_estimated_delivery_date": row[2],
        "order_delivered_customer_date": row[3],
    }
