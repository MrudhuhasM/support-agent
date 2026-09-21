# Store Policy — Cancellation and Returns (Experimental)

**Scope:** Cancellation and Returns  
**Version:** 0.3.0  
**Checkpoint:** F0 in progress  
**Policy type:** Synthetic experimental store policy

This policy defines cancellation and return behavior only. Refunds are not defined yet.

This is a synthetic policy for the experimental store. It is not intended to represent Olist’s historical policy.

## Database mapping used by this policy

This policy uses the following existing tables and columns:

- `orders.order_id`
- `orders.order_status`
- `orders.order_delivered_customer_date`

Return requests are tracked in a separate table:

- `order_returns.order_id`
- `order_returns.return_status`
- `order_returns.return_request_date`

The required return-tracking table is:

```sql
CREATE TABLE IF NOT EXISTS order_returns (
    order_id TEXT PRIMARY KEY,
    return_status TEXT NOT NULL,
    return_request_date TEXT NOT NULL
);
```

For this experimental policy, one order can have at most one active whole-order return request.

No other tables are modified by this policy. In particular, this policy does not modify:

- `customers`
- `order_items`
- `order_payments`
- `order_reviews`

## Determinism requirement

Given the same request and the same current database state, the policy MUST select exactly one action and exactly one expected final state.

The decision MUST NOT depend on subjective judgment, reason text, customer type, timing except explicit date rules, or operator discretion.

## Date handling rules

Date columns in the current schema are stored as `TEXT`.

For this policy:

- A missing date value means `NULL`, empty string, or whitespace-only string.
- A valid date value must be parseable as an ISO-8601 date or timestamp.
- If a timestamp is provided, use its UTC calendar date.
- If no timezone is present, treat the value as UTC.
- The 30-day return window is inclusive.
- Example: if `orders.order_delivered_customer_date` is `2026-10-01`, then a return request dated `2026-10-31` is eligible, while a request dated `2026-11-01` is denied.

## Cancellation rules

The cancellation decision uses `orders.order_status`.

| Current `orders.order_status` | Cancellation allowed? | Permitted action | Expected database state afterward | Deterministic result code |
|---|---|---|---|---|
| `created` | Yes | Cancel the order. | `orders.order_status` becomes `canceled`. | `CANCELED` |
| `approved` | Yes | Cancel the order. | `orders.order_status` becomes `canceled`. | `CANCELED` |
| `invoiced` | Yes | Cancel the order. | `orders.order_status` becomes `canceled`. | `CANCELED` |
| `processing` | Yes | Cancel the order. | `orders.order_status` becomes `canceled`. | `CANCELED` |
| `shipped` | No | Deny the cancellation request. | `orders.order_status` remains `shipped`. | `DENIED` |
| `delivered` | No | Deny the cancellation request. | `orders.order_status` remains `delivered`. | `DENIED` |
| `canceled` | Already canceled | Return already canceled. | `orders.order_status` remains `canceled`. | `ALREADY_CANCELED` |
| `unavailable` | Escalate | Escalate the request. Do not automatically change the order. | `orders.order_status` remains `unavailable`. | `ESCALATION_REQUIRED` |

### Cancellation notes

- Cancellation is whole-order only. Item-level cancellation is out of scope.
- For allowed statuses, the only database change is:

```sql
UPDATE orders
SET order_status = 'canceled'
WHERE order_id = ?;
```

- For denied, already-canceled, and escalation cases, no table is modified.
- For `unavailable`, escalation may produce a later decision outside this policy, but the cancellation request itself MUST NOT automatically change `orders.order_status`.

## Cancellation: nonexistent or invalid order ID

| Condition | Permitted action | Expected database state afterward | Deterministic result code |
|---|---|---|---|
| `order_id` is missing or empty | Return invalid request. | No table is modified. | `INVALID_REQUEST` |
| `order_id` does not exist in `orders` | Return not found. | No table is modified. | `ORDER_NOT_FOUND` |

## Cancellation: unrecognized order status fallback

If an order exists but `orders.order_status` is not one of the statuses listed above:

- Do not cancel automatically.
- Keep `orders.order_status` unchanged.
- Return `UNSUPPORTED_STATUS`.

