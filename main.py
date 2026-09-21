import csv
import sqlite3
from contextlib import closing
from pathlib import Path

from store.schema import initialize_schema


ROOT = Path(__file__).resolve().parent
RAW_DATA = ROOT / "data" / "raw"
DATABASE = ROOT / "data" / "olist.sqlite"
WORKING_DATABASE = ROOT / "data" / "store.sqlite"

TABLE_FILES = {
    "orders": "olist_orders_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "order_payments": "olist_order_payments_dataset.csv",
    "order_reviews": "olist_order_reviews_dataset.csv",
}


def quote_identifier(identifier: str) -> str:
    """Quote a SQLite identifier read from a CSV header."""
    return '"' + identifier.replace('"', '""') + '"'


def load_csv(connection: sqlite3.Connection, table: str, path: Path) -> None:
    """Load a CSV unchanged, storing every value as text."""
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.reader(csv_file)
        columns = next(reader)
        quoted_columns = ", ".join(quote_identifier(column) for column in columns)
        definitions = ", ".join(f"{quote_identifier(column)} TEXT" for column in columns)

        connection.execute(f"DROP TABLE IF EXISTS {quote_identifier(table)}")
        connection.execute(f"CREATE TABLE {quote_identifier(table)} ({definitions})")
        placeholders = ", ".join("?" for _ in columns)
        insert_sql = (
            f"INSERT INTO {quote_identifier(table)} ({quoted_columns}) "
            f"VALUES ({placeholders})"
        )
        connection.executemany(insert_sql, reader)


def main() -> None:
    missing_files = [filename for filename in TABLE_FILES.values() if not (RAW_DATA / filename).is_file()]
    if missing_files:
        names = ", ".join(missing_files)
        raise FileNotFoundError(f"Missing CSV files in {RAW_DATA}: {names}")

    DATABASE.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DATABASE)) as connection:
        with connection:
            for table, filename in TABLE_FILES.items():
                load_csv(connection, table, RAW_DATA / filename)

        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        order_statuses = connection.execute(
            "SELECT order_status, COUNT(*) FROM orders GROUP BY order_status"
        ).fetchall()

        # Seed a new working copy; preserve an existing environment and its events.
        WORKING_DATABASE.parent.mkdir(parents=True, exist_ok=True)
        new_working_database = not WORKING_DATABASE.exists()
        with closing(sqlite3.connect(WORKING_DATABASE)) as working_connection:
            if new_working_database:
                connection.backup(working_connection)
            with working_connection:
                initialize_schema(working_connection)

    print(f"Orders: {order_count}")
    print("Orders by status:")
    for status, count in order_statuses:
        print(f"  {status}: {count}")
    print(f"Source database: {DATABASE}")
    print(f"Working database: {WORKING_DATABASE}")


if __name__ == "__main__":
    main()
