from __future__ import annotations

import unittest

from mcp.client import Client

from web_research.server import mcp


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_exposes_exactly_one_structured_tool(self) -> None:
        async with Client(mcp) as client:
            result = await client.list_tools()
        self.assertEqual([tool.name for tool in result.tools], ["web_search"])
        tool = result.tools[0]
        self.assertEqual(tool.input_schema["required"], ["query"])
        self.assertIn("answer_markdown", tool.output_schema["properties"])


if __name__ == "__main__":
    unittest.main()
