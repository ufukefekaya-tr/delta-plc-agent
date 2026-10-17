from __future__ import annotations

import asyncio
import os
import sys
import unittest

from mcp import Client, StdioServerParameters

from axmcp_offline.constants import ALLOWED_OPERATIONS
from axmcp_offline.mcp_server import create_server
from axmcp_offline.replay import ReplayBackend
from axmcp_offline.service import OfflineService

from . import PACKAGE_ROOT, SRC_ROOT


class McpServerTests(unittest.TestCase):
    def test_in_memory_client_lists_and_calls_exact_tools(self) -> None:
        async def scenario() -> None:
            server = create_server(OfflineService(ReplayBackend.from_default()))
            async with Client(server) as client:
                listed = await client.list_tools()
                self.assertEqual(tuple(tool.name for tool in listed.tools), ALLOWED_OPERATIONS)
                for tool in listed.tools:
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertFalse(tool.annotations.destructive_hint)
                    self.assertTrue(tool.annotations.idempotent_hint)
                    self.assertFalse(tool.annotations.open_world_hint)
                called = await client.call_tool("source.read", {})
                self.assertFalse(called.is_error)
                self.assertEqual(called.structured_content["operation"], "source.read")
                self.assertFalse(called.structured_content["live_ax_verified"])

        asyncio.run(scenario())

    def test_real_stdio_subprocess_lists_and_calls_exact_tools(self) -> None:
        async def scenario() -> None:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SRC_ROOT)
            env["AXMCP_MODE"] = "recorded_replay"
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "axmcp_offline", "serve"],
                cwd=PACKAGE_ROOT,
                env=env,
            )
            async with Client(params, read_timeout_seconds=10) as client:
                listed = await client.list_tools()
                self.assertEqual(tuple(tool.name for tool in listed.tools), ALLOWED_OPERATIONS)
                called = await client.call_tool("environment.probe", {})
                self.assertFalse(called.is_error)
                self.assertEqual(called.structured_content["evidence_class"], "SIMULATED")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
