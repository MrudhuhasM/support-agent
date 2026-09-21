import sqlite3
import tempfile
import unittest
from pathlib import Path

from store.actions import request_return
from store.schema import initialize_schema


class RequestReturnTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        database_path = Path(temporary_directory.name) / "store.sqlite"
        self.connection = sqlite3.connect(database_path)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.connection.commit()

    def add_order(
        self,
        order_id: str,
        status: str = "delivered",
        delivery_date: str | None = "2026-10-01",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO orders (
                order_id, order_status, order_delivered_customer_date
            ) VALUES (?, ?, ?)
            """,
            (order_id, status, delivery_date),
        )
        self.connection.commit()

    def test_inclusive_thirty_day_return_window(self) -> None:
        scenarios = (
            ("day-15", "2026-10-16", "RETURN_CREATED"),
            ("day-30", "2026-10-31", "RETURN_CREATED"),
            ("day-31", "2026-11-01", "RETURN_DENIED_WINDOW"),
        )

        for order_id, request_date, expected_result in scenarios:
            with self.subTest(request_date=request_date):
                self.add_order(order_id)

                result = request_return(self.connection, order_id, request_date)

                self.assertEqual(result, expected_result)
                order = self.connection.execute(
                    "SELECT order_status FROM orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                self.assertEqual(order, ("delivered",))
                return_row = self.connection.execute(
                    """
                    SELECT return_status, return_request_date
                    FROM order_returns WHERE order_id = ?
                    """,
                    (order_id,),
                ).fetchone()
                if expected_result == "RETURN_CREATED":
                    self.assertEqual(return_row, ("created", request_date))
                else:
                    self.assertIsNone(return_row)
                event = self.connection.execute(
                    """
                    SELECT tool_name, order_id, result
                    FROM action_events WHERE order_id = ?
                    ORDER BY event_id DESC LIMIT 1
                    """,
                    (order_id,),
                ).fetchone()
                self.assertEqual(event, ("request_return", order_id, expected_result))
                self.connection.commit()

    def test_shipped_order_is_denied(self) -> None:
        self.add_order("shipped-order", status="shipped")

        result = request_return(self.connection, "shipped-order", "2026-10-16")

        self.assertEqual(result, "RETURN_DENIED_NOT_DELIVERED")
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM order_returns WHERE order_id = ?", ("shipped-order",)
            ).fetchone()
        )

    def test_duplicate_request_preserves_original_return(self) -> None:
        self.add_order("duplicate-order")
        first_result = request_return(
            self.connection, "duplicate-order", "2026-10-16"
        )
        self.connection.commit()

        second_result = request_return(
            self.connection, "duplicate-order", "2026-10-20"
        )

        self.assertEqual(first_result, "RETURN_CREATED")
        self.assertEqual(second_result, "RETURN_ALREADY_REQUESTED")
        self.assertEqual(
            self.connection.execute(
                """
                SELECT return_status, return_request_date
                FROM order_returns WHERE order_id = ?
                """,
                ("duplicate-order",),
            ).fetchone(),
            ("created", "2026-10-16"),
        )

    def test_invalid_delivery_date_is_denied(self) -> None:
        self.add_order("invalid-date-order", delivery_date="not-a-date")

        result = request_return(
            self.connection, "invalid-date-order", "2026-10-16"
        )

        self.assertEqual(result, "RETURN_DENIED_INVALID_DATE")
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM order_returns WHERE order_id = ?",
                ("invalid-date-order",),
            ).fetchone()
        )

    def test_rollback_removes_return_and_action_event(self) -> None:
        self.add_order("rollback-order")

        result = request_return(self.connection, "rollback-order", "2026-10-16")

        self.assertEqual(result, "RETURN_CREATED")
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute(
                "SELECT order_status FROM orders WHERE order_id = ?",
                ("rollback-order",),
            ).fetchone(),
            ("delivered",),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM order_returns WHERE order_id = ?",
                ("rollback-order",),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM action_events
                WHERE tool_name = 'request_return' AND order_id = ?
                """,
                ("rollback-order",),
            ).fetchone()[0],
            1,
        )

        self.connection.rollback()

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM order_returns WHERE order_id = ?",
                ("rollback-order",),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM action_events
                WHERE tool_name = 'request_return' AND order_id = ?
                """,
                ("rollback-order",),
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
