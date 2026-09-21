# Support agent store

Place the Olist CSVs in `data/raw/`, then run `uv run main.py`.
The loader reads the CSVs unchanged into `data/olist.sqlite`, a reproducible
source snapshot. On the first run it copies that snapshot to `data/store.sqlite`
and initializes the store tables there, including `order_returns`,
`refund_authorizations`, and `action_events`.
Existing Olist tables retain all their columns.

Use `data/store.sqlite` for agent actions and store tools. Later loader runs
preserve this working database, including its order changes and action records.
To reset the environment, close its connections, delete `data/store.sqlite`, and
run `uv run main.py` again to create a fresh working copy from the source data.

For a temporary empty store, call `store.schema.initialize_schema(connection)`
before inserting orders or calling tools. Initialization creates missing tables
without clearing data or committing the caller's transaction.

Run tests with `uv run python -m unittest discover -s tests`.

Customer tools are explicitly listed in `store/tools.py`; host code should use
`call_customer_tool()` to dispatch model requests. `store.trusted.approve_return()`
is a back-office operation and is excluded from that registry. Refund
authorization requires an approved return and reads payments from the same
transaction. It records authorization amounts only, without performing payouts.

For reproducible offline ticket generation, see [docs/ticket_generation.md](docs/ticket_generation.md).
The example configuration leaves ticket proportions, split ratio, and triage
label definitions for the owner to choose. API message generation is opt-in.

## F0 evaluation preparation and model runs

The complete walkthrough is [docs/f0_evaluation.md](docs/f0_evaluation.md), including
the infrastructure audit, proposed triage taxonomy, human review, local Qwen setup,
API configuration, and the prediction/gold contracts. No scorer or training code
is included.
The validation artifacts may already exist in this checkout. Inspect those drafts
or choose fresh output paths; preparation never overwrites an existing dataset.

1. Generate a tiny offline draft using an existing order split:

   ```powershell
   uv run python -m scripts.make_smoke_tickets --output data/f0-smoke/tickets --split-manifest data/tickets/order_splits.json
   ```

   If no split exists, explicitly use a smoke-only partition instead:
   `--split-manifest data/f0-smoke/order_splits.json --allow-new-split`.

2. Prepare all three jobs and export a human-review sidecar:

   ```powershell
   uv run python -m scripts.prepare_evaluation --tickets data/f0-smoke/tickets --output data/f0-smoke/prepared-v2 --config configs/evaluation.smoke.json
   uv run python -m scripts.review_evaluation inspect --dataset data/f0-smoke/prepared-v2 --job triage
   uv run python -m scripts.review_evaluation export --dataset data/f0-smoke/prepared-v2 --output data/f0-smoke/review-v2.jsonl
   ```

   Edit `corrected_labels`, decision, reviewer, and notes. Apply reviews with
   `scripts.review_evaluation apply` to a **new** dataset directory. Neither
   teacher outputs nor proposed taxonomy labels are automatically verified.

3. Run a fake-provider smoke test (repeat with `--job reply` and `--job actions`):

   ```powershell
   uv run python -m scripts.run_models --dataset data/f0-smoke/prepared-v2 --job triage --config configs/model.fake.json --output runs/f0-smoke-v2/triage --allow-draft
   ```

4. Install the optional `requirements-inference.txt` runtime and configure a
   pinned model revision in `model.qwen_base.json` or `model.qwen_chat.json` for
   local inference. Files are loaded from cache; downloads need `--allow-download`.

5. Fill `model.frontier.example.json` in a run-specific copy, set the named
   credential environment variable, and use `scripts.run_models --plan` to inspect
   the call budget. Real requests require explicit `--allow-api`. Optional reply
   references use `--purpose reference_draft` and still require human review.

6. Each run saves raw/parsed predictions, seeds/settings, latency, token/API
   metadata, and action trajectories to `predictions.jsonl` and durable journals.
   Reuse the same command/directory to resume. `evaluation.contracts.evaluation_pairs`
   joins predictions with gold for the scorer you implement later.

Prepared model inputs and private gold are separate files. Customer tools cannot
set return dates or approve returns. Action episodes use isolated temporary
databases and record observations without deciding success or rewards.
