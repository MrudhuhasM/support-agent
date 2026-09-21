"""Generate a few offline draft tickets while reusing an existing order split."""

import argparse
import json
from pathlib import Path

from evaluation.files import load_json
from tickets.generate import ROOT, generate_tickets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "olist.sqlite")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "tickets.smoke.json")
    parser.add_argument("--allow-new-split", action="store_true", help="Explicitly create a provisional smoke-only split when none exists")
    args = parser.parse_args()
    config = load_json(args.config)
    if args.split_manifest.exists():
        split = load_json(args.split_manifest)
        config["split_seed"] = split["split_seed"]
        config["held_out_fraction"] = split["held_out_fraction"]
    elif not args.allow_new_split:
        parser.error("Reuse an existing order split, or explicitly opt into a provisional smoke-only split with --allow-new-split")
    result = generate_tickets(args.source, args.output, args.split_manifest, config)
    print(json.dumps({"counts": result["counts"], "status": "draft_smoke_only", "source_sha256": result["source_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
