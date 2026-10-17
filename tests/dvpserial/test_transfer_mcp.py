"""Real SDK stdio/service/adapter transfer workflows against software SS2 only."""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    from mcp import Client, StdioServerParameters
except ImportError:
    Client = StdioServerParameters = None

if __package__:
    from .test_transfer import CAPACITY_BYTES, PAYLOAD, REVISION, Scenario, TARGET, sha
else:
    from test_transfer import CAPACITY_BYTES, PAYLOAD, REVISION, Scenario, TARGET, sha

ROOT = Path(__file__).resolve().parents[2]
SERVER = r'''
import json,os
from pathlib import Path
from test_transfer import SoftwareState,SoftwareTransport,Scenario,sha
import universal_plc.dvp_transfer as transfer
from universal_plc.service import Service
from universal_plc.mcp_server import create_server

state=SoftwareState()
state.fail_write=os.environ.get('SS2_FIXTURE_LOST_ACK')=='1'
service=Service.from_environment()
adapter=service.adapters.get('delta.dvpserial')
adapter.serial_factory=Scenario.forbid_serial
adapter.ports_provider=lambda:[]
adapter._read=lambda device,count:[int(state.running)] if device=='M1000' else [0]
transfer.NativeProgramTransport=lambda adapter:SoftwareTransport(adapter,state)
original=adapter.invoke
def observed(operation,args):
 result=original(operation,args)
 Path(os.environ['SS2_FIXTURE_AUDIT']).write_text(json.dumps(dict(events=state.events,
 writes=len(state.write_calls),image_sha256=sha(bytes(state.image)),running=state.running,
 parameters=state.parameters)),encoding='utf-8')
 return result
adapter.invoke=observed
create_server(service).run(transport='stdio')
'''


