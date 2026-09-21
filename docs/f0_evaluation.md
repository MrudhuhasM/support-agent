# F0 preparation and model-running guide

This is prediction collection and dataset preparation, not an evaluation scorer.
The owner implements metrics, rubrics, judgments, rewards, and estimators. A
prediction with `status: ok` passed the output contract (or reached an action
final response); it has **not** been judged correct.

## Audit and boundaries

The existing ticket generator, order split, policy tools, and verifiers are
reused. Blocking evaluation issues addressed in this batch:

- Raw ticket JSONL mixes labels, teacher prompts, hidden setup, and source facts.
  New prepared versions have explicit `inputs/<job>/<split>.jsonl` and
  `gold/<job>/<split>.jsonl`. Prompts accept whitelisted input fields only.
- Customer dispatch previously accepted `request_return.request_date`. Customer
  JSON now accepts only `order_id`. The host injects the trusted episode date.
  The raw Python store function retains its trusted date argument for setup.
- A valid policy result does not prove the tool should have been called. Action
  gold keeps the previous simulation as hidden **candidate** evidence and leaves
  `required_behavior` unresolved for explicit human decisions.
- Generated and teacher-written labels/references are drafts. Review sidecars
  produce new dataset versions with reviewer identity and rationale. Original
  datasets and order assignments are not rewritten.
- Action episodes use isolated temporary SQLite files. Committed state is read
  through a new connection and action events are scoped to the episode. No
  existing verifier is invoked to decide a reward or success label.
- Model responses are durably cached before tools execute. Interrupted episodes
  rebuild initial state and replay cached responses, without sharing databases
  or repeating completed model requests. Ambiguous paid requests need a separate
  explicit retry decision.

The weak and event-aware cancellation verifiers remain research baselines.
They are not universal action scorers. In particular, requiring a denial-tool
event for every shipped-order request is an unresolved behavior-label decision.

## Proposed triage taxonomy

The following is **proposed-v1**, not established ground truth or an SLA:

| Intent | Proposed urgency | Proposed route |
|---|---|---|
| cancellation | routine | order_support |
| delivery_issue | time_sensitive | order_support |
| return | routine | returns_refunds |
| refund | routine | returns_refunds |

Two urgency values and two broad routing groups keep the first comparison easy
to inspect. Current delivery tickets describe overdue, undelivered orders, so
they form the proposed time-sensitive group. This does not establish real-world
severity, escalation thresholds, staffing, or response-time guarantees. Those
rules are absent from the store policy and require owner approval.

`configs/evaluation.taxonomy.example.json` contains the deterministic mapping.
Supply a revised `triage_taxonomy` in preparation config if different labels or
rules are chosen. Version the taxonomy and its few-shot examples together.
Setting its approval field does not verify any example: all newly prepared gold
starts as `draft`. Human review is still required.

## Prepare a small offline draft

First load Olist CSVs using the existing loader if no source snapshot exists.
If you already have an order split, reuse it:

```powershell
uv run python -m scripts.make_smoke_tickets --output data/f0-smoke/tickets --split-manifest data/tickets/order_splits.json
```

For a fresh checkout without a split, explicitly create a **provisional,
smoke-only** partition in a separate namespace:

```powershell
uv run python -m scripts.make_smoke_tickets --output data/f0-smoke/tickets --split-manifest data/f0-smoke/order_splits.json --allow-new-split
```

The helper loads `configs/tickets.smoke.json`, makes no API calls, and reuses the
seed/ratio from an existing split when supplied. Its new-split ratio and tiny mix
are diagnostic examples, not approved project dataset choices. It never changes
an existing partition. Do not mix independent smoke partitions into formal data.

Prepare all three jobs from these frozen tickets:

```powershell
uv run python -m scripts.prepare_evaluation --tickets data/f0-smoke/tickets --output data/f0-smoke/prepared-v2 --config configs/evaluation.smoke.json
```

Preparation deterministically selects at most one ticket per intent per split
in smoke mode. `evaluation.full.json` retains every source ticket. For larger
datasets, choose counts in the original ticket generator config and use the
same permanent order split. Preparation copies its bytes unchanged. Existing
output directories are rejected.

