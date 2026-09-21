"""Small, strict JSONL and provenance helpers."""

import hashlib
import json
import os
from pathlib import Path


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    records = []
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                raise ValueError(f"Blank JSONL record: {path}:{line_number}")
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"Expected JSON object: {path}:{line_number}")
            records.append(record)
    return records


def write_json(path: Path, value: object) -> None:
    """Atomically finalize a file within a new version/run directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        output.write(canonical(value) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def write_jsonl(path: Path, records: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(canonical(record) + "\n")


def append_jsonl(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(canonical(record) + "\n")
        output.flush()
        os.fsync(output.fileno())


def replace_jsonl(path: Path, records: list[dict]) -> None:
    """Atomic export within an owned model run; never use on source datasets."""
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(canonical(record) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def read_journal(path: Path) -> list[dict]:
    """Recover only a torn final write in a model-run journal, never datasets."""
    path = Path(path)
    if not path.exists():
        return []
    content = path.read_bytes()
    if content and not content.endswith(b"\n"):
        start = content.rfind(b"\n") + 1
        tail = content[start:]
        try:
            json.loads(tail)
        except (ValueError, UnicodeDecodeError):
            with path.with_name(path.name + ".recovered_tail").open("ab") as backup:
                backup.write(tail)
            with path.open("r+b") as output:
                output.truncate(start)
        else:
            with path.open("ab") as output:
                output.write(b"\n")
    return read_jsonl(path)
