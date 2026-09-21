import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from store import payments


EXAMPLE_ORDER_ID = "fa65dad1b0e818e3ccc5cb0e39231352"
ACTUAL_29_VOUCHER_VALUES = (
    "66.02", "29.16", "3.71", "1.08", "12.86", "8.51", "3.68", "0.66",
    "5.02", "4.03", "0.00", "19.26", "29.05", "14.04", "2.95", "26.02",
    "3.60", "4.11", "5.02", "0.00", "12.86", "28.27", "3.72", "0.73",
    "0.32", "18.86", "150.00", "0.42", "4.03",
)


class CalculateRefundBreakdownTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.database = Path(temporary_directory.name) / "payments.sqlite"

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                CREATE TABLE order_payments (
                    order_id TEXT,
                    payment_type TEXT,
                    payment_value TEXT
                )
                """
            )
            connection.commit()

        database_patch = patch.object(payments, "DATABASE", self.database)
        database_patch.start()
        self.addCleanup(database_patch.stop)

    def add_payment(self, payment_type: str | None, value: str | None) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                """
                INSERT INTO order_payments (order_id, payment_type, payment_value)
                VALUES (?, ?, ?)
                """,
                (EXAMPLE_ORDER_ID, payment_type, value),
            )
            connection.commit()

    def test_voucher_order_returns_45799_cents(self) -> None:
        self.add_payment("voucher", "457.99")

        self.assertEqual(
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID),
            {"cash_cents": 0, "voucher_cents": 45799, "total_cents": 45799},
        )

    def test_actual_29_voucher_records_aggregate_to_45799_cents(self) -> None:
        self.assertEqual(len(ACTUAL_29_VOUCHER_VALUES), 29)
        for value in ACTUAL_29_VOUCHER_VALUES:
            self.add_payment("voucher", value)

        self.assertEqual(
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID),
            {"cash_cents": 0, "voucher_cents": 45799, "total_cents": 45799},
        )

    def test_mixed_card_and_voucher_payments_are_split(self) -> None:
        self.add_payment("credit_card", "123.45")
        self.add_payment("voucher", "23.99")

        self.assertEqual(
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID),
            {"cash_cents": 12345, "voucher_cents": 2399, "total_cents": 14744},
        )

    def test_zero_value_payment_is_accepted(self) -> None:
        self.add_payment("voucher", "0.00")

        self.assertEqual(
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID),
            {"cash_cents": 0, "voucher_cents": 0, "total_cents": 0},
        )

    def test_negative_payment_is_rejected(self) -> None:
        self.add_payment("credit_card", "-10.00")

        with self.assertRaisesRegex(ValueError, "negative payment_value"):
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID)

    def test_unsupported_payment_type_is_rejected_explicitly(self) -> None:
        self.add_payment("pix", "10.00")

        with self.assertRaisesRegex(ValueError, "unrecognized payment_type.*pix"):
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID)

    def test_not_defined_payment_type_requires_manual_review(self) -> None:
        self.add_payment("not_defined", "10.00")

        with self.assertRaisesRegex(ValueError, "not_defined.*manual review"):
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID)

    def test_missing_payment_records_are_rejected_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "no payment records"):
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID)

    def test_extra_decimal_places_are_allowed_only_when_exact_cents(self) -> None:
        self.add_payment("voucher", "1.230")
        self.assertEqual(
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID),
            {"cash_cents": 0, "voucher_cents": 123, "total_cents": 123},
        )

        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM order_payments")
            connection.commit()
        self.add_payment("voucher", "1.231")

        with self.assertRaisesRegex(ValueError, "not an exact cent amount"):
            payments.calculate_refund_breakdown(EXAMPLE_ORDER_ID)


if __name__ == "__main__":
    unittest.main()
