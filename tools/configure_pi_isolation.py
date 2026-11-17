#!/usr/bin/env python3
"""Switch only Pi's existing web-search profile to the verified isolated launcher."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".pi/agent/extensions/scoped-mcp/scoped-mcp.json",
    )
    options = parser.parse_args()
    path = options.config
    config = json.loads(path.read_text())
    server = config["$profiles"]["web-search"]["mcpServers"]["web-search"]
    server["command"] = str(ROOT / "web-search-isolated")
    server["requestTimeoutMs"] = 120_000
    for key in ("args", "env", "samplingAutoApprove"):
        server.pop(key, None)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(path.name + ".before-isolation-" + stamp)
    shutil.copy2(path, backup)
    temporary = path.with_name(path.name + ".isolation.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.chmod(path.stat().st_mode & 0o777)
    os.replace(temporary, path)
    print("Pi web-search profile now uses the isolated runtime.")
    print(f"Backup: {backup}")
    print("Restart Pi to reconnect the MCP server.")


if __name__ == "__main__":
    main()
