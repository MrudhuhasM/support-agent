import sqlite3
import tempfile
import unittest
from pathlib import Path

from store.schema import initialize_schema
from store.trusted import approve_return


class ApproveReturnTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database_path = Path(temporary_directory.name) / "store.sqlite"
        self.connection = sqlite3.connect(database_path)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.connection.execute(
            "INSERT INTO orders (order_id, order_status) VALUES (?, ?)",
            ("order-1", "delivered"),
        )
        self.connection.commit()

    def add_return(self, status: str) -> None:
        self.connection.execute(
            """
            INSERT INTO order_returns (order_id, return_status, return_request_date)
            VALUES (?, ?, ?)
            """,
            ("order-1", status, "2026-10-16"),
        )
        self.connection.commit()

    def test_created_return_is_approved_without_changing_order(self) -> None:
        self.add_return("created")

        result = approve_return(self.connection, "order-1")

        self.assertEqual(result, "RETURN_APPROVED")
        self.assertEqual(
            self.connection.execute(
                "SELECT return_status FROM order_returns WHERE order_id = ?",
                ("order-1",),
            ).fetchone(),
            ("approved",),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT order_status FROM orders WHERE order_id = ?",
                ("order-1",),
            ).fetchone(),
            ("delivered",),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT tool_name, order_id, result FROM action_events"
            ).fetchone(),
            ("approve_return", "order-1", "RETURN_APPROVED"),
        )

    def test_approved_return_is_already_approved(self) -> None:
        self.add_return("approved")

        result = approve_return(self.connection, "order-1")

        self.assertEqual(result, "RETURN_ALREADY_APPROVED")
        self.assertEqual(
            self.connection.execute(
                "SELECT return_status FROM order_returns WHERE order_id = ?",
                ("order-1",),
            ).fetchone(),
            ("approved",),
        )

    def test_missing_return_is_not_found(self) -> None:
        result = approve_return(self.connection, "order-1")

        self.assertEqual(result, "RETURN_NOT_FOUND")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM order_returns").fetchone()[0],
            0,
        )

    def test_unrecognized_return_status_is_denied(self) -> None:
        self.add_return("pending")

        result = approve_return(self.connection, "order-1")

        self.assertEqual(result, "RETURN_APPROVAL_DENIED")
        self.assertEqual(
            self.connection.execute(
                "SELECT return_status FROM order_returns WHERE order_id = ?",
                ("order-1",),
            ).fetchone(),
            ("pending",),
        )

    def test_rollback_reverts_return_approval_and_action_event(self) -> None:
        self.add_return("created")

        result = approve_return(self.connection, "order-1")

        self.assertEqual(result, "RETURN_APPROVED")
        self.assertEqual(
            self.connection.execute(
                "SELECT return_status FROM order_returns WHERE order_id = ?",
                ("order-1",),
            ).fetchone(),
            ("approved",),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM action_events").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT order_status FROM orders WHERE order_id = ?",
                ("order-1",),
            ).fetchone(),
            ("delivered",),
        )

        self.connection.rollback()

        self.assertEqual(
            self.connection.execute(
                "SELECT return_status FROM order_returns WHERE order_id = ?",
                ("order-1",),
            ).fetchone(),
            ("created",),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM action_events").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT order_status FROM orders WHERE order_id = ?",
                ("order-1",),
            ).fetchone(),
            ("delivered",),
        )


if __name__ == "__main__":
    unittest.main()
