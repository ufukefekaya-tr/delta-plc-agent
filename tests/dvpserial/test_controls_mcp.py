"""Real SDK stdio/workflow coverage with the independent SOFTWARE_ONLY wire PLC."""
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
    from .test_controls import ROOT, REVISION, TARGET, Scenario, SoftwarePort, save
else:
    from test_controls import ROOT, REVISION, TARGET, Scenario, SoftwarePort, save


SERVER = r'''
import json,os
from pathlib import Path
from test_controls import SoftwarePLC,SoftwarePort,save
from universal_plc.service import Service
from universal_plc.mcp_server import create_server
state=SoftwarePLC()
if os.environ.get('TEMPERATURE_FIXTURE_LOST_ACK')=='1':
 state.drop_ack.add((100,1))
if os.environ.get('PARAMETER_FIXTURE_LOST_ACK')=='1':
 state.drop_parameter_ack.add((2000,2000))
if os.environ.get('PARAMETER_FIXTURE_READBACK_LOSS')=='1':
 state.fail_parameter_readback=True
service=Service.from_environment()
adapter=service.adapters.get('delta.dvpserial')
adapter.serial_factory=state.factory
adapter.ports_provider=lambda:[SoftwarePort()]
original=adapter.invoke
def observed(operation,args):
 if operation=='machine.reconcile':state.fail_parameter_readback=False
 result=original(operation,args)
 save(Path(os.environ['TEMPERATURE_FIXTURE_AUDIT']),dict(mutations=state.mutations,
   parameter_mutations=state.parameter_mutations,frames=[f.hex() for f in state.frames],m=state.m,d=state.d))
 return result
adapter.invoke=observed
create_server(service).run(transport='stdio')
'''


