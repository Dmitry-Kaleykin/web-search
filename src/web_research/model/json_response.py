from __future__ import annotations

import json
import re
from typing import Any


def schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Return exactly one JSON object matching this JSON Schema. "
        "Do not wrap it in Markdown.\n" + json.dumps(schema, ensure_ascii=False)
    )


def parse_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise TypeError("response is not a JSON object")
    return parsed
