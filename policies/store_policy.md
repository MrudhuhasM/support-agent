# Store Policy

## Purpose

This policy defines how cancellation, return, return approval, refund eligibility, refund payment treatment, and refund requests are handled.

Every request must produce a deterministic result. Given the same request and the same current state, the same action and final state must be selected. No subjective judgment, discretion, or unstructured exception handling is allowed.

## Common request validation

All cancellation, return, return approval, and refund requests require a valid order identifier.

| Condition | Result | State change |
|---|---|---|
| Order identifier is missing or empty | `INVALID_REQUEST` | No state changes |
| Order identifier does not exist | `ORDER_NOT_FOUND` | No state changes |

Unless explicitly stated otherwise, a denied, invalid, or manual-review request does not modify order state, return state, payment state, or refund state.

## Date handling conventions

All date comparisons use UTC calendar dates.

Rules:

- A missing date value means `NULL`, an empty string, or a whitespace-only string.
- A valid date value must be parseable as an ISO-8601 date or timestamp.
- If a timestamp is provided, convert it to UTC and use the resulting UTC calendar date.
- If no timezone is present, treat the value as UTC.
- The 30-day return window is inclusive.
- A return is within the window when:

  `return_request_date <= delivery_date + 30 calendar days`

Example:

- Delivery date: `2026-10-01`
- Return request date `2026-10-31`: eligible
- Return request date `2026-11-01`: not eligible

## Order statuses

The order lifecycle uses the following statuses:

| Order status | Meaning |
|---|---|
| `created` | The order has been created. |
| `approved` | The order has been approved. |
| `invoiced` | The order has been invoiced. |
| `processing` | The order is being prepared. |
| `shipped` | The order has been shipped. |
| `delivered` | The order has been delivered to the customer. |
| `canceled` | The order has already been canceled. |
| `unavailable` | The order cannot be handled automatically and requires escalation. |

## Cancellation policy

A cancellation request applies to the full order.

| Current order status | Cancellation allowed? | Required action | Expected order state afterward | Result code |
|---|---:|---|---|---|
| `created` | Yes | Cancel the order. | `canceled` | `CANCELED` |
| `approved` | Yes | Cancel the order. | `canceled` | `CANCELED` |
| `invoiced` | Yes | Cancel the order. | `canceled` | `CANCELED` |
| `processing` | Yes | Cancel the order. | `canceled` | `CANCELED` |
| `shipped` | No | Deny the cancellation request. | `shipped` | `DENIED` |
| `delivered` | No | Deny the cancellation request. | `delivered` | `DENIED` |
| `canceled` | Already canceled | Do not cancel again. | `canceled` | `ALREADY_CANCELED` |
| `unavailable` | Escalate | Escalate the request. Do not automatically change the order status. | `unavailable` | `ESCALATION_REQUIRED` |

### Cancellation state effects

When cancellation is allowed:

- The order status becomes `canceled`.
- No return or refund state is created by the cancellation request.

When cancellation is denied, already completed, or escalated:

- The order status remains unchanged.
- No return or refund state is created by the cancellation request.

### Unrecognized order status

If an order exists but has a status not listed above:

- Do not cancel automatically.
- Keep the order status unchanged.
- Return `UNSUPPORTED_STATUS`.

## Return policy

A delivered order can be returned within 30 days of its delivery date.

A return request creates a return request record. It does not authorize a refund.

## Return eligibility

A return request is eligible only when all of the following are true:

1. The order exists.
2. The request is structurally valid.
3. No return request already exists for the order.
4. The order status is `delivered`.
5. A delivery date exists and is valid.
6. The return request date is valid.
7. The return request date is on or after the delivery date.
8. The return request date is no later than 30 calendar days after the delivery date.

For a structurally valid return request, the duplicate-return check has priority over order-status and delivery-window checks.

## Return decision table

Evaluate return requests in the following order.

| Condition | Return allowed? | Required action | Expected state afterward | Result code |
|---|---:|---|---|---|
| Order identifier is missing or empty | No | Return invalid request. | No state changes | `INVALID_REQUEST` |
| Return request date is missing or invalid | No | Return invalid request. | No state changes | `INVALID_REQUEST` |
| Order does not exist | No | Return not found. | No state changes | `ORDER_NOT_FOUND` |
| A return request already exists for the order | Already requested | Do not create a duplicate return request. | Existing return request remains unchanged. Order state remains unchanged. | `RETURN_ALREADY_REQUESTED` |
| Order status is not `delivered` | No | Deny the return request. | Order status remains unchanged. No return request is created. | `RETURN_DENIED_NOT_DELIVERED` |
| Delivery date is missing | No | Deny the return request. | Order status remains unchanged. No return request is created. | `RETURN_DENIED_MISSING_DATE` |
| Delivery date is invalid | No | Deny the return request. | Order status remains unchanged. No return request is created. | `RETURN_DENIED_INVALID_DATE` |
| Return request date is before the delivery date | No | Deny the return request. | Order status remains unchanged. No return request is created. | `RETURN_DENIED_INVALID_DATE` |
| Return request date is more than 30 calendar days after the delivery date | No | Deny the return request. | Order status remains unchanged. No return request is created. | `RETURN_DENIED_WINDOW` |
| Order is delivered, delivery date is valid, return request date is within 30 calendar days, and no return exists | Yes | Create a return request. | Order status remains `delivered`. Return request status becomes `created`. | `RETURN_CREATED` |

