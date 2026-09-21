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
