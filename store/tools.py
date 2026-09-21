"""Explicit tool boundary for the customer-facing agent.

The host supplies a connection and dispatches JSON arguments here. Never give
the model arbitrary Python/SQL execution or discover tools by module inspection.
Trusted return approval is intentionally absent from this registry.
"""

import sqlite3

from store.actions import authorize_refund, cancel_order, request_return
from store.actions import _parse_utc_date
from store.lookup import get_order


CUSTOMER_TOOLS = {
    "get_order": get_order,
    "cancel_order": cancel_order,
    "request_return": request_return,
    "authorize_refund": authorize_refund,
}

TOOL_SCHEMAS = [
    {"tool_name": name, "arguments": {"order_id": "string"}}
    for name in CUSTOMER_TOOLS
]


def call_customer_tool(
    connection: sqlite3.Connection, tool_name: str, arguments: dict,
    *, request_date: str | None = None,
) -> dict | str | None:
    """Dispatch model arguments; dates come exclusively from trusted host context.

    The raw request_return() function retains its trusted Python interface.
    Customer JSON must supply only order_id, including for return requests.
    """
    if not isinstance(tool_name, str) or tool_name not in CUSTOMER_TOOLS:
        raise ValueError(f"Tool is not available to the customer agent: {tool_name}")
    if not isinstance(arguments, dict) or set(arguments) != {"order_id"}:
        raise ValueError("Customer tool arguments must contain only order_id")
    if tool_name == "request_return":
        if _parse_utc_date(request_date) is None:
            raise ValueError("A trusted episode request_date is required")
        return request_return(connection, arguments["order_id"], request_date)
    return CUSTOMER_TOOLS[tool_name](connection, **arguments)
