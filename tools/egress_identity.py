#!/usr/bin/env python3
"""Report the identity websites actually see from this deployment.

`tools/broker_identity.py` answers the TLS and HTTP2 question. This answers the cheaper one that
decides whether a policy change is even worth discussing: what address, what geolocation, and what
the site thinks your language and timezone are. Both drive the installed native broker, because the
answer about the deployed binary is the only one that describes what a website sees.

Nothing here is a claim about anonymity on its own. An IP that is not your home address is not
anonymous, and a datacenter exit that is not anonymous is not your home address either; what the
number does decide is whether a rented proxy would change the observer's view of *where* you are or
merely change the reputation of the address.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# Same broker plumbing as the wire-identity tool; no reason to duplicate the subprocess framing.
import broker_identity

ECHO = "https://ipinfo.io/json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--broker",
        type=pathlib.Path,
        default=pathlib.Path("build/Web Search Network.app/Contents/MacOS/WebSearchNetwork"),
    )
    parser.add_argument("--raw", action="store_true", help="print the echo service's own JSON")
    args = parser.parse_args()

    broker_identity.BROKER = args.broker
    payload = broker_identity.fetch(ECHO)["json"]
    if args.raw:
        print(json.dumps(payload, indent=2))
        return 0
    print(
        json.dumps(
            {
                "ip": payload.get("ip"),
                "country": payload.get("country"),
                "city": payload.get("city"),
                "org": payload.get("org"),
                # What a site infers beyond the address. `hosting` means the address belongs to a
                # provider that serves many tenants, which is what attracts a bot check.
                "org_suggests": "hosting" if _looks_hosted(payload.get("org", "")) else "access",
            },
            indent=2,
        )
    )
    return 0


def _looks_hosted(org: str) -> bool:
    terms = ("hosting", "cloud", "vps", "llc", "inc", "gmbh", "srl", "limited")
    return any(term in org.lower() for term in terms)


if __name__ == "__main__":
    raise SystemExit(main())
