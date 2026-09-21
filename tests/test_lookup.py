import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from store.lookup import get_order
from store.schema import initialize_schema


class GetOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database_path = Path(temporary_directory.name) / "store.sqlite"
        self.connection = sqlite3.connect(database_path)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.connection.execute(
            """
            INSERT INTO orders (
                order_id,
                order_status,
                order_estimated_delivery_date,
                order_delivered_customer_date
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "existing-order",
                "processing",
                "2018-01-20 00:00:00",
                None,
            ),
        )
        self.connection.commit()

    def test_returns_requested_olist_order_fields(self) -> None:
        self.assertEqual(
            get_order(self.connection, "existing-order"),
            {
                "order_id": "existing-order",
                "order_status": "processing",
                "order_estimated_delivery_date": "2018-01-20 00:00:00",
                "order_delivered_customer_date": None,
            },
        )

    def test_returns_none_for_nonexistent_order(self) -> None:
        self.assertIsNone(get_order(self.connection, "missing-order"))

    def test_lookup_does_not_modify_order_state(self) -> None:
        before_changes = self.connection.total_changes
        before_order = self.connection.execute(
            """
            SELECT order_status, order_estimated_delivery_date,
                   order_delivered_customer_date
            FROM orders WHERE order_id = ?
            """,
            ("existing-order",),
        ).fetchone()
        before_events = self.connection.execute(
            "SELECT COUNT(*) FROM action_events"
        ).fetchone()[0]

        get_order(self.connection, "existing-order")

        after_order = self.connection.execute(
            """
            SELECT order_status, order_estimated_delivery_date,
                   order_delivered_customer_date
            FROM orders WHERE order_id = ?
            """,
            ("existing-order",),
        ).fetchone()
        after_events = self.connection.execute(
            "SELECT COUNT(*) FROM action_events"
        ).fetchone()[0]
        self.assertEqual(after_order, before_order)
        self.assertEqual(after_events, before_events)
        self.assertEqual(self.connection.total_changes, before_changes)


if __name__ == "__main__":
    unittest.main()
