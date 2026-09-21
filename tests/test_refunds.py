import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from store.actions import authorize_refund, request_return
from store.payments import calculate_refund_breakdown
from store.schema import initialize_schema
from store.tools import CUSTOMER_TOOLS, call_customer_tool
from store.trusted import approve_return


class AuthorizeRefundTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "store.sqlite"
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)
        initialize_schema(self.connection)
        self.connection.execute(
            """INSERT INTO orders
               (order_id, order_status, order_delivered_customer_date)
               VALUES ('order-1', 'delivered', '2026-10-01')"""
        )
        self.connection.commit()

    def add_return(self, status: str = "approved") -> None:
        self.connection.execute(
            "INSERT INTO order_returns VALUES (?, ?, ?)",
            ("order-1", status, "2026-10-16"),
        )
        self.connection.commit()

    def add_payment(self, kind: str | None, amount: str | None) -> None:
        self.connection.execute(
            "INSERT INTO order_payments VALUES (?, ?, ?)", ("order-1", kind, amount)
        )

    def test_approval_then_authorization_aggregates_current_transaction_payments(self) -> None:
        self.assertEqual(request_return(self.connection, "order-1", "2026-10-16"), "RETURN_CREATED")
        self.assertEqual(approve_return(self.connection, "order-1"), "RETURN_APPROVED")
        for kind, value in (
            ("credit_card", "12.34"), ("debit_card", "0.10"),
            ("boleto", "2.01"), ("voucher", "3.45"), ("voucher", "0.05"),
        ):
            self.add_payment(kind, value)
        before_payments = self.connection.execute("SELECT * FROM order_payments").fetchall()
        before_order = self.connection.execute("SELECT * FROM orders").fetchall()
        before_return = self.connection.execute("SELECT * FROM order_returns").fetchall()
        self.assertEqual(calculate_refund_breakdown("order-1", self.connection),
                         {"cash_cents": 1445, "voucher_cents": 350, "total_cents": 1795})

        self.assertEqual(authorize_refund(self.connection, "order-1"), "REFUND_AUTHORIZED")
        self.connection.commit()

        with closing(sqlite3.connect(self.database)) as observed:
            self.assertEqual(observed.execute("SELECT * FROM refund_authorizations").fetchall(),
                             [("order-1", "authorized", 1445, 350)])
            self.assertEqual(observed.execute("SELECT * FROM orders").fetchall(), before_order)
            self.assertEqual(observed.execute("SELECT * FROM order_returns").fetchall(), before_return)
            self.assertEqual(observed.execute("SELECT * FROM order_payments").fetchall(), before_payments)
            self.assertEqual(observed.execute(
                "SELECT tool_name, order_id, result FROM action_events ORDER BY event_id DESC LIMIT 1"
            ).fetchone(), ("authorize_refund", "order-1", "REFUND_AUTHORIZED"))

    def test_duplicate_takes_priority_over_changed_return_and_payment_state(self) -> None:
        self.add_return()
        self.add_payment("voucher", "457.99")
        self.assertEqual(authorize_refund(self.connection, "order-1"), "REFUND_AUTHORIZED")
        self.connection.commit()
        self.connection.execute("DELETE FROM order_returns")
        self.connection.execute("DELETE FROM order_payments")

        self.assertEqual(authorize_refund(self.connection, "order-1"), "REFUND_ALREADY_EXISTS")
        self.assertEqual(self.connection.execute("SELECT * FROM refund_authorizations").fetchall(),
                         [("order-1", "authorized", 0, 45799)])

    def test_denial_without_approved_return(self) -> None:
        self.assertEqual(authorize_refund(self.connection, "order-1"), "REFUND_DENIED_NO_RETURN")
        self.add_return("created")
        self.assertEqual(authorize_refund(self.connection, "order-1"), "REFUND_DENIED_RETURN_NOT_APPROVED")
        self.connection.execute("UPDATE order_returns SET return_status = 'unknown'")
        self.assertEqual(authorize_refund(self.connection, "order-1"), "REFUND_DENIED_RETURN_NOT_APPROVED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM refund_authorizations").fetchone()[0], 0)

    def test_invalid_or_missing_payments_require_manual_review(self) -> None:
        self.add_return()
        cases = (
            None, (None, "1.00"), ("", "1.00"), ("not_defined", "1.00"),
            ("crypto", "1.00"), ("voucher", None), ("credit_card", "bad"),
            ("boleto", "-1.00"), ("voucher", "NaN"), ("voucher", "Infinity"),
            ("voucher", "0.001"), ("voucher", "1e100"),
        )
        for payment in cases:
            with self.subTest(payment=payment):
                self.connection.execute("DELETE FROM order_payments")
                if payment is not None:
                    self.add_payment("credit_card", "1.00")
                    self.add_payment(*payment)
                self.assertEqual(authorize_refund(self.connection, "order-1"), "REFUND_MANUAL_REVIEW_REQUIRED")
                self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM refund_authorizations").fetchone()[0], 0)

    def test_invalid_and_unknown_identifiers(self) -> None:
        for order_id in (None, "", "   ", 123):
            with self.subTest(order_id=order_id):
                self.assertEqual(authorize_refund(self.connection, order_id), "INVALID_REQUEST")
                self.assertEqual(approve_return(self.connection, order_id), "INVALID_REQUEST")
        self.assertEqual(authorize_refund(self.connection, "missing"), "ORDER_NOT_FOUND")
        self.assertEqual(approve_return(self.connection, "missing"), "ORDER_NOT_FOUND")

    def test_rollback_removes_authorization_and_its_event(self) -> None:
        self.add_return()
        self.add_payment("credit_card", "25.00")
        self.connection.commit()
        self.assertEqual(authorize_refund(self.connection, "order-1"), "REFUND_AUTHORIZED")
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM action_events").fetchone()[0], 1)

        self.connection.rollback()

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM refund_authorizations").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM action_events").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT return_status FROM order_returns").fetchone(), ("approved",))
        self.assertEqual(self.connection.execute("SELECT payment_value FROM order_payments").fetchone(), ("25.00",))

    def test_customer_tools_cannot_approve_returns(self) -> None:
        self.assertNotIn("approve_return", CUSTOMER_TOOLS)
        with self.assertRaisesRegex(ValueError, "not available"):
            call_customer_tool(self.connection, "approve_return", {"order_id": "order-1"})
        self.assertEqual(call_customer_tool(self.connection, "request_return", {
            "order_id": "order-1", "request_date": "2026-10-16"
        }), "RETURN_CREATED")
        self.add_payment("credit_card", "10.00")
        self.assertEqual(call_customer_tool(self.connection, "authorize_refund", {
            "order_id": "order-1"
        }), "REFUND_DENIED_RETURN_NOT_APPROVED")
        self.assertEqual(self.connection.execute("SELECT return_status FROM order_returns").fetchone(), ("created",))


if __name__ == "__main__":
    unittest.main()
