from __future__ import annotations

import json
import re
from typing import Any


class ModelError(RuntimeError):
    pass


class OpenAICompatibleModelClient:
    """Small chat-completions client, intentionally independent of a provider SDK."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        timeout_seconds: float = 90.0,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx is required; install the project dependencies") from exc
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(timeout=timeout_seconds, headers=headers)

    async def close(self) -> None:
        await self._client.aclose()

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.model:
            raise ModelError("WEB_SEARCH_MODEL_ID is not configured")
        schema_instruction = (
            "Return exactly one JSON object matching this JSON Schema. "
            "Do not wrap it in Markdown.\n" + json.dumps(schema, ensure_ascii=False)
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system}\n\n{schema_instruction}"},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        try:
            response = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise ModelError(f"Model request failed for {schema_name}: {exc}") from exc
        if not isinstance(content, str):
            raise ModelError(f"Model returned non-text content for {schema_name}")
        try:
            parsed = _parse_json_object(content)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelError(f"Model returned invalid JSON for {schema_name}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ModelError(f"Model returned a non-object for {schema_name}")
        return parsed


def _parse_json_object(content: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(stripped[start : end + 1])
