"""Real MCP stdio transport, with software-only serial/USB fixtures.

Neither test enumerates physical ports nor opens a PLC connection. Successful
fixture endpoint responses are software evidence, never hardware acceptance.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

try:
    from mcp import Client, StdioServerParameters
except ImportError:
    Client = StdioServerParameters = None


ROOT = Path(__file__).resolve().parents[2]
TARGET = dict(vendor="delta", family="DVP", model="DVP14SS211R", toolchain="dvpserial")
TOOLS = {"environment.probe", "target.resolve", "capabilities.list", "workspace.create",
         "workspace.status", "workspace.import", "adapter.invoke", "evidence.read",
         "workflow.start", "workflow.run", "workflow.status"}

SERVER = r'''
from types import SimpleNamespace
from universal_plc.service import Service
from universal_plc.mcp_server import create_server

class SoftwareSerial:
    def __init__(self, **kwargs):
        assert kwargs['port'] is None
        self.port = None
        self.dtr = self.rts = True
        self.request = None
        self.pending = b''
        self.timeout = kwargs['timeout']
    def open(self):
        assert self.port == 'COM6' and self.dtr is False and self.rts is False
    def reset_input_buffer(self): pass
    def write(self, request):
        raw = bytes.fromhex(request[1:-2].decode('ascii'))
        assert raw[1] in (1, 3), 'PLC write function is forbidden'
        self.request = raw
        return len(request)
    def read(self, size):
        assert size == 1
        if self.pending:
            chunk, self.pending = self.pending[:size], self.pending[size:]
            return chunk
        station, function = self.request[:2]
        count = int.from_bytes(self.request[4:6], 'big')
        payload = bytes((count + 7) // 8 if function == 1 else count * 2)
        body = bytes([station, function, len(payload)]) + payload
        self.pending = b':' + (body + bytes([(-sum(body)) & 255])).hex().upper().encode('ascii') + b'\r\n'
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk
    def close(self): pass

service = Service.from_environment()
adapter = service.adapters.get('delta.dvpserial')
adapter.ports_provider = lambda: [SimpleNamespace(device='COM6', vid=0x05DD, pid=0x0100,
    hwid='FAKE USB fixture only', description='Software fixture')]
adapter.serial_factory = SoftwareSerial
create_server(service).run(transport='stdio')
'''


@unittest.skipIf(Client is None, "mcp SDK2.1.1 is required for real stdio tests")
class McpStdioTests(unittest.TestCase):
    async def call(self, client, name, arguments):
        response = await client.call_tool(name, arguments)
        self.assertFalse(response.is_error, (name, response))
        self.assertIsInstance(response.structured_content, dict)
        return response.structured_content

    def params(self, directory, enabled):
        folder = Path(directory)
        config = dict(enabled=enabled, port="COM6", baudrate=9600, station=1,
                      expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                      expected_vid=0x05DD, expected_pid=0x0100,
                      route="directlink_cdc", allow_read_probe=True)
        config_path = folder / "software-fixture-config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["UNIVERSAL_PLC_ROOT"] = str(folder / "runtime")
        env["UNIVERSAL_PLC_DVPSERIAL_CONFIG"] = str(config_path)
        env["UNIVERSAL_PLC_READ_ROOTS"] = str(folder)
        return StdioServerParameters(command=sys.executable, args=["-B", "-c", SERVER],
                                     cwd=ROOT, env=env)

    async def create_workspace(self, client):
        resolved = await self.call(client, "target.resolve", {"target": TARGET})
        self.assertEqual(resolved["adapter_id"], "delta.dvpserial")
        self.assertFalse(resolved["requires_selection"])
        self.assertFalse(resolved["native_verified"])
        workspace = await self.call(client, "workspace.create", {"target": TARGET})
        self.assertEqual(workspace["adapter_id"], "delta.dvpserial")
        self.assertFalse(workspace["native_project_created"])
        return workspace

    async def invoke(self, client, workspace, operation, arguments=None):
        return await self.call(client, "adapter.invoke",
                               dict(workspace_id=workspace["workspace_id"], operation=operation,
                                    arguments=arguments or {},
                                    expected_context_digest=workspace["context_digest"]))

    def test_real_stdio_routing_capabilities_inventory_and_blocked_transfer(self):
        async def scenario(directory):
            async with Client(self.params(directory, enabled=False), read_timeout_seconds=10) as client:
                listed = await client.list_tools()
                self.assertEqual({tool.name for tool in listed.tools}, TOOLS)
                workspace = await self.create_workspace(client)
                capabilities = await self.call(client, "capabilities.list", {"target": TARGET})
                ops = {item["operation"]: item for item in capabilities["adapters"]["delta.dvpserial"]}
                self.assertEqual(ops["target.read"]["implementation"], "available")
                self.assertEqual(ops["deployment.execute"]["qualification"], "guard_only")
                self.assertFalse(ops["deployment.execute"]["program_transfer_supported"])
                inventory = await self.invoke(client, workspace, "serial.ports")
                self.assertEqual(inventory["result"]["ports"][0]["port"], "COM6")
                self.assertEqual(inventory["result"]["ports"][0]["description"], "Software fixture")
                for operation in ("project.upload", "deployment.prepare", "deployment.execute"):
                    returned = await self.invoke(client, workspace, operation)
                    self.assertEqual(returned["result"]["code"], "PROGRAM_TRANSFER_UNAVAILABLE")
                    self.assertFalse(returned["result"]["plc_program_changed"])
                    evidence = await self.call(client, "evidence.read",
                                               dict(workspace_id=workspace["workspace_id"],
                                                    evidence_id=returned["evidence_id"]))
                    self.assertEqual(evidence["adapter_id"], "delta.dvpserial")
                    self.assertEqual(evidence["result"]["status"], "blocked")
                run = await self.call(client, "workflow.start",
                                      dict(workspace_id=workspace["workspace_id"], workflow="W5"))
                request = dict(workspace_id=workspace["workspace_id"], run_id=run["run_id"], steps=[
                    dict(operation="target.connect", arguments={}),
                    dict(operation="deployment.execute", arguments={})])
                failed = await self.call(client, "workflow.run", request)
                self.assertEqual(failed["status"], "failed")
                self.assertEqual(len(failed["completed"]), 1)
                self.assertEqual(failed["completed"][0]["result"]["code"], "SESSION_NOT_ENROLLED")
                before = len(list((Path(directory) / "runtime").rglob("*.intent.json")))
                repeated = await self.call(client, "workflow.run", request)
                self.assertEqual(repeated, failed)
                self.assertEqual(len(list((Path(directory) / "runtime").rglob("*.intent.json"))), before)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_real_stdio_w5_observation_fixture_and_disconnect(self):
        async def scenario(directory):
            async with Client(self.params(directory, enabled=True), read_timeout_seconds=10) as client:
                workspace = await self.create_workspace(client)
                run = await self.call(client, "workflow.start",
                                      dict(workspace_id=workspace["workspace_id"], workflow="W5"))
                result = await self.call(client, "workflow.run",
                                         dict(workspace_id=workspace["workspace_id"], run_id=run["run_id"], steps=[
                                             dict(operation="target.observe.start", arguments={}),
                                             dict(operation="target.observe.read", arguments={}),
                                             dict(operation="target.observe.stop", arguments={})]))
                self.assertEqual(result["status"], "steps_returned")
                self.assertEqual(len(result["completed"]), 3)
                for record in result["completed"][:2]:
                    backend = record["result"]
                    self.assertTrue(backend["endpoint_verified"])
                    self.assertFalse(backend["exact_model_verified"])
                    self.assertFalse(backend["running"])
                    self.assertNotIn("temperature_c", backend)
                    self.assertIn("capacity_steps", backend)
                    for trace in backend["trace"]:
                        tx = bytes.fromhex(trace["tx_hex"])
                        self.assertIn(int(tx[3:5], 16), (1, 3))
                self.assertFalse(result["completed"][2]["result"]["connected"])
                status = await self.call(client, "workflow.status",
                                         dict(workspace_id=workspace["workspace_id"], run_id=run["run_id"]))
                self.assertEqual(status, result)

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))


if __name__ == "__main__":
    unittest.main()
