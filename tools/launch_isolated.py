#!/usr/bin/env python3
"""Start the installed native broker; never fall back to the unrestricted server."""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    manifest = ROOT / "build/isolation.json"
    executable = ROOT / "build/Web Search Network.app/Contents/MacOS/WebSearchNetwork"
    if not manifest.exists() or not executable.exists():
        raise SystemExit(
            "Build the isolated runtime first: .venv/bin/python tools/build_isolated.py"
        )
    config = json.loads(manifest.read_text())
    audit = ROOT / ".web-search-data/isolation"
    audit.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = audit / "network.jsonl"
    # Rotate between launches, keeping at most ~10 MB of domain-only records.
    if log.exists() and log.stat().st_size > 5_000_000:
        os.replace(log, log.with_suffix(".previous.jsonl"))
    os.execv(
        str(executable),
        [
            str(executable),
            config["docker"],
            config["image"],
            config["volume"],
            str(log),
            config["docker_endpoint"],
        ],
    )


if __name__ == "__main__":
    main()
