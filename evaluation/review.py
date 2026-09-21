"""Human review sidecars produce new versions; source datasets stay frozen."""

import copy
import shutil
from pathlib import Path

from evaluation.contracts import JOBS, validate_gold
from evaluation.data import SPLITS, dataset_paths, load_job, verify_artifacts
from evaluation.files import file_hash, fingerprint, read_jsonl, write_json, write_jsonl


def export_review(dataset: Path, output: Path, *, reference_predictions: Path | None = None,
                  split: str = "held_out") -> None:
    dataset, output = Path(dataset).resolve(), Path(output).resolve()
    if output.is_relative_to(dataset):
        raise ValueError("Review sidecars must be outside the frozen dataset")
    manifest = verify_artifacts(dataset)
    references = {}
    if reference_predictions is not None:
        for row in read_jsonl(reference_predictions):
            if row["job"] == "reply" and row["status"] == "ok":
                references[row["example_id"]] = row
    records = []
    for job in JOBS:
        inputs, gold, _ = load_job(dataset, job, split)
        for visible in inputs:
            original = gold[visible["example_id"]]
            labels = copy.deepcopy(original["labels"])
            reference = references.get(visible["example_id"])
            if reference is not None:
                if reference.get("dataset_id") != manifest["dataset_id"] or reference.get("input_record_sha256") != fingerprint(visible):
                    raise ValueError("Reference predictions do not match this dataset's model inputs")
                labels["reference_response"] = reference["parsed_prediction"]["response"]
                labels["reference_status"] = "draft"
            records.append({
                "example_id": visible["example_id"], "job": job, "split": split,
                "dataset_id": manifest["dataset_id"], "source_gold_sha256": fingerprint(original),
                "model_input": visible["input"], "hidden_context": original["hidden"],
                "proposed_labels": original["labels"], "corrected_labels": labels,
                "reference_provenance": reference,
                "decision": "pending", "reviewer": None, "notes": "",
            })
    write_jsonl(output, records)


def apply_reviews(dataset: Path, reviews: Path, output: Path) -> dict:
    dataset, reviews, output = Path(dataset).resolve(), Path(reviews).resolve(), Path(output).resolve()
    if output.is_relative_to(dataset):
        raise ValueError("Reviewed versions must be outside their frozen parent dataset")
    if output.exists():
        raise FileExistsError("Review application must create a new dataset version")
    manifest = verify_artifacts(dataset)
    patches = read_jsonl(reviews)
    by_id = {row["example_id"]: row for row in patches}
    if len(by_id) != len(patches):
        raise ValueError("Duplicate review records")
    review_hash = file_hash(reviews)
    replacements = {}
    seen = set()
    for job in JOBS:
        for split in SPLITS:
            inputs, gold, _ = load_job(dataset, job, split)
            input_by_id = {row["example_id"]: row for row in inputs}
            for example_id, original in gold.items():
                patch = by_id.get(example_id)
                if patch is None:
                    continue
                seen.add(example_id)
                if patch.get("split") != split or patch["dataset_id"] != manifest["dataset_id"] or patch["job"] != job or patch["source_gold_sha256"] != fingerprint(original):
                    raise ValueError("Review does not match this frozen record and split")
                if patch["decision"] == "pending":
                    continue
                if patch["decision"] not in {"verified", "rejected"} or not isinstance(patch.get("reviewer"), str) or not patch["reviewer"].strip() or not isinstance(patch.get("notes"), str) or not patch["notes"].strip():
                    raise ValueError("Review needs an explicit decision, reviewer, and rationale")
                updated = copy.deepcopy(original)
                updated["labels"] = copy.deepcopy(patch["corrected_labels"])
                updated["review"] = {
                    "status": patch["decision"], "reviewer": patch["reviewer"], "notes": patch["notes"],
                    "parent_gold_sha256": fingerprint(original), "review_file_sha256": review_hash,
                    "reference_provenance": patch.get("reference_provenance"),
                }
                validate_gold(updated, verified=patch["decision"] == "verified")
                if patch["decision"] == "verified" and job == "triage":
                    definition = input_by_id[example_id]["input"]["task_definition"]
                    for label, space in (("intent", "intents"), ("urgency", "urgencies"), ("route", "routes")):
                        if updated["labels"][label] not in definition[space]:
                            raise ValueError("Corrected label is outside the model-visible taxonomy; prepare a new taxonomy version")
                replacements[example_id] = updated
    if seen != set(by_id):
        raise ValueError("Review contains unknown example IDs")
    shutil.copytree(dataset, output)
    for job in JOBS:
        for split in SPLITS:
            _, path = dataset_paths(output, job, split)
            rows = read_jsonl(path)
            temporary = path.with_name(split + ".reviewed.jsonl")
            write_jsonl(temporary, [replacements.get(row["example_id"], row) for row in rows])
            temporary.replace(path)
    review_name = "reviews/" + review_hash + ".jsonl"
    review_target = output / review_name
    review_target.parent.mkdir(parents=True, exist_ok=True)
    if review_target.exists():
        if file_hash(review_target) != review_hash:
            raise ValueError("Review history fingerprint mismatch")
    else:
        shutil.copyfile(reviews, review_target)
    new_manifest = copy.deepcopy(manifest)
    new_manifest.update(
        dataset_id=fingerprint([manifest["dataset_id"], review_hash]),
        parent_dataset_id=manifest["dataset_id"], review_file_sha256=review_hash,
        review_code_sha256=file_hash(Path(__file__)),
        certification="Review status is explicit per example; no automatic certification",
    )
    for name in list(new_manifest["artifacts_sha256"]):
        new_manifest["artifacts_sha256"][name] = file_hash(output / name)
    new_manifest["artifacts_sha256"][review_name] = file_hash(review_target)
    write_json(output / "manifest.json", new_manifest)
    return new_manifest
