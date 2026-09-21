"""Minimal final-state checks for store actions."""


def verify_cancellation(
    before_status: str,
    after_status: str,
    expected_status: str,
) -> bool:
    """Check whether the final order status matches the expected status.

    ``before_status`` is accepted for the verifier interface, but this initial
    version intentionally does not use it: matching final state does not prove
    that the cancellation action was called.
    """
    return after_status == expected_status
