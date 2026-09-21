import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from store.actions import authorize_refund, request_return
from store.schema import initialize_schema
from store.trusted import approve_return
from tickets.generate import INTENTS, generate_tickets


class TicketGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "olist.sqlite"
        self.splits = self.root / "order_splits.json"
        with closing(sqlite3.connect(self.source)) as source:
            source.execute("""CREATE TABLE orders (
                order_id TEXT, customer_id TEXT, order_status TEXT,
                order_purchase_timestamp TEXT, order_approved_at TEXT,
                order_delivered_carrier_date TEXT, order_delivered_customer_date TEXT,
                order_estimated_delivery_date TEXT)""")
            source.execute("""CREATE TABLE order_payments (
                order_id TEXT, payment_sequential TEXT, payment_type TEXT,
                payment_installments TEXT, payment_value TEXT)""")
            for index in range(80):
                order_id = f"order-{index:03d}"
                status = "delivered" if index % 2 else "shipped"
                source.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                    order_id, f"customer-{index:03d}", status, "2018-01-01 12:00:00",
                    "2018-01-02 12:00:00", "2018-01-03 12:00:00",
                    "2018-01-10 12:00:00" if status == "delivered" else "",
                    "2018-01-15 00:00:00",
                ))
                source.executemany("INSERT INTO order_payments VALUES (?, ?, ?, ?, ?)", (
                    (order_id, "1", "credit_card", "1", "12.34"),
                    (order_id, "2", "voucher", "1", "2.00"),
                ))
            source.commit()
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.config = {
            "seed": 42, "split_seed": 7, "held_out_fraction": 0.25,
            "tickets_per_split": {split: {intent: 2 for intent in INTENTS} for split in ("train", "held_out")},
            "cancellation_after_days": 0, "delivery_lateness_days": 1,
            "return_request_days": [15, 30, 31], "refund_return_after_days": 15,
            "refund_request_after_days": 16, "refund_return_states": ["approved"],
            "triage_labels": {intent: {"urgency": None, "route": None} for intent in INTENTS},
            "message_generation": {"mode": "template", "model": "offline-template-v1", "parameters": {}},
        }

    def generate(self, name: str = "tickets", **kwargs) -> tuple[dict, dict]:
        output = self.root / name
        manifest = generate_tickets(self.source, output, self.splits, self.config, **kwargs)
        records = {
            split: [json.loads(line) for line in (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()]
            for split in ("train", "held_out")
        }
        return manifest, records

    def test_facts_policy_labels_and_provenance_match_source(self) -> None:
        manifest, records = self.generate()
        self.assertEqual(manifest["counts"], {"train": 8, "held_out": 8})
        output = self.root / "tickets"
        for filename, expected_hash in manifest["artifacts_sha256"].items():
            self.assertEqual(hashlib.sha256((output / filename).read_bytes()).hexdigest(), expected_hash)
        self.assertEqual(manifest["policy_sha256"], manifest["artifacts_sha256"]["policy.md"])
        self.assertEqual(manifest["prompt_template_sha256"], manifest["artifacts_sha256"]["prompt_template.txt"])
        with closing(sqlite3.connect(self.source)) as source:
            source.row_factory = sqlite3.Row
            for split, tickets in records.items():
                for ticket in tickets:
                    with self.subTest(ticket=ticket["ticket_id"]):
                        order = dict(source.execute("SELECT * FROM orders WHERE order_id = ?", (ticket["order_id"],)).fetchone())
                        self.assertEqual(ticket["order_state"], order)
                        self.assertIn(order["order_id"], ticket["customer_message"])
                        self.assertIn(order["order_id"], ticket["generation"]["prompt"])
                        self.assertEqual(ticket["manual_verification_required"], split == "held_out")
                        self.assertEqual(ticket["generation"]["model"], "offline-template-v1")
                        self.assertIsInstance(ticket["generation"]["seed"], int)
                        if ticket["intent"] == "delivery_issue":
                            self.assertIsNone(ticket["expected_action"])
                            self.assertIsNone(ticket["expected_result_code"])
                            self.assertIn("delivery_issue_resolution_not_defined_in_store_policy", ticket["policy_gaps"])
                            self.assertGreater(ticket["request_date"], order["order_estimated_delivery_date"][:10])
                            self.assertEqual(order["order_status"], "shipped")
                        else:
                            self.assertEqual(len(ticket["expected_action_events"]), 1)
                            event = ticket["expected_action_events"][0]
                            self.assertEqual(event["tool_name"], ticket["expected_action"])
                            self.assertEqual(event["result"], ticket["expected_result_code"])
                            self.assertGreater(event["event_id"], ticket["episode_start_event_id"])
                        if ticket["intent"] == "cancellation":
                            self.assertEqual(ticket["expected_result_code"], "DENIED")
                        if ticket["intent"] == "refund":
                            self.assertEqual(ticket["initial_environment"]["return"]["return_status"], "approved")
                            self.assertTrue(ticket["environment_setup"][-1]["trusted_back_office"])
                            self.assertEqual(ticket["expected_result_code"], "REFUND_AUTHORIZED")
                            self.assertEqual(ticket["expected_environment"]["refund_authorization"]["cash_cents"], 1234)
                            self.assertEqual(ticket["expected_environment"]["refund_authorization"]["voucher_cents"], 200)
                            self.assertGreaterEqual(ticket["request_date"], ticket["initial_environment"]["return"]["return_request_date"])
                        if ticket["intent"] == "return":
                            if order["order_status"] != "delivered":
                                self.assertEqual(ticket["expected_result_code"], "RETURN_DENIED_NOT_DELIVERED")
                            elif ticket["request_date"] <= "2018-02-09":
                                self.assertEqual(ticket["expected_result_code"], "RETURN_CREATED")
                            else:
                                self.assertEqual(ticket["expected_result_code"], "RETURN_DENIED_WINDOW")
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)

    def test_no_order_leakage_and_identical_inputs_reproduce_selection_and_messages(self) -> None:
        _, records = self.generate("first")
        split_bytes = self.splits.read_bytes()
        _, repeated = self.generate("second")
        self.assertEqual(records, repeated)
        self.assertEqual(self.splits.read_bytes(), split_bytes)
        train_ids = {ticket["order_id"] for ticket in records["train"]}
        held_out_ids = {ticket["order_id"] for ticket in records["held_out"]}
        self.assertFalse(train_ids & held_out_ids)
        partition = json.loads(split_bytes)["orders"]
        self.assertEqual(len(partition), 80)
        self.assertTrue(all(partition[order_id] == "train" for order_id in train_ids))
        self.assertTrue(all(partition[order_id] == "held_out" for order_id in held_out_ids))

    def test_split_and_dataset_cannot_be_silently_overwritten(self) -> None:
        self.generate()
        held_out_before = (self.root / "tickets" / "held_out.jsonl").read_bytes()
        with self.assertRaises(FileExistsError):
            self.generate()
        self.config["split_seed"] += 1
        with self.assertRaisesRegex(ValueError, "cannot be overwritten"):
            self.generate("changed-split")
        self.assertEqual((self.root / "tickets" / "held_out.jsonl").read_bytes(), held_out_before)

    def test_teacher_is_opt_in_and_cannot_replace_policy_labels(self) -> None:
        self.config["message_generation"] = {"mode": "provider", "model": "fake-teacher", "parameters": {"temperature": 0}}
        calls = []

        def fake_teacher(prompt: str, *, model: str, parameters: dict, seed: int) -> str:
            calls.append({"prompt": prompt, "model": model, "parameters": parameters, "seed": seed})
            facts = json.loads(prompt.partition("Request facts:")[2].strip())
            return f"Please help with my order {facts['order']['order_id']}."

        with self.assertRaisesRegex(ValueError, "opt-in"):
            self.generate("not-opted-in", message_generator=fake_teacher)
        self.assertEqual(calls, [])
        _, records = self.generate("teacher", message_generator=fake_teacher, allow_api=True)
        self.assertEqual(len(calls), 16)
        for tickets in records.values():
            for ticket in tickets:
                self.assertTrue(ticket["message_verification_required"])
                self.assertEqual(ticket["generation"]["model"], "fake-teacher")
                if ticket["intent"] == "refund":
                    self.assertEqual(ticket["expected_result_code"], "REFUND_AUTHORIZED")

    def test_missing_sampling_decisions_are_flagged_instead_of_invented(self) -> None:
        self.config["held_out_fraction"] = None
        with self.assertRaisesRegex(ValueError, "held_out_fraction"):
            self.generate()
        self.assertFalse(self.splits.exists())

    def test_refund_setup_states_produce_policy_grounded_labels(self) -> None:
        self.config["tickets_per_split"] = {
            split: {intent: int(intent == "refund") for intent in INTENTS}
            for split in ("train", "held_out")
        }
        for state, result in (
            ("none", "REFUND_DENIED_NO_RETURN"),
            ("created", "REFUND_DENIED_RETURN_NOT_APPROVED"),
            ("approved", "REFUND_AUTHORIZED"),
        ):
            with self.subTest(state=state):
                self.config["refund_return_states"] = [state]
                _, records = self.generate("refund-" + state)
                for tickets in records.values():
                    for ticket in tickets:
                        self.assertEqual(ticket["expected_result_code"], result)
                        approvals = [step for step in ticket["environment_setup"] if step["tool_name"] == "approve_return"]
                        self.assertEqual(len(approvals), int(state == "approved"))

    def test_working_refund_and_ticket_generation_leave_original_olist_unchanged(self) -> None:
        working_path = self.root / "working.sqlite"
        with closing(sqlite3.connect(self.source.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(working_path)) as working:
                source.backup(working)
                initialize_schema(working)
                self.assertEqual(request_return(working, "order-001", "2018-01-25"), "RETURN_CREATED")
                self.assertEqual(approve_return(working, "order-001"), "RETURN_APPROVED")
                self.assertEqual(authorize_refund(working, "order-001"), "REFUND_AUTHORIZED")
                working.commit()
        self.generate()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        with closing(sqlite3.connect(self.source)) as source:
            self.assertEqual(source.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),
                             [("order_payments",), ("orders",)])

    def test_invalid_messages_preserve_attempt_provenance_without_a_completed_dataset(self) -> None:
        self.config["message_generation"] = {"mode": "provider", "model": "fake", "parameters": {}}
        with self.assertRaisesRegex(ValueError, "exact order ID"):
            self.generate(message_generator=lambda *args, **kwargs: "Invented unsupported content", allow_api=True)
        output = self.root / "tickets"
        self.assertFalse((output / "train.jsonl").exists())
        self.assertEqual(json.loads((output / "manifest.json").read_text())["status"], "incomplete")
        attempts = [json.loads(line) for line in (output / "generation_attempts.jsonl").read_text().splitlines()]
        self.assertEqual(attempts[0]["model"], "fake")
        self.assertEqual(attempts[0]["status"], "request")
        self.assertIn("prompt", attempts[0])
        self.assertEqual(attempts[1]["status"], "response")

    def test_offline_command_line_entry_point(self) -> None:
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        output = self.root / "cli-tickets"
        result = subprocess.run(
            [sys.executable, "-m", "scripts.generate_tickets", "--config", str(config_path),
             "--source", str(self.source), "--output", str(output),
             "--split-manifest", str(self.splits)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"], {"train": 8, "held_out": 8})
        self.assertTrue((output / "manifest.json").exists())

    def test_provider_failure_keeps_request_metadata(self) -> None:
        self.config["message_generation"] = {"mode": "provider", "model": "fake", "parameters": {}}

        def failing_provider(*args, **kwargs):
            raise RuntimeError("simulated provider failure")

        with self.assertRaisesRegex(RuntimeError, "simulated provider"):
            self.generate(message_generator=failing_provider, allow_api=True)
        output = self.root / "tickets"
        attempts = [json.loads(line) for line in (output / "generation_attempts.jsonl").read_text().splitlines()]
        self.assertEqual([attempt["status"] for attempt in attempts], ["request", "error"])
        self.assertIn("seed", attempts[0])
        self.assertEqual(attempts[1]["error_type"], "RuntimeError")
        self.assertEqual(json.loads((output / "manifest.json").read_text())["status"], "incomplete")

    def test_modified_order_assignments_are_rejected(self) -> None:
        self.generate()
        partition = json.loads(self.splits.read_text())
        order_id = next(iter(partition["orders"]))
        partition["orders"][order_id] = "train" if partition["orders"][order_id] == "held_out" else "held_out"
        self.splits.write_text(json.dumps(partition), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot be overwritten"):
            self.generate("changed-assignment")


if __name__ == "__main__":
    unittest.main()
