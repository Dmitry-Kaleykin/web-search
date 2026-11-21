#!/usr/bin/env python3
"""Measure the transport identity of the installed native broker.

The host-side probe in ``transport_fingerprint.py`` parses a ClientHello from a local server, and
the broker refuses loopback by policy: only public destinations leave it. This tool therefore asks
public fingerprinting services what they saw, and it drives the *installed* binary, so a result
describes the deployment rather than the checkout.

Nothing here is a claim about whether a site accepts a request. It is the wire identity a detection
stack can observe, measured from the same executable Little Snitch is asked to allow.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BROKER = ROOT / "build/Web Search Network.app/Contents/MacOS/WebSearchNetwork"
# The reader's own UA, so the measured header set is the one the reader sends, not a test artefact.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
PROBES = ("https://tls.peet.ws/api/all",)
CHROME_H2 = "1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p"
FIELDS = (
    "http_version",
    "ja3_hash",
    "ja4",
    "peetprint",
    "ciphers",
    "grease_fields",
    "encrypted_client_hello",
    "akamai_h2",
    "chrome_h2_for_comparison",
    "http2_settings",
    "http2_window_update",
    "pseudo_header_order",
    "header_order",
    "tcp",
)


def fetch(url: str) -> dict[str, Any]:
    request = {
        "url": url,
        "method": "GET",
        "headers": {"accept": "application/json", "user-agent": USER_AGENT},
    }
    completed = subprocess.run(
        [str(BROKER), "--test-transport"],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if not completed.stdout.strip():
        raise SystemExit(f"broker returned nothing: {completed.stderr.strip()[:400]}")
    row = json.loads(completed.stdout.splitlines()[0])
    if "body" not in row:
        raise SystemExit(f"broker error: {row.get('error') or completed.stderr[:400]}")
    return {**row, "json": json.loads(base64.b64decode(row["body"]))}


def is_grease(value: int) -> bool:
    """RFC 8701 reserves 0x0a0a, 0x1a1a, ... 0xfafa for GREASE."""
    high, low = value >> 8, value & 0xFF
    return high == low and (low & 0x0F) == 0x0A


def summarise(row: dict[str, Any]) -> dict[str, Any]:
    payload = row["json"]
    tls = payload.get("tls", {})
    http2 = payload.get("http2", {})
    tcp = payload.get("tcpip", {})
    frames = http2.get("sent_frames", [])
    headers = [
        header
        for frame in frames
        if frame.get("frame_type") == "HEADERS"
        for header in frame.get("headers", [])
    ]
    # The ja3 field is the numeric list; peet's cipher array is sometimes names instead.
    fields = (tls.get("ja3") or ",").split(",")
    ciphers = [int(value, 16) for value in fields[1].split("-") if value]
    extensions = [
        int(value) for value in (fields[2] if len(fields) > 2 else "").split("-") if value
    ]
    return {
        "status": row.get("status"),
        "http_version": payload.get("http_version"),
        "ja3_hash": tls.get("ja3_hash"),
        "ja4": tls.get("ja4"),
        "peetprint": tls.get("peetprint"),
        "ciphers": len(ciphers),
        # Zero here means the client never randomises the Hello, which is itself a fingerprint.
        "grease_fields": sum(is_grease(value) for value in ciphers + extensions),
        "encrypted_client_hello": any(
            "encrypted_client_hello" in entry["name"] for entry in tls.get("extensions", [])
        ),
        "akamai_h2": http2.get("akamai_fingerprint"),
        "chrome_h2_for_comparison": CHROME_H2,
        "http2_settings": [
            setting
            for frame in frames
            if frame.get("frame_type") == "SETTINGS"
            for setting in frame.get("settings", [])
        ],
        "http2_window_update": next(
            (
                frame.get("increment")
                for frame in frames
                if frame.get("frame_type") == "WINDOW_UPDATE"
            ),
            None,
        ),
        "pseudo_header_order": [
            header.split(":")[1].strip() for header in headers if header.startswith(":")
        ],
        "header_order": [
            header.split(":")[0].strip() for header in headers if not header.startswith(":")
        ],
        "tcp": {
            key: value
            for key, value in (tcp.get("tcp_syn") or tcp.get("tcp") or {}).items()
            if key in {"ttl", "window", "window_scale", "mss", "df", "option_order"}
        },
        "extensions": [entry["name"] for entry in tls.get("extensions", [])],
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=2, help="Confirm the identity is stable")
    args = parser.parse_args(argv)
    if not BROKER.exists():
        raise SystemExit(f"{BROKER} is not installed; run tools/build_isolated.py")
    for attempt in range(max(1, args.repeat)):
        for url in PROBES:
            summary = summarise(fetch(url))
            print(f"--- probe {attempt + 1}: {url} (status {summary['status']})")
            for field in FIELDS:
                print(f"{field:26} {json.dumps(summary[field])}")
            print(f"{'extensions':26} {', '.join(summary['extensions'])}")
    print(
        "\nGREASE and encrypted_client_hello are expected from Chrome and absent from Apple's "
        "TLS stack; that difference is a property of the transport, not a site decision.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
