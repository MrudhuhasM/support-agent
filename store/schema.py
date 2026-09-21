"""Tables required by the working store environment."""

import sqlite3


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create missing tables without changing existing data or committing.

    A loaded Olist database retains its full orders schema. An empty database
    receives the minimal orders table needed by the cancellation tool.
    """
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            order_status TEXT,
            order_estimated_delivery_date TEXT,
            order_delivered_customer_date TEXT
        )
        """
    )
    order_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(orders)")
    }
    for column in (
        "order_estimated_delivery_date",
        "order_delivered_customer_date",
    ):
        if column not in order_columns:
            connection.execute(f"ALTER TABLE orders ADD COLUMN {column} TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS action_events (
            event_id INTEGER PRIMARY KEY,
            tool_name TEXT NOT NULL,
            order_id TEXT NOT NULL,
            result TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS order_returns (
            order_id TEXT PRIMARY KEY,
            return_status TEXT NOT NULL,
            return_request_date TEXT NOT NULL
        )
        """
    )
