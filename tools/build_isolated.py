#!/usr/bin/env python3
"""Build the native macOS identity and the network-disabled worker image."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
APP = BUILD / "Web Search Network.app"


def run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-image", action="store_true", help="Reuse an already built image")
    parser.add_argument("--image", default="local-web-search-isolated:dev")
    options = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("This Little Snitch integration requires macOS")
    docker = shutil.which("docker")
    if not docker:
        raise SystemExit("Install and start Docker Desktop first")
    native = BUILD / "native"
    native.mkdir(parents=True, exist_ok=True)
    executable = APP / "Contents/MacOS/WebSearchNetwork"
    executable.parent.mkdir(parents=True, exist_ok=True)
    run(
        "xcrun",
        "clang",
        "-O2",
        "-Wall",
        "-Wextra",
        "-Wno-deprecated-declarations",
        "-c",
        "native/broker.c",
        "-o",
        str(native / "broker.o"),
    )
    run(
        "xcrun",
        "swiftc",
        "-O",
        "-module-cache-path",
        str(native / "module-cache"),
        "-import-objc-header",
        "native/broker.h",
        "native/NetworkBroker.swift",
        str(native / "broker.o"),
        "-lcurl",
        "-o",
        str(executable),
    )
    with (APP / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleIdentifier": "local.web-search.network",
                "CFBundleExecutable": "WebSearchNetwork",
                "CFBundleName": "Web Search Network",
                "CFBundleDisplayName": "Web Search Network",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1",
                "LSUIElement": True,
            },
            stream,
        )
    run(
        "codesign",
        "--force",
        "--sign",
        "-",
        "--identifier",
        "local.web-search.network",
        "--options",
        "runtime",
        str(APP),
    )
    run(str(executable), "--self-test")
    if not options.skip_image:
        run(docker, "build", "-f", "docker/isolated/Dockerfile", "-t", options.image, ".")
    image = subprocess.check_output(
        [docker, "image", "inspect", options.image, "--format", "{{.Id}}"],
        text=True,
    ).strip()
    if not image.startswith("sha256:"):
        raise SystemExit("Cannot pin the built runtime image")
    endpoint = subprocess.check_output(
        [docker, "context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}'],
        text=True,
    ).strip()
    if not endpoint.startswith("unix:///"):
        raise SystemExit("Isolation requires a local Docker Unix socket, not a remote engine")
    manifest = {
        "image": image,
        "docker": docker,
        "docker_endpoint": endpoint,
        "volume": "local-web-search-isolated-data",
        "version": 1,
    }
    path = BUILD / "isolation.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(temporary, path)
    rules = {
        "name": "Isolated Web Search",
        "description": "Only the dedicated native retrieval broker may access public web ports.",
        "rules": [
            {
                "action": "allow",
                "process": str(executable),
                "remote": "any",
                "direction": "outgoing",
                "protocol": "tcp",
                "ports": port,
                "notes": "Isolated Web Search native broker; retain process identity checking.",
            }
            for port in ("80", "443")
        ],
    }
    (BUILD / "Web Search Network.lsrules").write_text(json.dumps(rules, indent=2) + "\n")
    print(f"Ready: {ROOT / 'web-search-isolated'}")


if __name__ == "__main__":
    main()