This fallback is only a safeguard. It does not change the defined behavior for the listed statuses.

## Cancellation reference decision procedure

```text
handle_cancellation(order_id):

    if order_id is missing or empty:
        return INVALID_REQUEST

    order = SELECT order_status
            FROM orders
            WHERE order_id = ?

    if no order row exists:
        return ORDER_NOT_FOUND

    status = order.order_status

    if status is one of [created, approved, invoiced, processing]:
        UPDATE orders
        SET order_status = 'canceled'
        WHERE order_id = ?

        return CANCELED

    if status is one of [shipped, delivered]:
        return DENIED

    if status is canceled:
        return ALREADY_CANCELED

    if status is unavailable:
        return ESCALATION_REQUIRED

    return UNSUPPORTED_STATUS
```

## Returns

### Experimental return rule

A delivered order can be returned within 30 days of its delivery date.

The delivery date is `orders.order_delivered_customer_date`.

### Return eligibility

A return request qualifies only if all of the following are true:

1. The order exists in `orders`.
2. `orders.order_status` is `delivered`.
3. There is not already a return request for the order in `order_returns`.
4. `orders.order_delivered_customer_date` exists.
5. `orders.order_delivered_customer_date` can be parsed as a valid date.
6. The return request date is on or after the delivery date.
7. The return request date is no later than the delivery date plus 30 calendar days.

### Return decision table

Evaluate return conditions in the order shown.

| Evaluation condition | Return allowed? | Permitted action | Expected database state afterward | Deterministic result code |
|---|---|---|---|---|
| Missing `order_id` or missing `return_request_date` | No | Return invalid request. | No table is modified. | `INVALID_REQUEST` |
| `return_request_date` cannot be parsed as a valid date | No | Return invalid request. | No table is modified. | `INVALID_REQUEST` |
| Order does not exist in `orders` | N/A | Return not found. | No table is modified. | `ORDER_NOT_FOUND` |
| `orders.order_status` is not `delivered` | No | Deny the return request. | `orders.order_status` remains unchanged. No row is inserted into `order_returns`. | `RETURN_DENIED_NOT_DELIVERED` |
| `orders.order_status` is `delivered` and a row already exists in `order_returns` for the order | Already requested | Do not create a duplicate return request. | `orders.order_status` remains `delivered`. Existing `order_returns` row remains unchanged. | `RETURN_ALREADY_REQUESTED` |
| `orders.order_status` is `delivered` but `orders.order_delivered_customer_date` is missing | No | Deny the return request. | `orders.order_status` remains `delivered`. No row is inserted into `order_returns`. | `RETURN_DENIED_MISSING_DATE` |
| `orders.order_status` is `delivered` but `orders.order_delivered_customer_date` cannot be parsed as a valid date | No | Deny the return request. | `orders.order_status` remains `delivered`. No row is inserted into `order_returns`. | `RETURN_DENIED_INVALID_DATE` |
| `orders.order_status` is `delivered`, delivery date is valid, but return request date is before the delivery date | No | Deny the return request. | `orders.order_status` remains `delivered`. No row is inserted into `order_returns`. | `RETURN_DENIED_INVALID_DATE` |
| `orders.order_status` is `delivered`, delivery date is valid, no return exists, and return request date is between delivery date and delivery date + 30 days inclusive | Yes | Create a return request. | `orders.order_status` remains `delivered`. One row is inserted into `order_returns` with `return_status = 'created'`. | `RETURN_CREATED` |
| `orders.order_status` is `delivered`, delivery date is valid, no return exists, but return request date is after delivery date + 30 days | No | Deny the return request. | `orders.order_status` remains `delivered`. No row is inserted into `order_returns`. | `RETURN_DENIED_WINDOW` |

### What happens when a return is created

When a return is allowed:

```sql
INSERT INTO order_returns (
    order_id,
    return_status,
    return_request_date
)
VALUES (
    ?,
    'created',
    ?
);
```

The order itself remains delivered:

- `orders.order_status` remains `delivered`.
- `order_items` is not modified.
- `order_payments` is not modified.
- `order_reviews` is not modified.
- No refund is triggered by this policy.

