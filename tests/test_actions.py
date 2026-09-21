import sqlite3
import tempfile
import unittest
from pathlib import Path

from store.actions import cancel_order


class CancelOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database_path = Path(temporary_directory.name) / "store.sqlite"
        self.connection = sqlite3.connect(database_path)
        self.addCleanup(self.connection.close)
        self.connection.execute(
            """
            CREATE TABLE orders (
                order_id TEXT PRIMARY KEY,
                order_status TEXT
            )
            """
        )
        self.connection.executemany(
            "INSERT INTO orders (order_id, order_status) VALUES (?, ?)",
            [
                ("processing-order", "processing"),
                ("shipped-order", "shipped"),
                ("delivered-order", "delivered"),
                ("unavailable-order", "unavailable"),
                ("unknown-status-order", "on_hold"),
                ("canceled-order", "canceled"),
            ],
        )
        self.connection.commit()

    def order_status(self, order_id: str) -> str:
        row = self.connection.execute(
            "SELECT order_status FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return row[0]

    def test_successful_cancellation_changes_processing_order(self) -> None:
        result = cancel_order(self.connection, "processing-order")

        self.assertEqual(result, "CANCELED")
        self.assertEqual(self.order_status("processing-order"), "canceled")

    def test_denied_cancellation_leaves_shipped_order_unchanged(self) -> None:
        result = cancel_order(self.connection, "shipped-order")

        self.assertEqual(result, "DENIED")
        self.assertEqual(self.order_status("shipped-order"), "shipped")

    def test_nonexistent_order_is_reported(self) -> None:
        result = cancel_order(self.connection, "missing-order")

        self.assertEqual(result, "ORDER_NOT_FOUND")

    def test_repeated_cancellation_returns_already_canceled(self) -> None:
        first_result = cancel_order(self.connection, "processing-order")
        second_result = cancel_order(self.connection, "processing-order")

        self.assertEqual(first_result, "CANCELED")
        self.assertEqual(second_result, "ALREADY_CANCELED")
        self.assertEqual(self.order_status("processing-order"), "canceled")

    def test_missing_or_empty_order_id_is_invalid(self) -> None:
        self.assertEqual(cancel_order(self.connection, ""), "INVALID_REQUEST")
        self.assertEqual(cancel_order(self.connection, "   "), "INVALID_REQUEST")

    def test_delivered_order_is_denied_without_a_state_change(self) -> None:
        result = cancel_order(self.connection, "delivered-order")

        self.assertEqual(result, "DENIED")
        self.assertEqual(self.order_status("delivered-order"), "delivered")

    def test_unavailable_order_requires_escalation(self) -> None:
        result = cancel_order(self.connection, "unavailable-order")

        self.assertEqual(result, "ESCALATION_REQUIRED")
        self.assertEqual(self.order_status("unavailable-order"), "unavailable")

    def test_unrecognized_order_status_is_unsupported(self) -> None:
        result = cancel_order(self.connection, "unknown-status-order")

        self.assertEqual(result, "UNSUPPORTED_STATUS")
        self.assertEqual(self.order_status("unknown-status-order"), "on_hold")

    def test_already_canceled_order_is_not_updated(self) -> None:
        result = cancel_order(self.connection, "canceled-order")

        self.assertEqual(result, "ALREADY_CANCELED")
        self.assertEqual(self.order_status("canceled-order"), "canceled")


if __name__ == "__main__":
    unittest.main()
