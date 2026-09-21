"""Read-only payment calculations for store tools."""

import sqlite3
from contextlib import closing
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "store.sqlite"

MONETARY_PAYMENT_TYPES = {"credit_card", "debit_card", "boleto"}


def _payment_value_to_cents(value: str | None, order_id: str) -> int:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"Order {order_id} has a payment with no payment_value")

    try:
        with localcontext() as context:
            context.prec = max(28, len(value) + 2)
            cents = Decimal(value) * 100
    except (DecimalException, ValueError) as error:
        raise ValueError(
            f"Order {order_id} has an invalid payment_value: {value!r}"
        ) from error

    if not cents.is_finite():
        raise ValueError(f"Order {order_id} has an invalid payment_value: {value!r}")
    if cents < 0:
        raise ValueError(
            f"Order {order_id} has a negative payment_value: {value!r}; "
            "refund amounts cannot be negative"
        )
    if cents != cents.to_integral_value():
        raise ValueError(
            f"Order {order_id} has a payment_value that is not an exact cent amount: {value!r}"
        )
    return int(cents)


def calculate_refund_breakdown(
    order_id: str, connection: sqlite3.Connection | None = None
) -> dict[str, int]:
    """Return the monetary and voucher refund amounts for an order in cents.

    Supply the action's connection to read its current transaction. Otherwise a
    connection to the default working database is opened read-only, preserving
    the original one-argument interface. Missing payment records, missing
    or unsupported payment types, and invalid amounts raise ``ValueError`` so
    callers can route the request for manual review instead of assuming zero.
    """
    if not isinstance(order_id, str) or not order_id.strip():
        raise ValueError("order_id must be a non-empty string")

    if connection is None:
        database_uri = f"{DATABASE.as_uri()}?mode=ro"
        with closing(sqlite3.connect(database_uri, uri=True)) as working_connection:
            return calculate_refund_breakdown(order_id, working_connection)

    rows = connection.execute(
        """
        SELECT payment_type, payment_value
        FROM order_payments
        WHERE order_id = ?
        """,
        (order_id,),
    ).fetchall()

    if not rows:
        raise ValueError(f"Order {order_id} has no payment records")

    cash_cents = 0
    voucher_cents = 0
    for payment_type, payment_value in rows:
        if not isinstance(payment_type, str) or not payment_type.strip():
            raise ValueError(f"Order {order_id} has a payment with no payment_type")

        payment_type = payment_type.strip()
        if payment_type == "not_defined":
            raise ValueError(
                f"Order {order_id} has a not_defined payment type; manual review is required"
            )

        amount_cents = _payment_value_to_cents(payment_value, order_id)
        if payment_type in MONETARY_PAYMENT_TYPES:
            cash_cents += amount_cents
        elif payment_type == "voucher":
            voucher_cents += amount_cents
        else:
            raise ValueError(
                f"Order {order_id} has an unrecognized payment_type: {payment_type!r}"
            )

    return {
        "cash_cents": cash_cents,
        "voucher_cents": voucher_cents,
        "total_cents": cash_cents + voucher_cents,
    }