## Return request statuses

A return request has one of the following statuses relevant to refunds:

| Return status | Meaning |
|---|---|
| `created` | A return request exists, but it has not been approved. |
| `approved` | The return request has been approved. |

Creating a return request does not approve it.

## Return approval

A return approval request changes an existing return request from `created` to `approved`.

| Current return status | Approval allowed? | Required action | Expected return state afterward | Result code |
|---|---:|---|---|---|
| `created` | Yes | Approve the return request. | `approved` | `RETURN_APPROVED` |
| `approved` | Already approved | Do not change the return request. | `approved` | `RETURN_ALREADY_APPROVED` |
| No return exists | No | Deny the approval request. | No return state is created. | `RETURN_NOT_FOUND` |
| Any other return status | No | Deny the approval request. | Return status remains unchanged. | `RETURN_APPROVAL_DENIED` |

A return approval request does not create a refund. It only makes the return eligible for a later refund request.

## Refund policy

A refund is separate from a return.

- A return is the request to send an order back.
- A refund is the authorization to return money or store credit.

Creating a return does not authorize a refund. Approving a return permits a refund request, but the refund must still be evaluated separately.

This policy records refund authorizations only. It does not perform actual payment transfers, payouts, or store-credit ledger updates.

## Refund eligibility

A refund request is eligible for automatic authorization only when all of the following are true:

1. The order exists.
2. No refund authorization already exists for the order.
3. A return request exists for the order.
4. The return request status is `approved`.
5. The refund treatment can be determined automatically from the order’s payment information.

For a structurally valid refund request, the duplicate-refund check has priority over return-existence and return-status checks.

If any eligibility condition is not met, no refund authorization is created.

## Refund payment treatment

Refund treatment is determined by the order’s payment type.

| Payment type | Refund treatment |
|---|---|
| `credit_card` | Monetary refund |
| `debit_card` | Monetary refund |
| `boleto` | Monetary refund authorization; payout handled separately |
| `voucher` | Restore as store credit |
| `not_defined` | Require manual review |
| No payment records | Require manual review |

### Payment determination rules

For refund treatment:

- If the order has no payment record, require manual review.
- If the order has exactly one payment record, use that payment record’s payment type.
- If the order has multiple payment records, require manual review.
- If a payment type is missing, empty, unrecognized, or `not_defined`, require manual review.
- Recognized payment types are exactly:
  - `credit_card`
  - `debit_card`
  - `boleto`
  - `voucher`

### Refund treatment state values

When a refund is automatically authorized, the refund authorization must include a treatment value:

| Payment type | Refund treatment state |
|---|---|
| `credit_card` | `monetary_refund` |
| `debit_card` | `monetary_refund` |
| `boleto` | `monetary_refund_authorization` |
| `voucher` | `store_credit` |

Manual review does not create a refund authorization.

## Refund decision table

Evaluate refund requests in the following order.

| Condition | Refund allowed? | Required action | Expected state afterward | Result code |
|---|---:|---|---|---|
| Order identifier is missing or empty | No | Return invalid request. | No state changes | `INVALID_REQUEST` |
| Order does not exist | No | Return not found. | No state changes | `ORDER_NOT_FOUND` |
| A refund authorization already exists for the order | No duplicate refund | Do not create another refund authorization. | Existing refund authorization remains unchanged. Return state remains unchanged. Order state remains unchanged. | `REFUND_ALREADY_EXISTS` |
| No return request exists for the order | No | Deny the refund request. | No refund authorization is created. Return state remains unchanged. Order state remains unchanged. | `REFUND_DENIED_NO_RETURN` |
| Return status is `created` | No | Deny the refund request. | No refund authorization is created. Return status remains `created`. Order state remains unchanged. | `REFUND_DENIED_RETURN_NOT_APPROVED` |
| Return status is not `approved` | No | Deny the refund request. | No refund authorization is created. Return status remains unchanged. Order state remains unchanged. | `REFUND_DENIED_RETURN_NOT_APPROVED` |
| Refund treatment requires manual review | No automatic refund | Route the refund request to manual review. | No refund authorization is created. Return state remains unchanged. Order state remains unchanged. | `REFUND_MANUAL_REVIEW_REQUIRED` |
| Return status is `approved`, no refund authorization exists, and refund treatment is determined automatically | Yes | Authorize the refund with the determined treatment. | A refund authorization exists with status `authorized` and the assigned refund treatment. Return status remains unchanged. Order state remains unchanged. | `REFUND_AUTHORIZED` |

## Refund state effects

When a refund is automatically authorized:

- A refund authorization exists with status `authorized`.
- The refund authorization includes the appropriate refund treatment:
  - `monetary_refund`
  - `monetary_refund_authorization`
  - `store_credit`
