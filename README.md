# Support agent store

Place the Olist CSVs in `data/raw/`, then run `uv run main.py`.
The loader reads the CSVs unchanged into `data/olist.sqlite`, a reproducible
source snapshot. On the first run it copies that snapshot to `data/store.sqlite`
and initializes the required `orders` and `action_events` tables there.
Existing Olist tables retain all their columns.

Use `data/store.sqlite` for agent actions and store tools. Later loader runs
preserve this working database, including its order changes and action records.
To reset the environment, close its connections, delete `data/store.sqlite`, and
run `uv run main.py` again to create a fresh working copy from the source data.

For a temporary empty store, call `store.schema.initialize_schema(connection)`
before inserting orders or calling tools. Initialization creates missing tables
without clearing data or committing the caller's transaction.

Run tests with `uv run python -m unittest discover -s tests`.