@unittest.skipIf(Client is None, "MCP SDK2.1.1 required; skipped tests are not live evidence")
class TemperatureStdio(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Scenario(self.temp.name)
        self.config_path = self.s.root/"host-profile.json"
        self.audit_path = self.s.root/"software-wire-audit.json"
        self.save_config()

    def save_config(self):
        save(self.config_path,self.s.config)

    def params(self,lost_ack=False,parameter_lost_ack=False,parameter_readback_loss=False):
        env=os.environ.copy()
        env["PYTHONPATH"]=os.pathsep.join((str(ROOT/"src"),str(Path(__file__).resolve().parent),env.get("PYTHONPATH","")))
        env["UNIVERSAL_PLC_ROOT"]=str(self.s.root/"mcp-runtime")
        env["UNIVERSAL_PLC_READ_ROOTS"]=str(self.s.root)
        env["UNIVERSAL_PLC_DVPSERIAL_CONFIG"]=str(self.config_path)
        env["TEMPERATURE_FIXTURE_AUDIT"]=str(self.audit_path)
        env["TEMPERATURE_FIXTURE_LOST_ACK"]="1" if lost_ack else "0"
        env["PARAMETER_FIXTURE_LOST_ACK"]="1" if parameter_lost_ack else "0"
        env["PARAMETER_FIXTURE_READBACK_LOSS"]="1" if parameter_readback_loss else "0"
        return StdioServerParameters(command=sys.executable,args=["-B","-c",SERVER],cwd=ROOT,env=env)

    def audit(self):
        return json.loads(self.audit_path.read_text("utf-8"))

    async def call(self,client,tool,args):
        response=await client.call_tool(tool,args)
        self.assertFalse(response.is_error,(tool,response))
        self.assertIsInstance(response.structured_content,dict)
        return response.structured_content

    async def create(self,client):
        workspace=await self.call(client,"workspace.create",dict(target=TARGET,
            baseline_path=str(self.s.source),expected_sha256=self.s.source_sha256,
            task_id="temperature-sdk-software-only",revision=REVISION))
        root=self.s.root/"mcp-runtime"/"workspaces"/workspace["workspace_id"]
        self.s.proof_data["context"]=dict(workspace_id=workspace["workspace_id"],task_id=workspace["task_id"],
              revision=REVISION,context_digest=workspace["context_digest"],workspace_root=str(root),
              baseline_sha256=self.s.source_sha256)
        self.s.repin_proof();self.save_config()
        return workspace

    async def invoke(self,client,workspace,operation,**args):
        operation,args=self.typed(operation,args)
        return await self.call(client,"adapter.invoke",dict(workspace_id=workspace["workspace_id"],
             operation=operation,arguments=args,expected_context_digest=workspace["context_digest"]))

    async def step(self,client,workspace,operation,workflow="W5",**args):
        operation,args=self.typed(operation,args)
        run=await self.call(client,"workflow.start",dict(workspace_id=workspace["workspace_id"],workflow=workflow))
        request=dict(workspace_id=workspace["workspace_id"],run_id=run["run_id"],
                     steps=[dict(operation=operation,arguments=args)])
        returned=await self.call(client,"workflow.run",request)
        self.assertEqual(len(returned["completed"]),1,returned)
        return returned,request

    @staticmethod
    def typed(operation,args):
        args=dict(args)
        if operation in ("temperature.read","temperature.status","temperature.observe","temperature.reconcile"):
            return "machine."+operation.split(".")[1],args
        if operation.startswith("temperature."):
            args["command"]=operation
            if "channel" in args:
                args["selector"]=args.pop("channel")
            if "pair" in args:
                args["selector"]=args.pop("pair")
            return "machine.command",args
        return operation,args

    def test_real_stdio_capabilities_pulse_workflow_evidence_and_no_replay(self):
        async def run():
            async with Client(self.params(),read_timeout_seconds=20) as client:
                workspace=await self.create(client)
                capabilities=await self.call(client,"capabilities.list",dict(target=TARGET))
                operations={item["operation"]:item for item in capabilities["adapters"]["delta.dvpserial"]}
                for operation in ("machine.profile","machine.command","machine.parameter.write","machine.read","machine.status","machine.observe","machine.reconcile"):
                    self.assertIn(operation,operations)
                    self.assertFalse(operations[operation]["live_verified"])
                done,request=await self.step(client,workspace,"temperature.channel.start",channel=1,
                                  command_id="sdk-software-only-pulse",_execution_context={"revision":"V5","context_digest":"fake"})
                self.assertEqual(done["status"],"steps_returned",done)
                record=done["completed"][0]
                self.assertTrue(record["result"]["ok"],record)
                self.assertEqual(self.audit()["mutations"],[[100,1],[100,0]])
                evidence=await self.call(client,"evidence.read",dict(workspace_id=workspace["workspace_id"],
                                    evidence_id=record["evidence_id"]))
                self.assertEqual(evidence["context_digest"],workspace["context_digest"])
                self.assertIn("dvp_controls",evidence["implementation_identity"]["module_dependencies"])
                before=self.audit()
                self.assertEqual(await self.call(client,"workflow.run",request),done)
                self.assertEqual(self.audit(),before)
                repeated=await self.invoke(client,workspace,"temperature.channel.start",channel=1,
                                          command_id="sdk-software-only-pulse")
                self.assertFalse(repeated["result"]["ok"],repeated)
                self.assertEqual(self.audit()["mutations"],before["mutations"])
                stale=await client.call_tool("adapter.invoke",dict(workspace_id=workspace["workspace_id"],
                       operation="machine.command",arguments={"command":"temperature.channel.start","selector":2,"command_id":"stale"},
                       expected_context_digest="0"*64))
                self.assertTrue(stale.is_error)
                self.assertEqual(self.audit()["mutations"],before["mutations"])
        asyncio.run(run())

    def test_real_stdio_profile_configuration_smoke_while_human_owns_port(self):
        async def run():
            async with Client(self.params(),read_timeout_seconds=20) as client:
                workspace=await self.create(client)
                self.s.config["user_present"]=False;self.s.config["heaters_isolated"]=False
                self.s.lease_data["owner"]="human";save(self.s.lease,self.s.lease_data);self.save_config()
                profile=await self.invoke(client,workspace,"machine.profile")
                self.assertTrue(profile["result"]["ok"],profile)
                self.assertFalse(profile["result"]["live_revision_verified"],profile)
                self.assertIn("temperature.contactor.start",profile["result"]["commands"])
                self.assertEqual(self.audit()["frames"],[])
                self.s.source.write_bytes(b"SOURCE DRIFT AFTER PROFILE ENROLLMENT")
                rejected=await self.invoke(client,workspace,"machine.profile")
                self.assertFalse(rejected["result"]["ok"],rejected)
                self.assertEqual(self.audit()["frames"],[])
        asyncio.run(run())

    def test_host_revocation_cannot_be_spoofed_and_unknown_revision_status_is_raw(self):
        async def run():
            async with Client(self.params(),read_timeout_seconds=20) as client:
                workspace=await self.create(client)
                self.s.config["user_present"]=False;self.save_config()
                denied=await self.invoke(client,workspace,"temperature.tuning.start",channel=1,
                        command_id="spoof-no-attendant",_execution_context=self.s.proof_data["context"])
                self.assertFalse(denied["result"]["ok"],denied)
                self.assertEqual(self.audit()["mutations"],[])
                self.s.config["user_present"]=True
                self.s.proof_data["revision"]="V7";self.s.repin_proof();self.save_config()
                returned,_=await self.step(client,workspace,"machine.observe")
                data=returned["completed"][0]["result"]
                self.assertTrue(data["ok"],data)
                self.assertFalse(data["live_revision_verified"],data)
                self.assertEqual(self.audit()["mutations"],[])
        asyncio.run(run())

    def test_real_stdio_named_parameter_lost_ack_readback_and_workflow_no_replay(self):
        self.s.install_test_parameters();self.save_config()
        async def run():
            async with Client(self.params(parameter_lost_ack=True),read_timeout_seconds=20) as client:
                workspace=await self.create(client)
                profile=await self.invoke(client,workspace,"machine.profile")
                self.assertIn("setpoint",profile["result"]["parameters"],profile)
                done,request=await self.step(client,workspace,"machine.parameter.write",parameter="setpoint",
                    value=2000,command_id="sdk-named-parameter",_execution_context={"revision":"caller-spoof"})
                result=done["completed"][0]["result"]
                self.assertTrue(result["ok"],result)
                self.assertTrue(result["readback_verified"],result)
                self.assertFalse(result["ack_verified"],result)
                self.assertEqual(self.audit()["parameter_mutations"],[[2000,2000]])
                before=self.audit()
                self.assertEqual(await self.call(client,"workflow.run",request),done)
                self.assertEqual(self.audit(),before)
                self.s.config["allow_machine_parameters"]=False;self.save_config()
                denied=await self.invoke(client,workspace,"machine.parameter.write",parameter="gain",value=20,
                      command_id="revoked-parameter",_execution_context=self.s.proof_data["context"])
                self.assertFalse(denied["result"]["ok"],denied)
                self.assertEqual(self.audit()["parameter_mutations"],before["parameter_mutations"])
        asyncio.run(run())

    def test_real_stdio_parameter_uncertainty_fences_m_then_readonly_w6_reconciles(self):
        self.s.install_test_parameters();self.save_config()
        async def run():
            async with Client(self.params(parameter_readback_loss=True),read_timeout_seconds=20) as client:
                workspace=await self.create(client)
                failed,_=await self.step(client,workspace,"machine.parameter.write",parameter="setpoint",
                                        value=2000,command_id="sdk-d-readback-loss")
                result=failed["completed"][0]["result"]
                self.assertEqual(result["status"],"unknown_outcome",result)
                blocked=await self.invoke(client,workspace,"machine.command",command="temperature.channel.start",
                                         selector=1,command_id="sdk-m-after-d-unknown")
                self.assertFalse(blocked["result"]["ok"],blocked)
                recovered,_=await self.step(client,workspace,"machine.reconcile",workflow="W6",
                                           journal_path=result["journal_path"])
                data=recovered["completed"][0]["result"]
                self.assertTrue(data["ok"],data)
                self.assertEqual(data["classification"],"candidate",data)
                self.assertEqual(self.audit()["parameter_mutations"],[[2000,2000]])
                self.assertEqual(self.audit()["mutations"],[])
        asyncio.run(run())

    def test_lost_ack_workflow_is_unknown_and_reconcile_never_reissues_on(self):
        async def run():
            async with Client(self.params(lost_ack=True),read_timeout_seconds=20) as client:
                workspace=await self.create(client)
                failed,request=await self.step(client,workspace,"temperature.channel.start",channel=1,
                                                command_id="sdk-lost-ack")
                result=failed["completed"][0]["result"]
                self.assertFalse(result["ok"],result)
                self.assertEqual(result["status"],"unknown_outcome",result)
                self.assertFalse(result["retry_allowed"],result)
                self.assertEqual(self.audit()["mutations"],[[100,1],[100,0]])
                before=self.audit()["mutations"]
                self.assertEqual(await self.call(client,"workflow.run",request),failed)
                recovered,_=await self.step(client,workspace,"temperature.reconcile",workflow="W6",
                                           journal_path=result["journal_path"])
                self.assertTrue(recovered["completed"][0]["result"]["ok"],recovered)
                self.assertEqual(self.audit()["mutations"],before)
        asyncio.run(run())


@unittest.skipIf(Client is None,"MCP SDK needed to import observation client")
class ObservationRunnerAdmission(unittest.TestCase):
    def test_existing_evidence_is_rejected_before_mcp_client_or_serial(self):
        spec=importlib.util.spec_from_file_location("machine_observation_client",ROOT/"tools/dvp_controls_observe.py")
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"immutable-evidence.json";path.write_bytes(b"IMMUTABLE PREVIOUS OBSERVATION")
            args=["runner","--config","unused","--runtime-dir",directory,"--evidence-path",str(path)]
            with patch.object(sys,"argv",args),patch.object(module,"Client") as client,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(),1)
                client.assert_not_called()
            self.assertEqual(path.read_bytes(),b"IMMUTABLE PREVIOUS OBSERVATION")


if __name__ == "__main__":
    unittest.main()