- The return request status remains unchanged.
- The order status remains unchanged.
- Payment records are not modified.
- No actual payment transfer occurs.
- No payout occurs.
- No store-credit ledger update occurs.

When a refund is denied or sent to manual review:

- No refund authorization is created.
- The return request status remains unchanged.
- The order status remains unchanged.
- Payment records are not modified.
- No payment transfer occurs.
- No payout occurs.
- No store-credit ledger update occurs.

A denied refund request does not prevent a later refund request from being re-evaluated if the return state or payment state changes.

For example:

- Return status is `created`: refund request is denied.
- Return is later approved: return status becomes `approved`.
- A later refund request may then be authorized if no refund authorization exists and payment treatment can be determined automatically.

## Duplicate prevention

### Duplicate return prevention

Only one return request is allowed per order.

If a return request already exists for an order:

- Do not create another return request.
- Keep the existing return request unchanged.
- Return `RETURN_ALREADY_REQUESTED`.

For a structurally valid return request, this duplicate check applies even if the order status later changes.

### Duplicate refund prevention

Only one refund authorization is allowed per order.

If a refund authorization already exists for an order:

- Do not create another refund authorization.
- Keep the existing refund authorization unchanged.
- Return `REFUND_ALREADY_EXISTS`.

For a structurally valid refund request, this duplicate check applies even if the return status later changes, if no return exists, or if the payment state later changes.

## Idempotency

Repeated requests must produce consistent results while the underlying state is unchanged.

### Cancellation idempotency

- A successful cancellation changes the order status to `canceled`.
- A later cancellation request returns `ALREADY_CANCELED`.
- A denied cancellation request leaves the order unchanged and returns the same denial while the order status remains unchanged.
- An escalation-required cancellation request leaves the order unchanged and returns `ESCALATION_REQUIRED` while the order status remains `unavailable`.

### Return idempotency

- A successful return request creates a return request with status `created`.
- A later structurally valid return request for the same order returns `RETURN_ALREADY_REQUESTED`.
- A denied return request leaves the order unchanged and returns the same denial while the underlying state remains unchanged.

### Return approval idempotency

- A successful return approval changes the return status from `created` to `approved`.
- A later approval request returns `RETURN_ALREADY_APPROVED`.
- If no return exists, approval returns `RETURN_NOT_FOUND`.

### Refund idempotency

- A successful refund request creates a refund authorization with status `authorized`.
- A later structurally valid refund request returns `REFUND_ALREADY_EXISTS`.
- If no return exists, refund requests return `REFUND_DENIED_NO_RETURN` until a return exists.
- If the return is not approved, refund requests return `REFUND_DENIED_RETURN_NOT_APPROVED` until the return status becomes `approved`.
- If refund treatment requires manual review, refund requests return `REFUND_MANUAL_REVIEW_REQUIRED` while no refund authorization exists and the payment state still requires manual review.

## Final state summary

| Result code | Order state | Return state | Refund state |
|---|---|---|---|
| `INVALID_REQUEST` | No change | No change | No change |
| `ORDER_NOT_FOUND` | No change | No change | No change |
| `CANCELED` | `canceled` | No change | No change |
| `DENIED` | Unchanged | No change | No change |
| `ALREADY_CANCELED` | `canceled` | No change | No change |
| `ESCALATION_REQUIRED` | `unavailable` | No change | No change |
| `UNSUPPORTED_STATUS` | Unchanged | No change | No change |
| `RETURN_CREATED` | `delivered` | `created` | No refund |
| `RETURN_ALREADY_REQUESTED` | Unchanged | Existing return unchanged | No refund |
| `RETURN_DENIED_NOT_DELIVERED` | Unchanged | No return created | No refund |
| `RETURN_DENIED_MISSING_DATE` | Unchanged | No return created | No refund |
| `RETURN_DENIED_INVALID_DATE` | Unchanged | No return created | No refund |
| `RETURN_DENIED_WINDOW` | Unchanged | No return created | No refund |
| `RETURN_APPROVED` | No change | `approved` | No refund |
| `RETURN_ALREADY_APPROVED` | No change | `approved` | No refund |
| `RETURN_NOT_FOUND` | No change | No change | No refund |
| `RETURN_APPROVAL_DENIED` | No change | Unchanged | No refund |
| `REFUND_AUTHORIZED` | No change | No change | Refund authorization status `authorized`, with treatment `monetary_refund`, `monetary_refund_authorization`, or `store_credit` |
| `REFUND_ALREADY_EXISTS` | No change | No change | Existing refund authorization unchanged |
| `REFUND_DENIED_NO_RETURN` | No change | No change | No refund |
| `REFUND_DENIED_RETURN_NOT_APPROVED` | No change | Unchanged | No refund |
| `REFUND_MANUAL_REVIEW_REQUIRED` | No change | No change | No refund authorization |

## Scope limits

This policy defines eligibility, authorization state, and state transitions only.

The following are not defined here:

- Actual payment transfers
- Actual payout execution
- Store-credit ledger updates
- Payment record modifications
- Refund amount calculation
- Return shipping process
- Item-level returns
- Item-level cancellations
- Carrier or shipping interception behavior