### What happens when a return is denied

When any return denial result is produced:

- No row is inserted into `order_returns`.
- `orders.order_status` is not changed.
- `order_items` is not modified.
- `order_payments` is not modified.
- `order_reviews` is not modified.
- No refund is triggered.
- The database final state is unchanged from the state before the return request.

If the order was delivered, it remains `delivered`.

If the order was not delivered, it remains in whatever `orders.order_status` it already had.

### Return reference decision procedure

```text
handle_return(order_id, return_request_date):

    if order_id is missing or empty:
        return INVALID_REQUEST

    if return_request_date is missing:
        return INVALID_REQUEST

    request_date = parse_utc_calendar_date(return_request_date)

    if request_date is not valid:
        return INVALID_REQUEST

    order = SELECT order_status, order_delivered_customer_date
            FROM orders
            WHERE order_id = ?

    if no order row exists:
        return ORDER_NOT_FOUND

    if order.order_status != delivered:
        return RETURN_DENIED_NOT_DELIVERED

    existing_return = SELECT order_id
                      FROM order_returns
                      WHERE order_id = ?

    if existing_return exists:
        return RETURN_ALREADY_REQUESTED

    delivery_date = parse_utc_calendar_date(order.order_delivered_customer_date)

    if order.order_delivered_customer_date is missing:
        return RETURN_DENIED_MISSING_DATE

    if delivery_date is not valid:
        return RETURN_DENIED_INVALID_DATE

    if request_date < delivery_date:
        return RETURN_DENIED_INVALID_DATE

    if request_date <= add_days(delivery_date, 30):
        INSERT INTO order_returns (
            order_id,
            return_status,
            return_request_date
        )
        VALUES (
            ?,
            'created',
            ?
        )

        return RETURN_CREATED

    return RETURN_DENIED_WINDOW
```

Where:

- `add_days(date, 30)` adds 30 calendar days.
- `parse_utc_calendar_date(value)` returns the UTC calendar date represented by the value.
- A return request is considered active if a row exists for the order in `order_returns`.

## Expected final state for reward evaluation

| Result code | Expected final `orders.order_status` | Expected `order_returns` state | Other table effects |
|---|---|---|---|
| `CANCELED` | `canceled` | No row inserted by this request | None |
| `DENIED` | Same as before request: `shipped` or `delivered` | No row inserted | None |
| `ALREADY_CANCELED` | `canceled` | No row inserted | None |
| `ESCALATION_REQUIRED` | `unavailable` | No row inserted | None |
| `UNSUPPORTED_STATUS` | Same as before request | No row inserted | None |
| `RETURN_CREATED` | `delivered` | One row with `return_status = 'created'` | None |
| `RETURN_ALREADY_REQUESTED` | `delivered` | Existing row remains unchanged | None |
| `RETURN_DENIED_NOT_DELIVERED` | Same as before request | No row inserted | None |
| `RETURN_DENIED_MISSING_DATE` | `delivered` | No row inserted | None |
| `RETURN_DENIED_INVALID_DATE` | `delivered` | No row inserted | None |
| `RETURN_DENIED_WINDOW` | `delivered` | No row inserted | None |
| `ORDER_NOT_FOUND` | No matching order row is created or changed | No row inserted | None |
| `INVALID_REQUEST` | No table is changed | No row inserted | None |

## Idempotency

- A successful cancellation changes `orders.order_status` to `canceled`.
- A later cancellation request for the same order MUST return `ALREADY_CANCELED` and MUST NOT change `orders.order_status`.
- A denied cancellation request does not change any table. Repeating the request while the underlying state remains unchanged MUST produce the same denial.
- An escalation-required cancellation result does not automatically change any table. Repeating the request while `orders.order_status` remains `unavailable` MUST produce the same escalation-required result.
- A successful return request inserts one row into `order_returns`.
- A later return request for the same order MUST return `RETURN_ALREADY_REQUESTED` and MUST NOT insert a duplicate return row.
- A denied return request does not change any table. Repeating the same denied request while the underlying state remains unchanged MUST produce the same denial.

