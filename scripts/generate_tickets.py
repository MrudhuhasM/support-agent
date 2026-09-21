"""Offline by default: python -m scripts.generate_tickets --help."""

import argparse
import importlib
import json
from pathlib import Path

from tickets.generate import ROOT, generate_tickets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "olist.sqlite")
    parser.add_argument("--split-manifest", type=Path, default=ROOT / "data" / "tickets" / "order_splits.json")
    parser.add_argument("--provider", help="Opt-in adapter as module:function; receives prompt, model, parameters, seed")
    parser.add_argument("--allow-api", action="store_true", help="Explicitly allow a provider after reviewing its cost")
    arguments = parser.parse_args()
    config = json.loads(arguments.config.read_text(encoding="utf-8"))
    generator = None
    if arguments.provider:
        if not arguments.allow_api or config["message_generation"]["mode"] != "provider":
            parser.error("A provider requires provider mode and --allow-api after cost approval")
        module_name, separator, function_name = arguments.provider.partition(":")
        if not separator:
            parser.error("--provider must be module:function")
        generator = getattr(importlib.import_module(module_name), function_name)
    manifest = generate_tickets(
        arguments.source, arguments.output, arguments.split_manifest, config,
        message_generator=generator, allow_api=arguments.allow_api,
    )
    print(json.dumps({"output": str(arguments.output), "counts": manifest["counts"],
                      "unresolved_design_rules": manifest["unresolved_design_rules"]}, indent=2))


if __name__ == "__main__":
    main()
