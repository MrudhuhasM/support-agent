import copy
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from evaluation.contracts import evaluation_pairs, parse_prediction, save_evaluation_results, validate_model_input
from evaluation.data import TAXONOMY, dataset_paths, load_job, prepare_dataset, verify_artifacts
from evaluation.episodes import run_episode
from evaluation.files import canonical, file_hash, load_json, read_jsonl, write_jsonl
from evaluation.models import create_adapter
from evaluation.prompts import build_messages, load_few_shots, render_base_prompt
from evaluation.review import apply_reviews, export_review
from evaluation.runner import run_predictions
from tickets.generate import INTENTS, generate_tickets


CANARY = "HIDDEN_EVALUATION_CANARY_9f61"


def response(text, error=None):
    return {"text": text, "error": error, "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "latency_seconds": 0.01, "provider_metadata": {"fake": True}}


def adapter(function, *, paid=False):
    return {"generate": function, "metadata": {"model_id": "test-fake-v1"}, "requires_api": paid}


class EvaluationInfrastructureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "olist.sqlite"
        with closing(sqlite3.connect(self.source)) as source:
            source.execute("""CREATE TABLE orders (order_id TEXT, order_status TEXT,
                order_purchase_timestamp TEXT, order_delivered_customer_date TEXT,
                order_estimated_delivery_date TEXT, internal_note TEXT)""")
            source.execute("CREATE TABLE order_payments (order_id TEXT, payment_type TEXT, payment_value TEXT)")
            for index in range(60):
                order_id = f"order-{index:03d}"
                source.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", (
                    order_id, "delivered" if index % 2 else "shipped", "2018-01-01",
                    "2018-01-10" if index % 2 else None, "2018-01-15", CANARY,
                ))
                source.execute("INSERT INTO order_payments VALUES (?, ?, ?)", (order_id, "credit_card", "12.34"))
            source.commit()
        self.source_hash = file_hash(self.source)
        self.tickets = self.root / "tickets"
        self.split_file = self.root / "splits.json"
        config = {
            "seed": 42, "split_seed": 7, "held_out_fraction": 0.25,
            "tickets_per_split": {split: {intent: 2 for intent in INTENTS} for split in ("train", "held_out")},
            "cancellation_after_days": 0, "delivery_lateness_days": 1,
            "return_request_days": [15, 30, 31], "refund_return_after_days": 15,
            "refund_request_after_days": 16, "refund_return_states": ["none"],
            "triage_labels": {intent: {"urgency": None, "route": None} for intent in INTENTS},
            "message_generation": {"mode": "template", "model": "offline-template-v1", "parameters": {}},
        }
        generate_tickets(self.source, self.tickets, self.split_file, config)
        self.dataset = self.root / "prepared"
        self.preparation = {"seed": 7, "per_intent_limit": 1}
        prepare_dataset(self.tickets, self.dataset, self.preparation)
        self.config = {"backend": "fake", "model_id": "test-fake-v1", "revision": "1",
                       "seed": 7, "batch_size": 1, "parameters": {}, "prompt_style": "chat",
                       "max_steps": 6, "max_tool_calls": 4}

    def test_dataset_separation_deterministic_sampling_and_source_preservation(self):
        repeated = self.root / "prepared-again"
        prepare_dataset(self.tickets, repeated, self.preparation)
        original = verify_artifacts(self.dataset)
        self.assertFalse(original["taxonomy"]["approved"])
        for job in ("triage", "reply", "actions"):
            train_inputs, train_gold, _ = load_job(self.dataset, job, "train")
            inputs, gold, _ = load_job(self.dataset, job, "held_out")
            self.assertEqual(len(inputs), 4)
            self.assertFalse({row["order_id"] for row in train_gold.values()} & {row["order_id"] for row in gold.values()})
            self.assertNotIn(CANARY, canonical(inputs))
            self.assertNotIn("expected_action", canonical(inputs))
            self.assertNotIn("expected_result_code", canonical(inputs))
            for row in inputs:
                self.assertEqual(gold[row["example_id"]]["review"]["status"], "draft")
                self.assertNotIn("labels", row["input"])
                self.assertEqual(gold[row["example_id"]]["evaluation_context"], row["input"])
            for split in ("train", "held_out"):
                left, right = dataset_paths(self.dataset, job, split), dataset_paths(repeated, job, split)
                for a, b in zip(left, right):
                    self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual((self.dataset / "order_splits.json").read_bytes(), self.split_file.read_bytes())
        self.assertEqual(file_hash(self.source), self.source_hash)

    def test_all_three_jobs_run_offline_without_gold_leakage_or_scores(self):
        for job in ("triage", "reply", "actions"):
            builtin = create_adapter(self.config)
            seen = []

            def generate(requests):
                self.assertNotIn(CANARY, canonical(requests))
                self.assertNotIn("expected_result_code", canonical(requests))
                self.assertTrue(all(set(request) == {"request_id", "messages", "job", "seed"} for request in requests))
                seen.extend(requests)
                return builtin["generate"](requests)

            output = self.root / ("run-" + job)
            result = run_predictions(self.dataset, output, job, "held_out", self.config,
                                     allow_draft=True, adapter=adapter(generate))
            self.assertEqual(result["completed_predictions"], 4)
            predictions = read_jsonl(output / "predictions.jsonl")
            self.assertTrue(all(row["status"] == "ok" for row in predictions))
            self.assertTrue(all("score" not in row and "reward" not in row for row in predictions))
            self.assertTrue(all(row["raw_output"] and row["parsed_prediction"] for row in predictions))
            if job == "actions":
                self.assertTrue(all(row["observations"]["turns"][0]["parsed"]["tool_name"] == "get_order" for row in predictions))
            self.assertGreaterEqual(len(seen), 4)
            _, gold_path = dataset_paths(self.dataset, job, "held_out")
            with self.assertRaisesRegex(ValueError, "manual verification"):
                list(evaluation_pairs(output / "predictions.jsonl", gold_path))
            self.assertEqual(len(list(evaluation_pairs(output / "predictions.jsonl", gold_path, require_verified=False))), 4)
        self.assertEqual(file_hash(self.source), self.source_hash)

    def test_manual_review_creates_new_version_and_requires_behavior_decisions(self):
        sidecar = self.root / "review.jsonl"
        export_review(self.dataset, sidecar)
        reviews = read_jsonl(sidecar)
        original_hash = file_hash(self.dataset / "gold" / "triage" / "held_out.jsonl")
        for row in reviews:
            row.update(decision="verified", reviewer="test-human", notes="Explicitly reviewed facts and this proposed label decision")
        pending_actions = self.root / "not-yet-valid.jsonl"
        write_jsonl(pending_actions, reviews)
        with self.assertRaisesRegex(ValueError, "behavior choice"):
            apply_reviews(self.dataset, pending_actions, self.root / "invalid-reviewed")
        for row in reviews:
            if row["job"] == "actions":
                row["corrected_labels"]["required_behavior"] = {
                    "mode": "reply_only", "tool_name": None, "notes": "Test human explicitly selected reply-only behavior",
                }
                row["corrected_labels"]["expected_outcome"] = row["hidden_context"]["episode"]["initial_environment"]
        revised = self.root / "human-reviewed.jsonl"
        write_jsonl(revised, reviews)
        target = self.root / "reviewed"
        apply_reviews(self.dataset, revised, target)
        self.assertEqual(file_hash(self.dataset / "gold" / "triage" / "held_out.jsonl"), original_hash)
        self.assertEqual((target / "order_splits.json").read_bytes(), self.split_file.read_bytes())
        for job in ("triage", "reply", "actions"):
            _, gold, _ = load_job(target, job, "held_out")
            self.assertTrue(all(row["review"]["status"] == "verified" for row in gold.values()))
        run_predictions(target, self.root / "verified-run", "triage", "held_out", self.config)
        with self.assertRaises(FileExistsError):
            apply_reviews(self.dataset, revised, target)

    def test_references_remain_drafts_until_review(self):
        output = self.root / "reference-run"
        run_predictions(self.dataset, output, "reply", "held_out", self.config,
                        purpose="reference_draft", allow_draft=True)
        sidecar = self.root / "references-review.jsonl"
        export_review(self.dataset, sidecar, reference_predictions=output / "predictions.jsonl")
        rows = read_jsonl(sidecar)
        replies = [row for row in rows if row["job"] == "reply"]
        self.assertTrue(all(row["decision"] == "pending" for row in replies))
        self.assertTrue(all(row["corrected_labels"]["reference_status"] == "draft" for row in replies))
        _, original, _ = load_job(self.dataset, "reply", "held_out")
        self.assertTrue(all(row["labels"]["reference_response"] is None for row in original.values()))

    def _delivered_episode(self):
        inputs, gold, _ = load_job(self.dataset, "actions", "held_out")
        for row in inputs:
            hidden = gold[row["example_id"]]["hidden"]
            if hidden["policy_simulation"]["expected_action"] == "authorize_refund":
                return copy.deepcopy(hidden["episode"]), build_messages(row, [])
        self.fail("Fixture lacks a delivered refund episode")

    def test_trusted_date_approval_scope_and_sql_boundaries(self):
        episode, messages = self._delivered_episode()
        episode["request_date"] = "2018-02-10"
        order_id = episode["order_id"]
        outputs = [
            {"type": "tool_call", "tool_name": "request_return", "arguments": {"order_id": order_id, "request_date": "2018-01-20"}},
            {"type": "tool_call", "tool_name": "request_return", "arguments": {"order_id": order_id}},
            {"type": "tool_call", "tool_name": "approve_return", "arguments": {"order_id": order_id}},
            {"type": "tool_call", "tool_name": "execute_sql", "arguments": {"order_id": order_id}},
            {"type": "final", "response": "Finished observing the host results."},
        ]

        def generate(transcript, turn):
            self.assertNotIn(CANARY, canonical(transcript))
            return response(canonical(outputs[turn]))

        result = run_episode(episode, messages, generate, max_steps=5, max_tool_calls=4)
        self.assertEqual(result["termination"], "final")
        self.assertEqual(result["final_state"]["order_returns"], [])
        self.assertEqual([event["result"] for event in result["action_events"]], ["RETURN_DENIED_WINDOW"])
        self.assertIsNotNone(result["turns"][0]["observation"]["error"])
        self.assertEqual(result["turns"][1]["trusted_arguments"]["request_date"], "2018-02-10")
        self.assertIsNotNone(result["turns"][2]["observation"]["error"])
        self.assertIsNotNone(result["turns"][3]["observation"]["error"])

    def test_episode_isolation_and_limits(self):
        episode, messages = self._delivered_episode()
        order_id = episode["order_id"]

        def create_return(transcript, turn):
            value = {"type": "tool_call", "tool_name": "request_return", "arguments": {"order_id": order_id}} if turn == 0 else {"type": "final", "response": "Done"}
            return response(canonical(value))

        for _ in range(2):
            result = run_episode(episode, messages, create_return)
            self.assertEqual(result["initial_state"]["order_returns"], [])
            self.assertEqual(result["action_events"][0]["result"], "RETURN_CREATED")
        repeated = lambda transcript, turn: response(canonical({"type": "tool_call", "tool_name": "get_order", "arguments": {"order_id": order_id}}))
        limited = run_episode(episode, messages, repeated, max_steps=5, max_tool_calls=1)
        self.assertEqual(limited["termination"], "tool_limit")
        malformed = run_episode(episode, messages, lambda transcript, turn: response("not JSON"), max_steps=2)
        self.assertEqual(malformed["termination"], "turn_limit")
        self.assertEqual(file_hash(self.source), self.source_hash)

    def test_resume_after_interruption_and_recover_torn_journal(self):
        output = self.root / "resumable"
        calls = []

        def interrupted(requests):
            calls.extend(requests)
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return [response('{"intent":"return","urgency":"routine","route":"returns_refunds"}') for _ in requests]

        with self.assertRaises(KeyboardInterrupt):
            run_predictions(self.dataset, output, "triage", "held_out", self.config,
                            allow_draft=True, adapter=adapter(interrupted))
        self.assertEqual(len(read_jsonl(output / "attempts.jsonl")), 1)
        resumed_calls = []

        def resumed(requests):
            resumed_calls.extend(requests)
            return [response('{"intent":"return","urgency":"routine","route":"returns_refunds"}') for _ in requests]

        run_predictions(self.dataset, output, "triage", "held_out", self.config,
                        allow_draft=True, adapter=adapter(resumed))
        self.assertEqual(len(resumed_calls), 3)
        rows = read_jsonl(output / "predictions.jsonl")
        self.assertEqual(len({row["prediction_id"] for row in rows}), 4)
        before = (output / "predictions.jsonl").read_bytes()
        with (output / "attempts.jsonl").open("ab") as journal:
            journal.write(b'{"prediction_id":')
        run_predictions(self.dataset, output, "triage", "held_out", self.config, allow_draft=True,
                        adapter=adapter(lambda requests: self.fail("Completed predictions must not run again")))
        self.assertEqual((output / "predictions.jsonl").read_bytes(), before)
        self.assertTrue((output / "attempts.jsonl.recovered_tail").exists())

    def test_errors_do_not_lose_other_predictions_and_can_be_explicitly_retried(self):
        output = self.root / "errors"
        counter = []

        def faulty(requests):
            counter.extend(requests)
            if len(counter) == 2:
                return [response(None, {"type": "FakeFailure", "message": "test"})]
            return [response('{"response":"A draft reply."}')]

        run_predictions(self.dataset, output, "reply", "held_out", self.config,
                        allow_draft=True, adapter=adapter(faulty))
        rows = read_jsonl(output / "predictions.jsonl")
        self.assertEqual([row["status"] for row in rows].count("model_error"), 1)
        retried = []

        def recovered(requests):
            retried.extend(requests)
            return [response('{"response":"A recovered draft reply."}')]

        run_predictions(self.dataset, output, "reply", "held_out", self.config,
                        allow_draft=True, retry_errors=True, adapter=adapter(recovered))
        self.assertEqual(len(retried), 1)
        self.assertEqual(len(read_jsonl(output / "attempts.jsonl")), 5)
        self.assertTrue(all(row["status"] == "ok" for row in read_jsonl(output / "predictions.jsonl")))

    def test_action_resume_replays_cached_response_in_a_fresh_environment(self):
        config = {**self.config, "max_examples": 1}
        output = self.root / "action-resume"
        builtin = create_adapter(self.config)
        first_calls = []

        def interrupted(requests):
            first_calls.extend(requests)
            if len(first_calls) == 2:
                raise KeyboardInterrupt()
            result = builtin["generate"](requests)
            parsed = json.loads(result[0]["text"])
            parsed["tool_name"] = "cancel_order"
            result[0]["text"] = canonical(parsed)
            return result

        with self.assertRaises(KeyboardInterrupt):
            run_predictions(self.dataset, output, "actions", "held_out", config,
                            allow_draft=True, adapter=adapter(interrupted))
        resumed_calls = []

        def resumed(requests):
            resumed_calls.extend(requests)
            return [response('{"type":"final","response":"Observed the result."}')]

        run_predictions(self.dataset, output, "actions", "held_out", config,
                        allow_draft=True, adapter=adapter(resumed))
        self.assertEqual(len(resumed_calls), 1)
        observations = read_jsonl(output / "predictions.jsonl")[0]["observations"]
        self.assertEqual(len(observations["action_events"]), 1)
        self.assertEqual(observations["action_events"][0]["tool_name"], "cancel_order")

    def test_parse_errors_can_be_retried_and_changed_runs_are_rejected(self):
        config = {**self.config, "max_examples": 1}
        output = self.root / "parse-errors"
        run_predictions(self.dataset, output, "reply", "held_out", config,
                        allow_draft=True, adapter=adapter(lambda requests: [response("bad JSON")]))
        self.assertEqual(read_jsonl(output / "predictions.jsonl")[0]["status"], "parse_error")
        calls = []

        def recovered(requests):
            calls.extend(requests)
            return [response('{"response":"Valid retry"}')]

        run_predictions(self.dataset, output, "reply", "held_out", config,
                        allow_draft=True, retry_errors=True, adapter=adapter(recovered))
        self.assertEqual(len(calls), 1)
        self.assertEqual(read_jsonl(output / "predictions.jsonl")[0]["status"], "ok")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            run_predictions(self.dataset, output, "reply", "held_out", {**config, "seed": 123}, allow_draft=True)

    def test_pending_api_request_is_not_automatically_reissued(self):
        config = {**self.config, "max_examples": 1}
        output = self.root / "ambiguous-api"

        def interrupted(requests):
            raise KeyboardInterrupt()

        with self.assertRaises(PermissionError):
            run_predictions(self.dataset, output, "reply", "held_out", config, allow_draft=True,
                            adapter=adapter(interrupted, paid=True))
        with self.assertRaises(KeyboardInterrupt):
            run_predictions(self.dataset, output, "reply", "held_out", config, allow_draft=True,
                            allow_api=True, adapter=adapter(interrupted, paid=True))
        calls = []

        def recovered(requests):
            calls.extend(requests)
            return [response('{"response":"Recovered"}')]

        run_predictions(self.dataset, output, "reply", "held_out", config, allow_draft=True,
                        allow_api=True, adapter=adapter(recovered, paid=True))
        self.assertEqual(calls, [])
        run_predictions(self.dataset, output, "reply", "held_out", config, allow_draft=True,
                        allow_api=True, retry_errors=True, retry_pending_api=True,
                        adapter=adapter(recovered, paid=True))
        self.assertEqual(len(calls), 1)

    def test_base_few_shots_exclude_held_out_and_output_contracts_are_strict(self):
        examples = Path(__file__).resolve().parents[1] / "prompts" / "few_shot.proposed.json"
        self.assertEqual(len(load_few_shots(examples, set())), 6)
        with self.assertRaisesRegex(ValueError, "disjoint"):
            load_few_shots(examples, {"demo-a"})
        self.assertTrue(render_base_prompt([{"role": "user", "content": "test"}]).endswith("Assistant:\n"))
        with self.assertRaises(ValueError):
            parse_prediction("triage", '{"intent":"return","urgency":"routine","route":"x","score":1}')
        with self.assertRaises(ValueError):
            parse_prediction("actions", '{"type":"python","code":"print(1)"}')
        save_evaluation_results(self.root / "owner-results.jsonl", [{
            "prediction_id": "p", "example_id": "e", "job": "reply", "scorer": "owner", "scorer_version": "1",
            "result": {"note": "Owner-supplied serialization only"},
        }])

    def test_nested_hidden_fields_and_privileged_tool_schemas_are_rejected(self):
        reply_inputs, _, _ = load_job(self.dataset, "reply", "held_out")
        leaked = copy.deepcopy(reply_inputs[0])
        leaked["input"]["order_facts"]["order"]["expected_result_code"] = CANARY
        with self.assertRaisesRegex(ValueError, "hidden"):
            validate_model_input(leaked)
        actions, _, _ = load_job(self.dataset, "actions", "held_out")
        expanded = copy.deepcopy(actions[0])
        expanded["input"]["permitted_tools"].append({"tool_name": "approve_return", "arguments": {"order_id": "string"}})
        with self.assertRaisesRegex(ValueError, "allowlist"):
            validate_model_input(expanded)

    def test_new_versions_and_runs_cannot_be_created_inside_frozen_datasets(self):
        sidecar = self.root / "safe-review.jsonl"
        export_review(self.dataset, sidecar)
        with self.assertRaisesRegex(ValueError, "outside"):
            apply_reviews(self.dataset, sidecar, self.dataset / "nested-version")
        with self.assertRaisesRegex(ValueError, "outside"):
            prepare_dataset(self.tickets, self.tickets / "nested-prepared", self.preparation)
        with self.assertRaisesRegex(ValueError, "outside"):
            run_predictions(self.dataset, self.dataset / "nested-run", "reply", "held_out", self.config, allow_draft=True)
        self.assertFalse((self.dataset / "nested-version").exists())

    def test_taxonomy_configuration_never_certifies_generated_gold(self):
        taxonomy = copy.deepcopy(TAXONOMY)
        taxonomy.update(version="owner-test-version", approved=True, urgencies=["ordinary", "urgent"])
        for labels in taxonomy["labels_by_intent"].values():
            labels["urgency"] = "urgent" if labels["urgency"] == "time_sensitive" else "ordinary"
        target = self.root / "custom-taxonomy"
        prepare_dataset(self.tickets, target, {**self.preparation, "triage_taxonomy": taxonomy})
        inputs, gold, manifest = load_job(target, "triage", "held_out")
        self.assertTrue(manifest["taxonomy"]["approved"])
        self.assertTrue(all(row["review"]["status"] == "draft" for row in gold.values()))
        self.assertTrue(all(row["labels"]["urgency"] in {"ordinary", "urgent"} for row in gold.values()))
        self.assertEqual(inputs[0]["input"]["task_definition"]["urgencies"], ["ordinary", "urgent"])

    def test_documented_cli_preparation_review_fake_run_and_api_plan(self):
        project = Path(__file__).resolve().parents[1]

        def command(module, *arguments):
            result = subprocess.run([sys.executable, "-m", module, *map(str, arguments)],
                                    cwd=project, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        source_split = self.split_file.read_bytes()
        tickets = self.root / "cli-smoke-tickets"
        command("scripts.make_smoke_tickets", "--source", self.source, "--output", tickets,
                "--split-manifest", self.split_file)
        self.assertEqual(self.split_file.read_bytes(), source_split)
        prepared = self.root / "cli-prepared"
        command("scripts.prepare_evaluation", "--tickets", tickets, "--output", prepared,
                "--config", project / "configs" / "evaluation.smoke.json")
        sidecar = self.root / "cli-review.jsonl"
        command("scripts.review_evaluation", "export", "--dataset", prepared, "--output", sidecar)
        human_output = command("scripts.review_evaluation", "inspect", "--dataset", prepared, "--job", "actions")
        self.assertIn("gold_for_human_review", human_output)
        reviewed = self.root / "cli-reviewed"
        command("scripts.review_evaluation", "apply", "--dataset", prepared, "--reviews", sidecar, "--output", reviewed)
        _, gold, _ = load_job(reviewed, "triage", "held_out")
        self.assertTrue(all(row["review"]["status"] == "draft" for row in gold.values()))
        run = self.root / "cli-run"
        output = command("scripts.run_models", "--dataset", prepared, "--job", "triage", "--config",
                         project / "configs" / "model.fake.json", "--output", run, "--allow-draft")
        self.assertEqual(json.loads(output)["completed_predictions"], 4)
        planned_output = self.root / "no-api-run"
        plan = command("scripts.run_models", "--dataset", prepared, "--job", "reply", "--config",
                       project / "configs" / "model.frontier.example.json", "--output", planned_output, "--plan")
        self.assertEqual(json.loads(plan)["requests_made"], 0)
        self.assertFalse(planned_output.exists())


class ApiAdapterTests(unittest.TestCase):
    def test_api_gate_and_available_usage_are_preserved_without_credentials(self):
        calls = []
        config = {"backend": "chat_api", "model_id": "test-snapshot", "endpoint": "https://example.test/chat/completions",
                  "api_key_env": "F0_TEST_KEY", "parameters": {"max_completion_tokens": 16}}

        class FakeResponse(io.BytesIO):
            headers = {"x-request-id": "fake-request"}

        def opener(request, timeout):
            calls.append(json.loads(request.data))
            payload = {"id": "response-1", "model": "resolved-snapshot", "choices": [{"message": {"content": '{"response":"A draft"}'}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "cost": {"reported": "provider-value"}}
            return FakeResponse(json.dumps(payload).encode())

        with patch.dict(os.environ, {"F0_TEST_KEY": "never-log-this-test-secret"}):
            with self.assertRaises(PermissionError):
                create_adapter(config, http_open=opener)
            self.assertEqual(calls, [])
            provider = create_adapter(config, allow_api=True, http_open=opener)
            result = provider["generate"]([{"request_id": "test", "job": "reply", "messages": [{"role": "user", "content": "Hi"}], "seed": 7}])[0]
        self.assertEqual(result["usage"]["completion_tokens"], 5)
        self.assertEqual(result["provider_metadata"]["returned_model"], "resolved-snapshot")
        self.assertEqual(result["provider_metadata"]["cost"], {"reported": "provider-value"})
        self.assertNotIn("never-log-this-test-secret", canonical(provider["metadata"]))
        self.assertNotIn("seed", calls[0])


class HuggingFaceAdapterTests(unittest.TestCase):
    def test_local_adapter_padding_revisions_batching_and_sampling_seeds(self):
        class Inputs(dict):
            def __init__(self, count):
                super().__init__(input_ids=SimpleNamespace(shape=(count, 5)),
                                 attention_mask=[SimpleNamespace(sum=lambda: 5) for _ in range(count)])

            def to(self, device):
                return self

        class Tokens:
            def tolist(self):
                return [101, 2]

        class Output:
            def __init__(self, count):
                self.count = count

            def __getitem__(self, key):
                return [Tokens() for _ in range(self.count)]

        tokenizer = MagicMock()
        tokenizer.pad_token_id = tokenizer.eos_token_id = 2
        tokenizer.side_effect = lambda texts, **kwargs: Inputs(len(texts))
        tokenizer.decode.return_value = '{"response":"Fake local response"}'
        tokenizer.apply_chat_template.return_value = "formatted chat"
        model = MagicMock()
        model.to.return_value = model
        model.eval.return_value = model
        model.config = SimpleNamespace(max_position_embeddings=128, _commit_hash="resolved-sha")
        model.generate.side_effect = lambda **kwargs: Output(kwargs["input_ids"].shape[0])
        torch = SimpleNamespace(
            __version__="fake-torch", float16="fp16", float32="fp32", bfloat16="bf16",
            manual_seed=Mock(), inference_mode=nullcontext,
            cuda=SimpleNamespace(is_available=lambda: False, synchronize=Mock(), empty_cache=Mock()),
        )
        loaders = SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=Mock(return_value=tokenizer)),
            AutoModelForCausalLM=SimpleNamespace(from_pretrained=Mock(return_value=model)),
        )
        config = {"backend": "huggingface", "model_id": "local-test", "revision": "fixed-sha",
                  "prompt_style": "base", "parameters": {"max_new_tokens": 8, "do_sample": False},
                  "max_input_tokens": 32}
        requests = [{"request_id": str(seed), "job": "reply", "messages": [{"role": "user", "content": "test"}], "seed": seed} for seed in (3, 4)]
        with patch.dict("sys.modules", {"torch": torch, "transformers": loaders}), patch("evaluation.models.importlib.metadata.version", return_value="4.57-test"):
            provider = create_adapter(config)
            results = provider["generate"](requests)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(row["error"] is None for row in results))
            self.assertEqual(results[0]["usage"], {"prompt_tokens": 5, "completion_tokens": 2})
            self.assertEqual(results[0]["provider_metadata"]["batch_size"], 2)
            self.assertEqual(provider["metadata"]["resolved_revision"], "resolved-sha")
            self.assertEqual(model.generate.call_count, 1)
            self.assertTrue(loaders.AutoTokenizer.from_pretrained.call_args.kwargs["local_files_only"])
            self.assertEqual(loaders.AutoTokenizer.from_pretrained.call_args.kwargs["padding_side"], "left")
            self.assertEqual(loaders.AutoModelForCausalLM.from_pretrained.call_args.kwargs["revision"], "fixed-sha")
            self.assertFalse(loaders.AutoModelForCausalLM.from_pretrained.call_args.kwargs["trust_remote_code"])
            tokenizer.apply_chat_template.assert_not_called()
            self.assertIn("Assistant:\n", tokenizer.call_args.args[0][0])
            model.generate.reset_mock()
            torch.manual_seed.reset_mock()
            sampled = create_adapter({**config, "parameters": {"max_new_tokens": 8, "do_sample": True}})
            sampled["generate"](requests)
            self.assertEqual(model.generate.call_count, 2)
            self.assertEqual([call.args[0] for call in torch.manual_seed.call_args_list], [3, 4])


if __name__ == "__main__":
    unittest.main()
