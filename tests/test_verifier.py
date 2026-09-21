from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from store.actions import cancel_order
from store.schema import initialize_schema
from store.verifier import verify_cancellation


class VerifyCancellationTests(unittest.TestCase):
    def test_cancellation_is_verified_from_database_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "store.sqlite"
            with closing(sqlite3.connect(database_path)) as connection:
                initialize_schema(connection)
                order_id = "processing-order"
                connection.execute(
                    "INSERT INTO orders (order_id, order_status) VALUES (?, ?)",
                    (order_id, "processing"),
                )
                connection.commit()

                before_row = connection.execute(
                    "SELECT order_status FROM orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                self.assertIsNotNone(before_row)
                before_status = before_row[0]

                cancel_order(connection, order_id)
                connection.commit()

            with closing(sqlite3.connect(database_path)) as verification_connection:
                after_row = verification_connection.execute(
                    "SELECT order_status FROM orders WHERE order_id = ?",
                    (order_id,),
                ).fetchone()
                self.assertIsNotNone(after_row)
                after_status = after_row[0]
                events = verification_connection.execute(
                    "SELECT tool_name, order_id, result FROM action_events"
                ).fetchall()
                self.assertEqual(events, [("cancel_order", order_id, "CANCELED")])

                initialize_schema(verification_connection)
                verification_connection.commit()
                self.assertEqual(
                    verification_connection.execute(
                        "SELECT tool_name, order_id, result FROM action_events"
                    ).fetchall(),
                    events,
                )

        self.assertEqual(before_status, "processing")
        self.assertEqual(after_status, "canceled")
        self.assertTrue(verify_cancellation(before_status, after_status, "canceled"))

    def test_final_status_examples(self) -> None:
        cases = (
            ("processing", "canceled", "canceled", True),
            ("processing", "processing", "canceled", False),
            ("shipped", "shipped", "shipped", True),
            ("shipped", "canceled", "shipped", False),
        )

        for before, after, expected, result in cases:
            with self.subTest(before=before, after=after, expected=expected):
                self.assertEqual(
                    verify_cancellation(before, after, expected),
                    result,
                )


if __name__ == "__main__":
    unittest.main()
