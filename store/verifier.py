"""Minimal final-state checks for store actions."""

from collections.abc import Sequence


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


def verify_cancellation_episode(
    initial_status: str,
    final_status: str,
    expected_final_status: str,
    expected_result_code: str,
    order_id: str,
    action_events: Sequence[tuple[str, str, str]],
) -> bool:
    """Check the requested final state and the episode's complete action log.

    ``action_events`` must contain only records created in this episode, for
    example by querying ``action_events`` with ``event_id > episode_start_id``.
    Exactly one event is required, and it must record the expected cancellation
    result for this order. The initial status is part of the verifier interface
    so the caller supplies the full episode state alongside its outcome.
    """
    return (
        final_status == expected_final_status
        and len(action_events) == 1
        and action_events[0]
        == ("cancel_order", order_id, expected_result_code)
    )
