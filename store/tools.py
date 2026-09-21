"""Explicit tool boundary for the customer-facing agent.

The host supplies a connection and dispatches JSON arguments here. Never give
the model arbitrary Python/SQL execution or discover tools by module inspection.
Trusted return approval is intentionally absent from this registry.
"""

import sqlite3

from store.actions import authorize_refund, cancel_order, request_return
from store.lookup import get_order


CUSTOMER_TOOLS = {
    "get_order": get_order,
    "cancel_order": cancel_order,
    "request_return": request_return,
    "authorize_refund": authorize_refund,
}


def call_customer_tool(
    connection: sqlite3.Connection, tool_name: str, arguments: dict
) -> dict | str | None:
    """Dispatch only an explicitly allowed customer tool."""
    if tool_name not in CUSTOMER_TOOLS:
        raise ValueError(f"Tool is not available to the customer agent: {tool_name}")
    return CUSTOMER_TOOLS[tool_name](connection, **arguments)