All jobs preserve source fingerprints, source-generation prompts/settings,
configuration, policy snapshot, order membership, and per-example provenance.
Preparation does not edit the Olist source or existing ticket files.

## Visible inputs and private gold

- Triage receives the customer message, trusted request date, and generic task
  definition. It does not receive the ticket's intent, urgency, or route labels.
- Reply writing receives the message, trusted date, current order/return/refund
  facts, historical payment facts, and policy context. These facts are from the
  **initial** state, never the simulated outcome. A customer-facing reply must
  not claim execution merely because a policy rule would permit it.
- Actions receive the message, trusted date, policy, and public tool schemas.
  Initial database state, setup, candidate result codes, expected outcomes, and
  verifier material stay in gold and are consumed only by host code.

General policy result-code tables and task label spaces are public instructions;
the per-example expected result or chosen label is private. The model adapter
receives only `{request_id, job, messages, seed}`, never a gold record.
Gold also carries a copy of those visible inputs under `evaluation_context`, so
an owner-written scorer receives customer text, facts, and policy directly in its
prediction/gold pair rather than having to discover request-cache files.

## Inspect and review

```powershell
uv run python -m scripts.review_evaluation inspect --dataset data/f0-smoke/prepared-v2 --job actions
uv run python -m scripts.review_evaluation export --dataset data/f0-smoke/prepared-v2 --output data/f0-smoke/review-v2.jsonl
```

The inspection command is for the human owner and displays private gold. Do not
use its output as a model prompt. The exported JSONL defaults to held-out data;
use `--split train` to review training references/labels separately.

Edit a sidecar record's `corrected_labels`, `decision` (`pending`, `verified`, or
`rejected`), `reviewer`, and `notes`. A verified or rejected record needs a named
reviewer and a rationale. Merely exporting, generating a reference, or running a
model never changes verification status.

For an action record, explicitly choose:

```json
{"mode":"either","tool_name":"cancel_order","notes":"Owner decision about whether an observed denial or a policy explanation is acceptable."}
```

`required_behavior.mode` can be `tool_required`, `reply_only`, or `either`. Set a
tool name for tool-required cases. Review `expected_outcome` alongside that choice:
reply-only behavior usually implies no business-state change. These decisions
are not inferred by the runner. Delivery resolution and whether denied requests
need an attempted tool call remain unresolved until the owner specifies them.
Original candidate simulations remain available under `hidden.policy_simulation`.

Reply gold permits an absent reference. A present reference must have
`reference_status: reviewed` before verification. This is a human decision about
that reference, not a rubric score. The reply rubric remains `owner_required`.

Apply changes to a new version:

```powershell
uv run python -m scripts.review_evaluation apply --dataset data/f0-smoke/prepared-v2 --reviews data/f0-smoke/review-v2.jsonl --output data/f0-smoke/reviewed-v1
```

Review sidecars are bound to the original dataset and gold fingerprints.
Unreviewed records remain drafts, rejected records remain in the audit trail and
are excluded from model runs, and split/order assignments and model inputs are
preserved. Corrections outside the existing label space require a new taxonomy
version and matching prompts, not an impossible gold label for the current task.

## Run an offline fake-model smoke test

```powershell
uv run python -m scripts.run_models --dataset data/f0-smoke/prepared-v2 --job triage --config configs/model.fake.json --output runs/f0-smoke-v2/triage --allow-draft
uv run python -m scripts.run_models --dataset data/f0-smoke/prepared-v2 --job reply --config configs/model.fake.json --output runs/f0-smoke-v2/reply --allow-draft
uv run python -m scripts.run_models --dataset data/f0-smoke/prepared-v2 --job actions --config configs/model.fake.json --output runs/f0-smoke-v2/actions --allow-draft
```

These exercise I/O only. The fake model is not a model-quality baseline.
`--allow-draft` is diagnostic and never certifies labels. Formal held-out runs
default to requiring verified records. Use a reviewed version without this flag
after human decisions are complete. Training examples can be collected while
still marked draft; that also does not establish label correctness.