@unittest.skipIf(Client is None, "MCP SDK2.1.1 required for real stdio")
class TransferStdio(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Scenario(self.temp.name)
        self.config_path = self.fixture.root / "host-profile.json"
        self.config = dict(self.fixture.config, verified_contract=False)
        self.audit_path = self.fixture.root / "software-state.json"
        self.save_config()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def params(self, lost_ack=False):
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(Path(__file__).resolve().parent),
                                             str(ROOT / ".agent-state" / "deps"), env.get("PYTHONPATH", "")))
        env["UNIVERSAL_PLC_ROOT"] = str(self.fixture.root / "mcp-runtime")
        env["UNIVERSAL_PLC_READ_ROOTS"] = str(self.fixture.root)
        env["UNIVERSAL_PLC_DVPSERIAL_CONFIG"] = str(self.config_path)
        env["SS2_FIXTURE_AUDIT"] = str(self.audit_path)
        env["SS2_FIXTURE_LOST_ACK"] = "1" if lost_ack else "0"
        return StdioServerParameters(command=sys.executable, args=["-B", "-c", SERVER], cwd=ROOT, env=env)

    def audit(self):
        return json.loads(self.audit_path.read_text("utf-8"))

    async def call(self, client, tool, args):
        response = await client.call_tool(tool, args)
        self.assertFalse(response.is_error, (tool, response))
        self.assertIsInstance(response.structured_content, dict)
        return response.structured_content

    async def create(self, client):
        resolved = await self.call(client, "target.resolve", {"target": TARGET})
        self.assertEqual(resolved["adapter_id"], "delta.dvpserial")
        self.assertFalse(resolved["native_verified"])
        return await self.call(client, "workspace.create", dict(target=TARGET,
                               baseline_path=str(self.fixture.source), expected_sha256=self.fixture.source_sha256,
                               task_id="ss2-mcp-fixture", revision=REVISION))

    async def invoke(self, client, ws, operation, **args):
        return await self.call(client, "adapter.invoke", dict(workspace_id=ws["workspace_id"], operation=operation,
                               arguments=args, expected_context_digest=ws["context_digest"]))

    async def step(self, client, ws, operation, workflow="W5", **args):
        run = await self.call(client, "workflow.start", dict(workspace_id=ws["workspace_id"], workflow=workflow))
        request = dict(workspace_id=ws["workspace_id"], run_id=run["run_id"],
                       steps=[dict(operation=operation, arguments=args)])
        returned = await self.call(client, "workflow.run", request)
        self.assertEqual(len(returned["completed"]), 1, returned)
        return returned, request

    async def qualify(self, client, ws):
        returned, _ = await self.step(client, ws, "program.contract.qualify", workflow="W1", output_dir="contracts")
        result = returned["completed"][0]["result"]
        self.assertTrue(result["ok"], result)
        files = self.fixture.root / "mcp-runtime" / "workspaces" / ws["workspace_id"] / "files"
        path = files / result["contract_path"]
        self.assertEqual(sha(path.read_bytes()), result["contract_sha256"])
        self.config.update(contract_path=str(path), contract_sha256=result["contract_sha256"], verified_contract=True)
        self.save_config()  # Host enrollment update; same real server reloads this current profile.

    async def plan(self, client, ws):
        await self.qualify(client, ws)
        backup, _ = await self.step(client, ws, "project.upload", output_dir="backups")
        data = backup["completed"][0]["result"]
        self.assertTrue(data["ok"], data)
        before = self.audit()
        prepared, _ = await self.step(client, ws, "deployment.prepare", input_path="baseline.dvp",
                                      expected_sha256=self.fixture.source_sha256, backup_path=data["backup_path"],
                                      expected_backup_sha256=data["backup_sha256"])
        result = prepared["completed"][0]["result"]
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.audit(), before, "prepare must cause zero transport operations")
        return result

    def test_real_stdio_v7_full_transaction_evidence_and_workflow_noreplay(self):
        async def run():
            async with Client(self.params(), read_timeout_seconds=25) as client:
                ws = await self.create(client)
                plan = await self.plan(client, ws)
                done, request = await self.step(client, ws, "deployment.execute", plan_path=plan["plan_path"],
                                                expected_sha256=plan["plan_sha256"])
                self.assertEqual(done["status"], "steps_returned")
                result = done["completed"][0]["result"]
                self.assertTrue(result["ok"], result)
                for flag in ("readback_verified", "program_verified", "parameters_preserved", "tail_preserved", "stop_verified"):
                    self.assertTrue(result[flag], result)
                observed = self.audit()
                expected = PAYLOAD + self.fixture.state.original[len(PAYLOAD):]
                self.assertEqual(observed["image_sha256"], sha(expected))
                self.assertEqual(observed["writes"], 1)
                self.assertFalse(observed["running"])
                self.assertEqual(len(expected), CAPACITY_BYTES)
                evidence = await self.call(client, "evidence.read", dict(workspace_id=ws["workspace_id"],
                                           evidence_id=done["completed"][0]["evidence_id"]))
                dependencies = evidence["implementation_identity"]["module_dependencies"]
                self.assertEqual(set(dependencies), {"dvp_native", "dvp_transfer", "dvp_controls"})
                self.assertEqual(evidence["context_digest"], ws["context_digest"])
                self.assertEqual(await self.call(client, "workflow.run", request), done)
                self.assertEqual(self.audit(), observed)
                repeated = await self.invoke(client, ws, "deployment.execute", plan_path=plan["plan_path"],
                                              expected_sha256=plan["plan_sha256"])
                self.assertEqual(repeated["result"]["code"], "PLAN_ALREADY_ADMITTED")
                self.assertEqual(self.audit()["writes"], 1)
                disconnected = await self.invoke(client, ws, "target.disconnect")
                self.assertFalse(disconnected["result"]["connected"])
        asyncio.run(run())

    def test_real_stdio_lost_ack_reconciles_without_resending_program(self):
        async def run():
            async with Client(self.params(lost_ack=True), read_timeout_seconds=25) as client:
                ws = await self.create(client)
                plan = await self.plan(client, ws)
                failed, _ = await self.step(client, ws, "deployment.execute", plan_path=plan["plan_path"],
                                            expected_sha256=plan["plan_sha256"])
                result = failed["completed"][0]["result"]
                self.assertEqual(result["status"], "unknown_outcome")
                self.assertFalse(result["retry_allowed"])
                self.assertIsNone(result["plc_program_changed"])
                recovered, _ = await self.step(client, ws, "deployment.reconcile", workflow="W6",
                                               journal_path=result["journal_path"], expected_sha256=result["journal_sha256"])
                recovery = recovered["completed"][0]["result"]
                self.assertTrue(recovery["ok"], recovery)
                self.assertEqual(recovery["program_outcome"], "candidate_current")
                self.assertTrue(recovery["stop_verified"])
                self.assertEqual(self.audit()["writes"], 1)
        asyncio.run(run())

    def test_host_profile_revocation_and_service_context_spoof_are_not_authority(self):
        async def run():
            async with Client(self.params(), read_timeout_seconds=25) as client:
                ws = await self.create(client)
                plan = await self.plan(client, ws)
                before = self.audit()
                self.config["user_present"] = False
                self.save_config()
                blocked = await self.invoke(client, ws, "deployment.execute", plan_path=plan["plan_path"],
                                             expected_sha256=plan["plan_sha256"],
                                             _execution_context=dict(workspace_id="fake", revision="obsolete-V5"))
                self.assertFalse(blocked["result"]["ok"])
                self.assertEqual(blocked["result"]["code"], "PROGRAM_TRANSFER_NOT_AUTHORIZED")
                self.assertEqual(self.audit(), before)
                stale = await client.call_tool("adapter.invoke", dict(workspace_id=ws["workspace_id"],
                                     operation="deployment.execute", arguments={}, expected_context_digest="0" * 64))
                self.assertTrue(stale.is_error)
                self.assertEqual(self.audit(), before)
                self.config["user_present"] = True
                self.save_config()
                invoked = await self.invoke(client, ws, "deployment.execute", plan_path=plan["plan_path"],
                                             expected_sha256=plan["plan_sha256"], _execution_context={"revision": "obsolete-V5"})
                self.assertTrue(invoked["result"]["ok"], invoked)
                self.assertEqual(self.audit()["writes"], 1)
        asyncio.run(run())


@unittest.skipIf(Client is None, "MCP SDK2.1.1 required for transfer runner import")
class RunnerAdmission(unittest.TestCase):
    def test_existing_report_is_rejected_before_any_mcp_client(self):
        spec = importlib.util.spec_from_file_location("software_transfer_runner", ROOT / "tools" / "dvp_transfer_e2e.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "existing.json"
            output.write_bytes(b"IMMUTABLE PREVIOUS EVIDENCE")
            args = ["runner", "--config", "unused", "--runtime-dir", directory, "--evidence-path", str(output)]
            with patch.object(sys, "argv", args), patch.object(module, "Client") as client, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(), 1)
                client.assert_not_called()
            self.assertEqual(output.read_bytes(), b"IMMUTABLE PREVIOUS EVIDENCE")


if __name__ == "__main__":
    unittest.main()
