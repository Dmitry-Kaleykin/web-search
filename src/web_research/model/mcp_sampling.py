from __future__ import annotations

import warnings
from typing import Any

from mcp import MCPDeprecationWarning
from mcp.server.mcpserver import Context
from mcp.types import SamplingMessage, TextContent

from .errors import ModelError
from .json_response import parse_json_object, schema_instruction


class MCPSamplingModelClient:
    """Delegates model calls to the connected MCP client's selected model."""

    def __init__(
        self,
        context: Context,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> None:
        self.context = context
        self.max_tokens = max_tokens
        self.temperature = temperature

    @staticmethod
    def supported(context: Context) -> bool:
        capabilities = context.client_capabilities
        return capabilities is not None and capabilities.sampling is not None

    async def close(self) -> None:
        return None

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.supported(self.context):
            raise ModelError("The connected MCP client does not support model sampling")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", MCPDeprecationWarning)
                result = await self.context.session.create_message(
                    messages=[SamplingMessage(role="user", content=TextContent(text=user))],
                    system_prompt=f"{system}\n\n{schema_instruction(schema)}",
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    include_context="none",
                    metadata={"schema_name": schema_name},
                    related_request_id=self.context.request_id,
                )
        except Exception as exc:
            raise ModelError(f"MCP client sampling failed for {schema_name}: {exc}") from exc

        blocks = result.content if isinstance(result.content, list) else [result.content]
        content = "\n\n".join(
            block.text for block in blocks if getattr(block, "type", None) == "text"
        ).strip()
        if not content:
            raise ModelError(f"MCP client returned no text for {schema_name}")
        try:
            return parse_json_object(content)
        except (TypeError, ValueError) as exc:
            raise ModelError(f"MCP client returned invalid JSON for {schema_name}: {exc}") from exc
