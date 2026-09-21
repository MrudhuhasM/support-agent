from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from store.actions import cancel_order
from store.schema import initialize_schema
from store.verifier import verify_cancellation, verify_cancellation_episode


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

    def test_episode_verifier_requires_a_matching_current_action_event(self) -> None:
        cases = (
            ("processing", True, False, "canceled", "CANCELED", True),
            ("shipped", True, False, "shipped", "DENIED", True),
            ("shipped", False, False, "shipped", "DENIED", False),
            ("shipped", False, True, "shipped", "DENIED", False),
        )

        for (
            initial_status,
            invoke_tool,
            add_old_event,
            expected_status,
            expected_result,
            expected,
        ) in cases:
            with self.subTest(
                initial_status=initial_status,
                invoke_tool=invoke_tool,
                add_old_event=add_old_event,
            ), tempfile.TemporaryDirectory() as temporary_directory:
                database_path = Path(temporary_directory) / "store.sqlite"
                order_id = f"{initial_status}-order"
                with closing(sqlite3.connect(database_path)) as connection:
                    initialize_schema(connection)
                    connection.execute(
                        "INSERT INTO orders (order_id, order_status) VALUES (?, ?)",
                        (order_id, initial_status),
                    )
                    if add_old_event:
                        connection.execute(
                            """
                            INSERT INTO action_events (tool_name, order_id, result)
                            VALUES (?, ?, ?)
                            """,
                            ("cancel_order", order_id, "DENIED"),
                        )
                    connection.commit()
                    initial_row = connection.execute(
                        "SELECT order_status FROM orders WHERE order_id = ?",
                        (order_id,),
                    ).fetchone()
                    self.assertIsNotNone(initial_row)
                    observed_initial_status = initial_row[0]
                    episode_start_id = connection.execute(
                        "SELECT COALESCE(MAX(event_id), 0) FROM action_events"
                    ).fetchone()[0]

                    if invoke_tool:
                        cancel_order(connection, order_id)
                        connection.commit()

                with closing(sqlite3.connect(database_path)) as verification_connection:
                    final_row = verification_connection.execute(
                        "SELECT order_status FROM orders WHERE order_id = ?",
                        (order_id,),
                    ).fetchone()
                    self.assertIsNotNone(final_row)
                    final_status = final_row[0]
                    episode_events = verification_connection.execute(
                        """
                        SELECT tool_name, order_id, result
                        FROM action_events
                        WHERE event_id > ?
                        ORDER BY event_id
                        """,
                        (episode_start_id,),
                    ).fetchall()

                with self.subTest(expected=expected):
                    self.assertEqual(
                        verify_cancellation_episode(
                            observed_initial_status,
                            final_status,
                            expected_status,
                            expected_result,
                            order_id,
                            episode_events,
                        ),
                        expected,
                    )


if __name__ == "__main__":
    unittest.main()
