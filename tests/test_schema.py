import io
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main
from store.actions import cancel_order


class StoreInitializationTests(unittest.TestCase):
    def test_loader_preserves_source_and_existing_working_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_data = root / "raw"
            raw_data.mkdir()
            source_database = root / "olist.sqlite"
            working_database = root / "store.sqlite"
            raw_contents = {}
            for table, filename in main.TABLE_FILES.items():
                contents = (
                    "order_id,order_status,order_purchase_timestamp\n"
                    "sample-order,processing,2018-01-01 00:00:00\n"
                    if table == "orders"
                    else "order_id\nsample-order\n"
                )
                path = raw_data / filename
                path.write_text(contents, encoding="utf-8")
                raw_contents[path] = path.read_bytes()

            with patch.multiple(
                main,
                RAW_DATA=raw_data,
                DATABASE=source_database,
                WORKING_DATABASE=working_database,
            ), redirect_stdout(io.StringIO()):
                main.main()
                with closing(sqlite3.connect(working_database)) as connection:
                    self.assertEqual(cancel_order(connection, "sample-order"), "CANCELED")
                    connection.commit()

                main.main()

            with closing(sqlite3.connect(working_database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT order_status, order_purchase_timestamp FROM orders"
                    ).fetchall(),
                    [("canceled", "2018-01-01 00:00:00")],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT tool_name, order_id, result FROM action_events"
                    ).fetchall(),
                    [("cancel_order", "sample-order", "CANCELED")],
                )
                for table in main.TABLE_FILES:
                    self.assertEqual(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                        1,
                    )

            with closing(sqlite3.connect(source_database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT order_status FROM orders").fetchall(),
                    [("processing",)],
                )
            for path, contents in raw_contents.items():
                self.assertEqual(path.read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
