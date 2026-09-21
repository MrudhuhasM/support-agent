# Store Policy — Cancellation (Experimental)

**Scope:** Cancellation only  
**Version:** 0.1.0  
**Policy type:** Synthetic experimental store policy

This policy defines cancellation behavior only. Returns and refunds are not defined yet.



## Determinism requirement

Given the same cancellation request and the same current order state, the policy MUST select exactly one action and exactly one expected final order state.

The decision MUST NOT depend on subjective judgment, reason text, customer type, timing, or operator discretion.

The only inputs required for the cancellation decision are:

1. Whether the requested `order_id` exists.
2. The current `orders.status` value for that order.

## Cancellation rules

| Current order status | Cancellation allowed? | Permitted action | Expected order state afterward | Deterministic result code |
|---|---|---|---|---|
| `created` | Yes | Cancel the order. | `canceled` | `CANCELED` |
| `approved` | Yes | Cancel the order. | `canceled` | `CANCELED` |
| `invoiced` | Yes | Cancel the order. | `canceled` | `CANCELED` |
| `processing` | Yes | Cancel the order. | `canceled` | `CANCELED` |
| `shipped` | No | Deny the cancellation request. Do not change the order status. | `shipped` | `DENIED` |
| `delivered` | No | Deny the cancellation request. Do not change the order status. | `delivered` | `DENIED` |
| `canceled` | Already canceled | Return already canceled. Do not change the order status. | `canceled` | `ALREADY_CANCELED` |
| `unavailable` | Escalate | Escalate the request. Do not automatically change the order status. | `unavailable` | `ESCALATION_REQUIRED` |

### Notes

- Cancellation is whole-order only. Item-level cancellation is out of scope.
- For allowed statuses, the only required order-state change is:
  - `orders.status -> canceled`
- For denied, already-canceled, and escalation cases, `orders.status` MUST remain the same as before the request.
- For `unavailable`, escalation may produce a later decision outside this policy, but the cancellation request itself MUST NOT automatically change the order status.

## Nonexistent order ID

| Condition | Permitted action | Expected database state afterward | Deterministic result code |
|---|---|---|---|
| `order_id` does not exist | Return not found. Do not create, update, or delete any order-related state. | No order row is created or modified. | `ORDER_NOT_FOUND` |
| `order_id` is missing or empty | Return invalid request. Do not create, update, or delete any order-related state. | No order row is created or modified. | `INVALID_REQUEST` |

## Unrecognized order status fallback

If an order exists but its status is not one of the statuses listed above:

- Do not cancel automatically.
- Keep the current order status unchanged.
- Return `UNSUPPORTED_STATUS`.

This fallback is only a safeguard. It does not change the defined behavior for the listed statuses.

## Reference decision procedure

```text
handle_cancellation(order_id):

    if order_id is missing or empty:
        return INVALID_REQUEST

    order = fetch_order(order_id)

    if order does not exist:
        return ORDER_NOT_FOUND

    status = order.status

    if status is one of [created, approved, invoiced, processing]:
        order.status = canceled
        save(order)
        return CANCELED

    if status is one of [shipped, delivered]:
        return DENIED

    if status is canceled:
        return ALREADY_CANCELED

    if status is unavailable:
        return ESCALATION_REQUIRED

    return UNSUPPORTED_STATUS