Action models emit one JSON `tool_call` or `final` per turn. Calls run only
through the allowlist and are scoped to the task's order. `request_return` takes
only `order_id`; dates come from immutable host context even if the customer asks
to backdate. `approve_return`, arbitrary Python, and SQL are unavailable. Defaults
are six model turns and four attempted tool calls. The runner records final
responses, errors, tool observations, scoped action events, and independently read
final database tables. It does not decide whether the trajectory was successful.

## Local Qwen inference

Optional local inference requirements are in `requirements-inference.txt`.
Install a CUDA-enabled PyTorch build appropriate to your machine using the
[official PyTorch selector](https://pytorch.org/get-started/locally/), then install
the remaining optional requirements into your environment. For Windows:

```powershell
uv pip install --python .venv/Scripts/python.exe -r requirements-inference.txt
```

Under WSL use `.venv/bin/python`. No heavy runtime is needed for data preparation,
HTTP adapters, fake runs, or tests. This batch does not install or download weights.

The supplied configs target [Qwen3-0.6B-Base](https://huggingface.co/Qwen/Qwen3-0.6B-Base)
and the [official Qwen3-0.6B](https://huggingface.co/Qwen/Qwen3-0.6B). Qwen3 needs
Transformers 4.51 or later. The Base config uses plain text continuation with two
synthetic few-shot demonstrations per job; the official model uses its chat
template with thinking disabled. Demonstrations are proposed format examples,
not approved held-out data or a tuned baseline. Review them and freeze their
version before comparisons. Held-out few-shot examples are rejected.
The provided greedy decoding and context limits are infrastructure examples,
not tuned or owner-approved comparison settings. Freeze the chosen settings
before interpreting baseline quality.

Configure model IDs, exact model/tokenizer revision SHAs, device, dtype, context
limit, generation parameters, and seeds in run-specific copies of the configs.
`main` is a convenient example revision, not a stable research pin. Resolved HF
commit IDs and installed runtime versions are recorded when available.

```powershell
uv run python -m scripts.run_models --dataset data/f0-smoke/prepared-v2 --job triage --config configs/model.qwen_base.json --output runs/qwen-base-triage --allow-draft
uv run python -m scripts.run_models --dataset data/f0-smoke/prepared-v2 --job triage --config configs/model.qwen_chat.json --output runs/qwen-chat-triage --allow-draft
```

Local runs use cached files only by default. Explicitly add `--allow-download`
after deciding to download the configured weights. The adapter uses safetensors,
disables remote Python code, selects CUDA/FP16 when available, and otherwise
records CPU/FP32. Explicit CUDA requests fail if unavailable. Batch size one and
a bounded context are initial settings for the RTX 3050 6GB; actual memory and
speed depend on your runtime and prompt lengths and have not been GPU-tested here.
There is no silent prompt truncation. Reduce context/batch limits if necessary.

[HF generation guidance](https://huggingface.co/docs/transformers/llm_tutorial)
uses left padding for decoder-only batches. Greedy runs can batch independent
requests; their elapsed generation time is recorded as **batch** latency, not
invented per-example latency. Sampling uses one request per RNG seed to avoid
changing results merely because a batch was partially resumed. Seeds do not
guarantee identical GPU results across devices/library versions or API backends.

## Configure a frontier API and optional reference writer

Copy `configs/model.frontier.example.json`. Set the provider's full HTTPS
Chat-Completions-compatible endpoint, model/snapshot ID, supported parameters,
token cap, and the name of an environment variable containing its credential.
No account, provider, endpoint, or credential is discovered automatically.
No API call occurs when inspecting a plan:

```powershell
uv run python -m scripts.run_models --dataset data/f0-smoke/prepared-v2 --job reply --config configs/model.frontier.local.json --output runs/frontier-reply --plan
```

The plan reports example count and maximum model-call count, including episode
turn limits. Review the provider's current prices, token caps, and these counts
before approving paid requests. There is no bundled pricing table or cost formula.
After explicit cost approval, add `--allow-api` (and `--allow-draft` only for
diagnostics). Credentials come exclusively from the configured environment variable
and are never recorded in prompts, manifests, or requests on disk.

The HTTP adapter follows the [official Chat Completions schema](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
It records returned model identity, raw response, token usage/details, request IDs,
latency, and cost metadata if the provider supplies it. It does not estimate cost.
Provider seed support is explicit via `send_seed`; the requested seed is always
logged, even if the endpoint cannot accept it. Endpoint errors become per-example
records and there are no automatic network retries. Other protocols can implement
the same batch callable contract used by the fake/HF/HTTP adapters.

To create optional reference drafts, run the reply job with
`--purpose reference_draft`. Export a review sidecar with
`--reference-predictions runs/<reference-run>/predictions.jsonl`. References are
attached to **pending** review records only; they do not replace gold automatically.
This also works with local models and training examples using `--split train`.

## Resume and hand off to owner-written scorers

Re-run the same command and output directory to resume. The dataset, input/gold,
code, few-shot, model configuration, and resolved runtime fingerprints must match.
Completed records are skipped, including failures. `--retry-errors` explicitly
retries failures while retaining previous attempts. For parse/limit failures that
can mean generating a new attempt; it is not a free cached replay.

Durable response files allow an interrupted action episode to replay tools in a
new database without requesting its completed responses again. If a paid request
was started but no response was saved, billing may be ambiguous: default resume
does not repeat that request. Check provider usage before explicitly supplying
both `--retry-errors` and `--retry-pending-api`. Client-side logging cannot guarantee
exactly-once billing after a network/process failure. Use one writer per run
directory; concurrent writers are unsupported.

Each run preserves `manifest.json`, request/response caches, `generation.jsonl`,
append-only `attempts.jsonl`, and a consolidated `predictions.jsonl` with one latest
record per example. A torn last journal line is retained in a recovery file;
dataset files are never repaired or truncated. Predictions include raw output,
parsed prediction, parameters, seeds, latency/usage/provider metadata, and action
observations. Retrying an error does not erase the earlier attempt.

Use the following I/O boundary when implementing your scorer:

```python
from evaluation.contracts import evaluation_pairs, save_evaluation_results

pairs = evaluation_pairs(
    "runs/my-run/predictions.jsonl",
    "data/my-reviewed-dataset/gold/triage/held_out.jsonl",
)
# Each yielded pair is (prediction_record, gold_record).
# Supply your own scorer and then serialize its EvaluationResult records.
```

Gold fingerprints must match. Deliberately rescoring an old prediction against a
new human-reviewed gold version requires `allow_gold_revision=True`; this decision
must be recorded by the owner. No result is computed by `evaluation_pairs`.

The owner still implements triage metrics, reply rubrics/swapped-order judging,
action success/reward definitions, pass@k estimation, confidence intervals, standard
errors/variance analysis, and all post-training algorithms. No mechanisms under
`core/`, training code, or Post-Training Spine progress records are changed here.

## File map

| Files | Role |
|---|---|
| `evaluation/data.py`, `review.py` | Whitelisted job inputs, private gold, versioned manual corrections |
| `evaluation/contracts.py`, `files.py` | Typed I/O, strict parsing, scorer joins, durable serialization |
| `evaluation/prompts.py`, `models.py` | Job prompts, few-shot validation, fake/HF/API adapters |
| `evaluation/episodes.py`, `runner.py` | Isolated execution, observations, response cache, resume |
| `scripts/make_smoke_tickets.py`, `prepare_evaluation.py` | Small offline source tickets and configurable preparation |
| `scripts/review_evaluation.py`, `run_models.py` | Human inspection/review and prediction/reference collection |
| `configs/evaluation.*.json`, `model.*.json`, `tickets.smoke.json` | Draft taxonomy, dataset sizes, and runtime/provider settings |
| `prompts/few_shot.proposed.json`, `requirements-inference.txt` | Synthetic format demonstrations and optional local dependencies |
| `store/tools.py`, `store/actions.py` | Trusted-date dispatch and safe UTC/date-window handling |
| `tests/test_evaluation.py`, `tests/test_refunds.py` | New integration coverage and updated customer-date boundary test |
| `README.md`, `.gitignore`, this guide | Workflow, artifact locations, and local generated-file exclusions |
