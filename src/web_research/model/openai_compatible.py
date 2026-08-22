from __future__ import annotations

from typing import Any

from .errors import ModelError
from .json_response import parse_json_object, schema_instruction


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
        response_instruction = schema_instruction(schema)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system}\n\n{response_instruction}"},
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
            parsed = parse_json_object(content)
        except (TypeError, ValueError) as exc:
            raise ModelError(f"Model returned invalid JSON for {schema_name}: {exc}") from exc
        return parsed
