# F0 ticket preparation

This pipeline reads real orders and historical payment records from the Olist
source database through a read-only connection. It creates English customer
messages, task metadata, and draft policy labels. It does not train models or
score model behavior.

## Choose the dataset configuration

Copy `configs/tickets.example.json` to a run-specific configuration file.
Set `held_out_fraction` and every count in `tickets_per_split` explicitly.
These values are intentionally unset in the example: the project owner has not
chosen the ticket mix or the train/held-out ratio. Zero counts are allowed.
Counts specify tickets per intent in each split, with at most one ticket per
order and intent. Different intents can reference the same order within a split.
Generation fails with a candidate count if the requested sample is too large.

The example seeds and date offsets illustrate a small draft experiment; they
are not established project decisions. `split_seed` controls the permanent order
partition. `seed` controls ticket selection, scenario choices, and per-message
seeds. A provider may not honor seeds deterministically; saved responses are the
record of what it generated. Offline templates are deterministic.

Define `triage_labels` (`urgency` and `route`) for each intent when those design
rules are agreed. Leaving a value null produces an explicit `policy_gaps` entry
and `label_status: requires_design_decision`. No urgency or routing taxonomy is
inferred from the teacher or from order status.

The current policy defines no delivery-issue resolution. Delivery tickets are
supported as grounded overdue-order observations, but their `expected_action`
and `expected_result_code` remain null and their labels are flagged unresolved.
Do not use them as action evaluation labels until that policy decision is made.

## Run without API calls

From the repository root, after loading the raw CSVs with `uv run main.py`:

```powershell
uv run python -m scripts.generate_tickets --config configs/tickets.my-run.json --output data/tickets/run-001 --split-manifest data/tickets/order_splits.json
```

`--source` defaults to `data/olist.sqlite`. Use the reproducible source snapshot,
not the agent's modified working database. The source file is never initialized,
migrated, or updated by ticket generation. Source facts and a SHA-256 fingerprint
are recorded. Inconsistent or unusable source dates are excluded and candidate
counts are included in the run manifest.

The pipeline assigns **all source order IDs** to train or held-out before it
selects tickets. A split manifest is created once, then reused. A changed source
fingerprint, split seed, or ratio is rejected; the existing manifest is never
rewritten. Do not replace it to obtain more favorable results. Version a genuinely
new source dataset separately and resolve split continuity before combining data.
For tiny datasets the held-out partition contains at least one order, and the
training partition also contains at least one. Otherwise the count is the floor
of `order_count * held_out_fraction`.

Existing output directories are rejected, including held-out files. Make a new
run directory for each generation attempt. The permanent split can be created
even if a later step fails; future attempts reuse it.
Failed provider runs keep an incomplete manifest, configuration, prompt/policy
snapshots, and a `generation_attempts.jsonl` log of requests and responses. They
do not produce completed dataset files. Keep these logs when investigating a
failure; retries must use a new output directory.

## Facts, simulated dates, and policy labels

Every ticket keeps its exact source `order_state` and `payment_records`. Request
dates are explicitly simulated UTC calendar dates compatible with that fixed
snapshot; they are not reconstructed historical customer requests. Recorded
purchase, approval, carrier delivery, and customer delivery dates cannot fall
after a ticket's simulated request date. An order with a customer delivery date
and a non-delivered status is excluded as inconsistent.

Cancellation requests use the latest recorded event date plus the configured
offset. Delivery issues use active, undelivered orders whose simulated request
date is after estimated delivery. Return dates are offsets from actual delivery
(or the latest available order event when the order is not delivered).

Refund scenarios can start with no return, a created return, or an approved
return, according to `refund_return_states`. Each starts from a real delivered
order. Its return date must lie within the policy window and precede the refund
request. Simulated preconditions are prepared using `request_return()` and the
trusted `approve_return()` operation, then recorded under `environment_setup`.
They are never presented as original Olist records or as actions the customer
agent can perform. This first version does not synthesize prior refunds.

For cancellation, return, and refund intents, the pipeline applies the existing
policy tools to an isolated in-memory working database. The resulting action,
result code, amounts, and final state become the draft task labels. It snapshots
the last event ID after setup and records expected events from this episode only.
This reuses the policy implementation; it is not an independent audit of that
implementation. The policy tests and later manual verification remain necessary.

Messages cannot replace these labels. Held-out records always have
`manual_verification_required: true` and `verification_status:
pending_manual_verification`; generation does not certify them for evaluation.

## Optional teacher adapter

The prompt is in `prompts/customer_ticket.txt`. A provider-independent Python
callable receives only a prompt and generation settings:

```python
def generate_message(prompt: str, *, model: str, parameters: dict, seed: int) -> str:
    # Connect this to your chosen, separately configured model client.
    # Return English customer prose only, including the supplied exact order ID.
    ...
```

Set `message_generation.mode` to `provider`, record the exact model identity and
generation parameters, and provide your adapter as `module:function`.
After approving the provider's cost, opt in explicitly:

```powershell
uv run python -m scripts.generate_tickets --config configs/tickets.teacher.json --output data/tickets/teacher-001 --provider my_adapter:generate_message --allow-api
```

There is no bundled API client, credential discovery, or paid default. The adapter
is not even imported without opt-in. It should return one message per call; the
maximum number of pipeline calls is the sum of configured ticket counts. Budget
for the chosen provider's token prices and any retries implemented by the adapter
before approving a run. Generated prose still requires factual review: the prompt
and simple output checks cannot establish that a teacher avoided invented facts.

## Saved artifacts and replay

Each run contains `train.jsonl`, `held_out.jsonl`, `manifest.json`, `config.json`,
`order_splits.json`, `policy.md`, and `prompt_template.txt`. Every ticket includes
its prompt, model identity, generation parameters, seed, config fingerprint,
source facts, simulated setup, policy outcome, triage labels, and review flags.
The manifest includes the Git revision, hashes of the implementation actually
used (including uncommitted code), source/policy fingerprints, candidate counts,
and output hashes. Keep the source snapshot and these artifacts together.

To replay a task later, copy its real source order/payment facts to an isolated
working database, apply its recorded setup through trusted host code, then begin
the customer episode. Give the model access only to the explicit registry in
`store.tools`. Never give it arbitrary SQL/Python execution or expose
`store.trusted`; module separation alone is not a sandbox.

The datasets are JSONL infrastructure artifacts. No evaluation scorer or model
training is included in this batch.
