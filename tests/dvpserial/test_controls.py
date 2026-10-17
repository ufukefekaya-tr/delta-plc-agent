"""Neutralized software pulse/fault/observation tests; SOFTWARE_ONLY, never a PLC.

The wire fixture parses production bytes independently and models documented
request edges. No OS serial enumeration, physical USB, native program or vendor
compiler is used. Its revision proofs are expressly synthetic test inputs.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from universal_plc.dvp_serial import DvpSerialAdapter
from universal_plc.service import Service


ROOT = Path(__file__).resolve().parents[2]
TARGET = dict(vendor="delta", family="DVP", model="DVP14SS211R", toolchain="dvpserial")
REVISION = "SOFTWARE_R1"
IDENTITY = bytes.fromhex("000400000001000e0000")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_bytes((json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def packet(body):
    return b":" + (body + bytes([(-sum(body)) & 255])).hex().upper().encode() + b"\r\n"


class SoftwarePort:
    device = "COM6"
    vid = 0x05DD
    pid = 0x0100
    hwid = "SOFTWARE_ONLY USB V10 FIXTURE"
    description = "Synthetic CDC; no physical PLC"


class SoftwarePLC:
    """Wire-level truth kept separate from production's signal lookup logic."""
    def __init__(self):
        self.m = {1000: 1, 1002: 0}
        self.d = {1001: 0x8840, 1002: 7920,2120:4000}
        self.y = {}
        for i in range(12):
            self.m.update({140+i: 0, 390+i: 1, 430+i: 1, 880+i: 0})
            self.d.update({100+i: 230+i, 200+i: 2300+i, 300+i: i,
                           2000+10*i: 1500+i, 2400+i: 15000+i,
                           2200+i: 0, 2220+i: 10+i, 2240+i: 20+i,
                           2600+3*i: 0, 2601+3*i: 12+i, 2602+3*i: 0})
        for p in range(6):
            self.m.update({80+p: 0, 310+p: 1, 300+p: 0})
        self.frames = []
        self.mutations = []
        self.parameter_mutations = []
        self.identity = IDENTITY
        self.on_reset = None
        self.on_write = None
        self.drop_ack = set()
        self.drop_write = set()
        self.keep_edge_on_release = False
        self.ignore_effect = False
        self.connections = []
        self.extra_effects = {}
        self.drop_parameter_ack = set()
        self.drop_parameter_write = set()
        self.partial_parameter_write = False
        self.fail_parameter_readback = False
        self.on_parameter_write = None

    def effect(self, number, value):
        previous = self.m.get(number, 0)
        self.m[number] = value
        if not self.m[1000]:
            return
        edges = {**{100+i: 160+i for i in range(12)},
                 **{40+p: 90+p for p in range(6)},
                 **{640+i: 680+i for i in range(12)}, 56: 58,500:710,520:730}
        if number in edges:
            if value:
                self.m[edges[number]] = 1
            elif not self.keep_edge_on_release:
                self.m[edges[number]] = 0
        if not value or previous or self.ignore_effect:
            return
        if number in self.extra_effects:
            ack,level,edge = self.extra_effects[number]
            self.m[ack] = level
            if edge is not None:
                self.m[edge] = value
            return
        if 100 <= number <= 111:
            self.m[140+number-100] = 1
        elif 120 <= number <= 131:
            self.m[140+number-120] = 0
        elif 40 <= number <= 45:
            self.m[80+number-40] = 1
        elif 60 <= number <= 65:
            self.m[80+number-60] = 0
        elif 640 <= number <= 651:
            i = number-640
            self.m[880+i] = 1
            self.d[2600+3*i] = 1
        elif 660 <= number <= 671:
            i = number-660
            self.m[880+i] = 0
            self.d[2600+3*i] = 3
        elif number in (56, 57):
            for i in range(12):
                self.m[880+i] = int(number == 56)
                self.d[2600+3*i] = 1 if number == 56 else 3

    def add_independent_machine(self):
        self.extra_effects={500:(700,1,710),501:(700,0,None),520:(720,1,730)}
        self.m.update({500:0,501:0,520:0,700:0,710:0,720:0,730:0})
        self.d[900]=0xFFF6

    def factory(self, **kwargs):
        connection = WireSerial(self, **kwargs)
        self.connections.append(connection)
        return connection


class WireSerial:
    def __init__(self, state, **kwargs):
        assert kwargs["port"] is None
        self.state = state
        self.port = None
        self.dtr = self.rts = True
        self.pending = b""
        self.timeout = kwargs["timeout"]
        self.closed = False

    def open(self):
        assert self.port == "COM6" and not self.dtr and not self.rts

    def reset_input_buffer(self):
        self.pending = b""
        if self.state.on_reset:
            self.state.on_reset(self.state)

    def write(self, frame):
        assert frame.startswith(b":") and frame.endswith(b"\r\n")
        data = bytes.fromhex(frame[1:-2].decode())
        assert not sum(data) & 255, "Production emitted invalid LRC"
        station, function = data[:2]
        assert station == 1, "Wrong-station fixture request"
        assert function in (1, 3, 5, 6, 0x67), "Native program mutation forbidden"
        self.state.frames.append(frame)
        address, count = int.from_bytes(data[2:4], "big"), int.from_bytes(data[4:6], "big")
        if function == 5:
            assert len(data) == 7 and 0x800 <= address < 0xE00
            number, value = address-0x800, int(count == 0xFF00)
            assert count in (0xFF00, 0), "Only bit ON/OFF permitted"
            self.state.mutations.append((number, value))
            if self.state.on_write:
                self.state.on_write(self.state, number, value)
            if (number, value) not in self.state.drop_write:
                self.state.effect(number, value)
            self.pending = b"" if (number, value) in self.state.drop_ack else frame
        elif function == 6:
            assert len(data)==7 and 0x1000 <= address < 0x2000
            number=address-0x1000
            self.state.parameter_mutations.append((number,count))
            if self.state.on_parameter_write:
                self.state.on_parameter_write(self.state,number,count)
            if (number,count) not in self.state.drop_parameter_write:
                self.state.d[number]=count
            self.pending=b"" if (number,count) in self.state.drop_parameter_ack else frame
            if self.state.partial_parameter_write:
                return len(frame)-1
        elif function == 0x67:
            assert data[2:6] == bytes.fromhex("00100005")
            self.pending = packet(bytes([station, function, 10]) + self.state.identity)
        else:
            assert 1 <= count <= 64
            if function == 3:
                assert 0x1000 <= address < 0x2000
                values = [self.state.d.get(address-0x1000+i, 0) for i in range(count)]
                payload = b"".join(v.to_bytes(2, "big") for v in values)
                if self.state.fail_parameter_readback and self.state.parameter_mutations:
                    self.pending=b""
                    return len(frame)
            else:
                memory, base = (self.state.y, 0x500) if address < 0x800 else (self.state.m, 0x800)
                assert base <= address < base+1536
                payload = bytearray((count+7)//8)
                for i in range(count):
                    payload[i//8] |= int(bool(memory.get(address-base+i, 0))) << (i%8)
                payload = bytes(payload)
            self.pending = packet(bytes([station, function, len(payload)]) + payload)
        return len(frame)

    def read(self, size):
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def close(self):
        self.closed = True


class Scenario:
    def __init__(self, directory, state=None):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = state or SoftwarePLC()
        self.source = self.root/"software-only-v10.dvp"
        self.source.write_bytes(b"SOFTWARE_ONLY NEUTRAL BASELINE; NOT A COMPILED PLC PROJECT\n")
        self.source_sha256 = sha(self.source.read_bytes())
        self.catalog = self.root/"software-only-catalog.json"
        self.catalog_data = json.loads((ROOT/"tests/dvpserial/fixtures/thermal-regression.json").read_text("utf-8"))
        self.catalog_data.update(reference_source_sha256=self.source_sha256,
                                 reference_payload_sha256=sha(b"SOFTWARE_ONLY PAYLOAD"))
        save(self.catalog, self.catalog_data)
        self.service = Service(self.root/"runtime", read_roots=[self.root])
        self.workspace = self.service.workspace_create(TARGET, str(self.source), self.source_sha256,
                                                       task_id="temperature-software-only", revision=REVISION)
        self.workspace_root, _ = self.service.workspace(self.workspace["workspace_id"])
        self.context = dict(workspace_id=self.workspace["workspace_id"], task_id=self.workspace["task_id"],
                            revision=REVISION, context_digest=self.workspace["context_digest"],
                            workspace_root=str(self.workspace_root), baseline_sha256=self.source_sha256)
        self.proof = self.root/"software-only-live-revision.json"
        self.proof_data = dict(schema_version=1, verified=True, kind="HUMAN_CONFIRMED_LIVE_REVISION",
             contract_id=self.catalog_data["contract_id"], revision=REVISION,
             source_sha256=self.source_sha256, payload_sha256=self.catalog_data["reference_payload_sha256"],
             identity_sha256=sha(IDENTITY), usb_hwid=SoftwarePort.hwid, station=1,
             route="directlink_cdc", context=self.context,
             evidence="SOFTWARE_ONLY fixture field; no real human/compile/load/readback claim")
        save(self.proof, self.proof_data)
        self.lease = self.root/"software-only-port-lease.json"
        self.lease_data = dict(schema_version=1, status="HELD", owner="software-test",
                              token="unique-software-fixture-lease", port="COM6",
                              expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
        save(self.lease, self.lease_data)
        self.config = dict(enabled=True, port="COM6", baudrate=9600, station=1,
             expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),
             expected_vid=SoftwarePort.vid, expected_pid=SoftwarePort.pid, expected_hwid=SoftwarePort.hwid,
             expected_identity_sha256=sha(IDENTITY), route="directlink_cdc", allow_read_probe=True,
             target_model="DVP14SS211R", machine_control_enabled=True, allow_machine_commands=True,
             user_present=True, heaters_isolated=True, pulse_ms=50,
             machine_catalog_path=str(self.catalog), machine_catalog_sha256=sha(self.catalog.read_bytes()),
             machine_source_path=str(self.source), machine_source_sha256=self.source_sha256,
             live_revision_evidence_path=str(self.proof), live_revision_evidence_sha256=sha(self.proof.read_bytes()),
             machine_state_dir=str(self.root/"shared-machine-state"),
             port_lease_path=str(self.lease), port_lease_owner=self.lease_data["owner"],
             port_lease_token=self.lease_data["token"])
        self.adapter = self.new_adapter()

    def new_adapter(self):
        return DvpSerialAdapter(config=self.config, serial_factory=self.state.factory,
                                ports_provider=lambda: [SoftwarePort()])

    def repin_proof(self):
        save(self.proof, self.proof_data)
        self.config["live_revision_evidence_sha256"] = sha(self.proof.read_bytes())

    def install_unrelated_profile(self):
        """Only host/profile/fixture data changes; production code is untouched."""
        self.state.add_independent_machine()
        self.source.write_bytes(b"SOFTWARE_ONLY SECOND PUMP/VALVE BASELINE\n")
        self.source_sha256=sha(self.source.read_bytes())
        self.catalog_data=dict(schema_version=1,contract_id="PUMP_VALVE_SOFTWARE_FIXTURE",revision="PUMP_BENCH_R4",
              reference_source_sha256=self.source_sha256,reference_payload_sha256=sha(b"OTHER SOFTWARE PAYLOAD"),
              read_devices=["M500","M501","M520","M700","M710","M720","M730","M1000","M1002","D900"],
              observations={"pressure":{"device":"D900","signed":True,"scale":0.01,"unit":"bar"},
                            "pump_mode":{"device":"M700","enum":{"0":"DISABLED","1":"ENABLED"}},
                            "valve":{"device":"M720"}},commands={})
        for name,coil,ack,level,edge,start,stop in (
            ("pump.enable","M500","M700",1,"M710",True,["M501"]),
            ("pump.disable","M501","M700",0,None,False,[]),
            ("valve.enable","M520","M720",1,"M730",True,[])):
            self.catalog_data["commands"][name]=dict(selector=None,coils=[coil],ack_devices=[ack],
                     ack_expected=level,release_edge_devices=[edge] if edge else [],
                     stop_guards=[stop],is_start=start,effect="SOFTWARE_ONLY logical state")
        save(self.catalog,self.catalog_data)
        self.workspace=self.service.workspace_create(TARGET,str(self.source),self.source_sha256,
                           task_id="different-machine-software-only",revision=self.catalog_data["revision"])
        self.workspace_root,_=self.service.workspace(self.workspace["workspace_id"])
        self.context=dict(workspace_id=self.workspace["workspace_id"],task_id=self.workspace["task_id"],
              revision=self.catalog_data["revision"],context_digest=self.workspace["context_digest"],
              workspace_root=str(self.workspace_root),baseline_sha256=self.source_sha256)
        self.proof_data.update(contract_id=self.catalog_data["contract_id"],revision=self.catalog_data["revision"],
              source_sha256=self.source_sha256,payload_sha256=self.catalog_data["reference_payload_sha256"],context=self.context)
        self.repin_proof()
        self.config.update(machine_catalog_sha256=sha(self.catalog.read_bytes()),machine_source_sha256=self.source_sha256)
        self.adapter=self.new_adapter()

    def call(self, op, **args):
        op = {"temperature.read":"machine.read", "temperature.status":"machine.status",
              "temperature.observe":"machine.observe", "temperature.reconcile":"machine.reconcile"}.get(op,op)
        return self.adapter.invoke(op, dict(universal_target=TARGET, _execution_context=self.context, **args))

    def command(self, op="temperature.channel.start", command_id="software-command-1", **args):
        if "channel" in args:
            args["selector"] = args.pop("channel")
        if "pair" in args:
            args["selector"] = args.pop("pair")
        return self.call("machine.command", command=op, command_id=command_id, **args)

    def install_test_parameters(self,only=False):
        self.state.d[2120]=4000
        self.catalog_data["parameters"]={
          "setpoint":{"device":"D2000","min":0,"max":4000,"signed":False,"unit":"0.1 C",
             "write_guards":[{"device":"M880","count":12,"equals":0}],
             "relations":[{"left":"value","op":"lt","right":"D2120","offset":-4}]},
          "gain":{"device":"D2200","min":0,"max":30000,"signed":False,
             "write_guards":[{"device":"M880","equals":0}],"relations":[]}}
        if only:
            self.catalog_data["commands"]={}
            self.config.pop("pulse_ms",None)
        save(self.catalog,self.catalog_data)
        self.config.update(machine_catalog_sha256=sha(self.catalog.read_bytes()),allow_machine_parameters=True)
        self.adapter=self.new_adapter()

    def parameter(self,key="setpoint",value=2000,command_id="software-parameter-1",**extra):
        return self.call("machine.parameter.write",parameter=key,value=value,command_id=command_id,**extra)


class TemperatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = Scenario(self.temp.name)

    def success(self, result):
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "ok", result)

    def blocked(self, result):
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.s.state.mutations, [], result)

    def test_channel_and_pair_boundaries_write_only_exact_request_then_release(self):
        for operation, selection, n, coil in (
            ("temperature.channel.start", "channel", 1, 100),
            ("temperature.channel.start", "channel", 12, 111),
            ("temperature.channel.stop", "channel", 12, 131),
            ("temperature.contactor.start", "pair", 1, 40),
            ("temperature.contactor.start", "pair", 6, 45),
            ("temperature.contactor.stop", "pair", 6, 65)):
            with self.subTest(operation=operation, selection=n):
                before = len(self.s.state.mutations)
                self.success(self.s.command(operation, command_id=f"{selection}-{coil}", **{selection:n}))
                self.assertEqual(self.s.state.mutations[before:], [(coil,1),(coil,0)])

    def test_separate_all_heating_batch_and_all_contactors_never_use_global_coupling(self):
        self.success(self.s.command("temperature.all_heating.start", command_id="all-heat"))
        self.assertEqual(self.s.state.mutations, [(n,v) for n in range(100,112) for v in (1,0)])
        self.success(self.s.command("temperature.all_contactors.start", command_id="all-pairs"))
        self.assertEqual(self.s.state.mutations[24:], [(n,v) for n in range(40,46) for v in (1,0)])
        self.assertFalse({20,21} & {n for n,_ in self.s.state.mutations})

    def test_independent_tuning_preserves_other_active_channel_and_all_gains(self):
        self.s.state.m[881] = 1
        original_gains = {n:v for n,v in self.s.state.d.items() if 2200 <= n <= 2251}
        self.success(self.s.command("temperature.tuning.start", channel=1))
        self.success(self.s.command("temperature.tuning.stop", command_id="tune-stop", channel=1))
        self.assertEqual(self.s.state.mutations, [(640,1),(640,0),(660,1),(660,0)])
        self.assertEqual(self.s.state.m[881], 1)
        self.assertEqual({n:v for n,v in self.s.state.d.items() if 2200 <= n <= 2251}, original_gains)

    def test_all_tuning_exact_single_frozen_request_no_old_at_or_cr28_writes(self):
        self.success(self.s.command("temperature.all_tuning.start", command_id="at-all-start"))
        self.success(self.s.command("temperature.all_tuning.stop", command_id="at-all-stop"))
        self.assertEqual(self.s.state.mutations, [(56,1),(56,0),(57,1),(57,0)])

    def test_separate_group_stops_pulse_every_member_without_global_stop(self):
        for i in range(12):self.s.state.m[140+i]=1
        for p in range(6):self.s.state.m[80+p]=1
        self.success(self.s.command("temperature.all_heating.stop",command_id="all-heat-stop"))
        self.success(self.s.command("temperature.all_contactors.stop",command_id="all-contactor-stop"))
        expected=[(n,v) for n in (*range(120,132),*range(60,66)) for v in (1,0)]
        self.assertEqual(self.s.state.mutations,expected)
        self.assertFalse(any(self.s.state.m[140+i] for i in range(12)))
        self.assertFalse(any(self.s.state.m[80+p] for p in range(6)))

    def test_profile_inspection_is_zero_wire_without_port_ownership_or_attendance(self):
        self.s.config["user_present"]=False
        self.s.config["heaters_isolated"]=False
        self.s.lease_data["owner"]="human";save(self.s.lease,self.s.lease_data)
        self.s.adapter.serial_factory=lambda **_:self.fail("Offline profile inspection opened serial")
        self.s.adapter.ports_provider=lambda:self.fail("Offline profile inspection enumerated ports")
        result=self.s.call("machine.profile")
        self.success(result)
        self.assertEqual(result["contract_id"],self.s.catalog_data["contract_id"])
        self.assertEqual(result["revision"],REVISION)
        self.assertFalse(result["live_revision_verified"])
        self.assertIn("temperature.channel.start",result["commands"])
        self.assertEqual(self.s.state.frames,[])
        self.s.catalog.write_bytes(self.s.catalog.read_bytes()+b" ")
        result=self.s.call("machine.profile")
        self.blocked(result)
        self.assertEqual(self.s.state.frames,[])

    def test_read_y_octal_alias_uses_documented_pdu_and_forbids_crossing_catalog(self):
        self.s.state.y[24] = 1
        result = self.s.call("temperature.read", device="Y30", count=4)
        self.success(result)
        self.assertEqual(result["values"], [1,0,0,0])
        query = bytes.fromhex(self.s.state.frames[-1][1:-2].decode())
        self.assertEqual(query[1:6], bytes.fromhex("0105180004"))
        for device,count in (("Y27",6),("Y6",1),("Y28",1),("D2000",2),("D999",2),("M1535",2), ("D100",65)):
            with self.subTest(device=device,count=count):
                before = len(self.s.state.frames)
                self.blocked(self.s.call("temperature.read",device=device,count=count))
                self.assertTrue(all(bytes.fromhex(f[1:-2].decode())[1] == 0x67
                                    for f in self.s.state.frames[before:]), "Invalid read must not query outside catalog")

    def test_m_and_d_reads_bounded_in_whitelist_without_setting_write(self):
        for device,count,expected in (("D100",12,list(range(230,242))),
                                     ("M880",12,[0]*12),("D2200",12,[0]*12)):
            result = self.s.call("temperature.read", device=device,count=count)
            self.success(result)
            self.assertEqual(result["values"],expected)
        self.assertEqual(self.s.state.mutations, [])

    def test_unknown_live_revision_allows_labeled_raw_reads_and_blocks_commands(self):
        self.s.proof_data["revision"] = "V7"
        self.s.repin_proof()
        result = self.s.call("machine.status")
        self.success(result)
        self.assertFalse(result["live_revision_verified"], result)
        self.assertNotIn("COMPLETE",json.dumps(result))
        self.blocked(self.s.command(channel=1))

    def test_status5_suppresses_stale_reason_and_snapshot_is_not_atomic(self):
        self.s.state.d[2600], self.s.state.d[2602] = 5,7
        result = self.s.call("machine.status")
        self.success(result)
        encoded = json.dumps(result)
        self.assertIn("PRECONDITION_MISSING",encoded)
        self.assertNotIn("READ_ERROR",encoded)
        status_field=next(name for name,spec in self.s.catalog_data["observations"].items()
                          if spec["device"]=="D2600")
        reason_field=self.s.catalog_data["observations"][status_field]["reason_field"]
        self.assertEqual(result["data"][reason_field]["raw"],[7])
        self.assertEqual(result["data"][reason_field]["labels"],["PRECONDITION_MISSING"])
        self.assertFalse(result["atomic"],result)
        for field in ("timestamp","began_at","finished_at"):
            self.assertIsNotNone(datetime.fromisoformat(result[field].replace("Z","+00:00")).tzinfo)

    def test_stop_or_boot_or_high_command_or_stale_edge_rejects_new_on(self):
        for number in (1000,1002,100,160):
            with self.subTest(number=number):
                self.s.state.m[number] = 0 if number == 1000 else 1
                self.blocked(self.s.command(command_id=f"negative-{number}",channel=1))
                self.s.state.m[number] = 1 if number == 1000 else 0

    def test_existing_terminal_tuning_state_is_not_new_completion_and_kp_zero_allowed(self):
        self.s.state.d[2600]=2
        self.s.state.d[2200]=0
        snapshot=self.s.call("machine.status")
        self.success(snapshot)
        self.assertNotIn('"completion_newly_proven": true',json.dumps(snapshot))
        self.success(self.s.command("temperature.tuning.start",channel=1))
        self.assertEqual(self.s.state.d[2600],1)

    def test_opposing_stop_levels_are_checked_before_on(self):
        for number in (21,120,60,57,660):
            with self.subTest(number=number):
                self.s.state.m[number]=1
                op="temperature.tuning.start" if number in (57,660) else "temperature.channel.start"
                self.blocked(self.s.command(op,command_id=f"opposing-{number}",channel=1))
                self.s.state.m[number]=0

    def test_invalid_selector_and_caller_raw_fields_are_zero_wire(self):
        for args in (dict(channel=0),dict(channel=13),dict(channel=True),dict(channel=1,pulse_ms=1),
                     dict(channel=1,device="M1072"),dict(channel=1,value=True),dict(channel=1,profile_path="untrusted")):
            with self.subTest(args=args):
                before=len(self.s.state.frames)
                self.blocked(self.s.command(**args))
                self.assertEqual(len(self.s.state.frames),before)

    def test_missing_or_wrong_authority_and_source_and_context_emit_no_coil_write(self):
        for key,value in (("user_present",False),("heaters_isolated",False),
                          ("machine_control_enabled",False),("allow_machine_commands",False),
                          ("pulse_ms",49),("pulse_ms",501),("expected_identity_sha256","0"*64)):
            with self.subTest(key=key,value=value):
                original=self.s.config[key];self.s.config[key]=value
                self.blocked(self.s.command(command_id=f"bad-{key}-{value}",channel=1))
                self.s.config[key]=original
        self.s.source.write_bytes(b"CHANGED ORIGINAL SOURCE")
        self.blocked(self.s.command(command_id="changed-source",channel=1))

    def test_wrong_identity_proof_payload_and_port_lease_block_even_with_same_com(self):
        for key,value in (("identity_sha256","0"*64),("payload_sha256","0"*64),
                          ("usb_hwid","different gateway"),("verified",False),("kind","TITLE_ONLY")):
            with self.subTest(key=key):
                original=self.s.proof_data[key];self.s.proof_data[key]=value;self.s.repin_proof()
                self.blocked(self.s.command(command_id=f"proof-{key}",channel=1))
                self.s.proof_data[key]=original;self.s.repin_proof()
        self.s.lease_data.update(owner="human",token="human-token")
        save(self.s.lease,self.s.lease_data)
        self.blocked(self.s.command(command_id="human-owns-com6",channel=1))

    def test_lost_on_ack_still_attempts_one_permitted_release_and_never_replays(self):
        self.s.state.drop_ack.add((100,1))
        result=self.s.command(channel=1)
        self.assertFalse(result["ok"],result)
        self.assertEqual(result["status"],"unknown_outcome",result)
        self.assertEqual(self.s.state.mutations,[(100,1),(100,0)])
        self.assertFalse(result["retry_allowed"],result)
        self.assertFalse(self.s.command(channel=1)["ok"])
        self.assertEqual(self.s.state.mutations,[(100,1),(100,0)])

    def test_unproved_release_fence_survives_adapter_restart_and_new_workspace(self):
        self.s.state.drop_ack.add((100,0));self.s.state.drop_write.add((100,0))
        result=self.s.command(channel=1)
        self.assertEqual(result["status"],"unknown_outcome",result)
        before=list(self.s.state.mutations)
        self.s.adapter=self.s.new_adapter()
        blocked=self.s.command(command_id="new-id-after-restart",channel=2)
        self.assertFalse(blocked["ok"],blocked)
        self.assertEqual(self.s.state.mutations,before)
        workspace=self.s.service.workspace_create(TARGET,str(self.s.source),self.s.source_sha256,
                        task_id="second-valid-workspace",revision=REVISION)
        workspace_root,_=self.s.service.workspace(workspace["workspace_id"])
        self.s.context=dict(workspace_id=workspace["workspace_id"],task_id=workspace["task_id"],
            revision=REVISION,context_digest=workspace["context_digest"],workspace_root=str(workspace_root),
            baseline_sha256=self.s.source_sha256)
        self.s.proof_data["context"]=dict(self.s.context);self.s.repin_proof()
        blocked=self.s.command(command_id="new-workspace-command",channel=3)
        self.assertFalse(blocked["ok"],blocked)
        self.assertEqual(self.s.state.mutations,before)
        reconciled=self.s.call("machine.reconcile",journal_path=result["journal_path"])
        self.assertFalse(reconciled["ok"],reconciled)
        self.assertEqual(self.s.state.mutations,before)

    def test_reconcile_rejects_tampered_mapping_instead_of_clearing_live_fence(self):
        self.s.state.drop_ack.add((100,0));self.s.state.drop_write.add((100,0))
        failed=self.s.command(channel=1)
        before=list(self.s.state.mutations)
        path=Path(failed["journal_path"])
        data=json.loads(path.read_text("utf-8"))
        data["members"][0]["coil"]="M101"
        data["members"][0]["edge_devices"]=["M161"]
        save(path,data)
        rejected=self.s.call("machine.reconcile",journal_path=str(path))
        self.assertFalse(rejected["ok"],rejected)
        self.assertEqual(self.s.state.mutations,before)
        self.assertFalse(self.s.command(command_id="tamper-cannot-remove-fence",channel=2)["ok"])
        self.assertEqual(self.s.state.mutations,before)

    def test_release_edge_high_preserves_fence_even_if_command_low(self):
        self.s.state.keep_edge_on_release=True
        result=self.s.command(channel=1)
        self.assertEqual(self.s.state.m.get(100),0)
        self.assertEqual(self.s.state.m.get(160),1)
        self.assertFalse(result["ok"],result)
        before=list(self.s.state.mutations)
        reconciled=self.s.call("temperature.reconcile",journal_path=result["journal_path"])
        self.assertFalse(reconciled["ok"],reconciled)
        self.assertFalse(self.s.command(command_id="different-while-edge-high",channel=2)["ok"])
        self.assertEqual(self.s.state.mutations,before)

    def test_read_only_reconcile_proves_lows_then_new_id_allowed_without_replaying(self):
        self.s.state.drop_ack.add((100,0));self.s.state.drop_write.add((100,0))
        failed=self.s.command(channel=1)
        before=list(self.s.state.mutations)
        self.s.state.m[100]=self.s.state.m[160]=0
        reconciled=self.s.call("temperature.reconcile",journal_path=failed["journal_path"])
        self.success(reconciled)
        self.assertEqual(self.s.state.mutations,before)
        self.s.state.drop_ack.clear();self.s.state.drop_write.clear()
        self.success(self.s.command(command_id="after-proven-release",channel=2))
        self.assertEqual(self.s.state.mutations[2:],[(101,1),(101,0)])
        self.assertFalse(self.s.command(channel=1)["ok"])

    def test_partial_batch_fourth_failure_stops_before_fifth_and_records_members(self):
        self.s.state.drop_ack.add((103,1))
        failed=self.s.command("temperature.all_heating.start")
        self.assertFalse(failed["ok"],failed)
        self.assertEqual(self.s.state.mutations,[(n,v) for n in range(100,104) for v in (1,0)])
        data=json.loads(Path(failed["journal_path"]).read_text("utf-8"))
        encoded=json.dumps(data)
        self.assertIn("M103",encoded)
        self.assertIn("M104",encoded,"Unattempted members must be explicitly recoverable")

    def test_unrelated_pump_valve_profile_uses_same_core_new_names_addresses_units(self):
        self.s.install_unrelated_profile()
        self.success(self.s.command("pump.enable",command_id="other-pump-enable"))
        self.success(self.s.command("valve.enable",command_id="other-valve-enable"))
        self.success(self.s.command("pump.disable",command_id="other-pump-disable"))
        self.assertEqual(self.s.state.mutations,[(500,1),(500,0),(520,1),(520,0),(501,1),(501,0)])
        result=self.s.call("machine.observe")
        self.success(result)
        self.assertEqual(result["data"]["pressure"]["raw"],[65526])
        self.assertEqual(result["data"]["pressure"]["values"],[-0.1])
        self.assertEqual(result["data"]["pressure"]["unit"],"bar")
        self.assertEqual(result["data"]["pump_mode"]["labels"],["DISABLED"])
        self.assertEqual(result["data"]["valve"]["raw"],[1])
        self.assertNotIn("temperature",json.dumps(result))

    def test_is_start_is_profile_semantics_independent_of_command_name(self):
        self.s.install_unrelated_profile()
        self.s.state.m[1000]=0
        self.blocked(self.s.command("pump.enable",command_id="enable-on-stopped-target"))

    def test_named_profile_command_cannot_bypass_platform_system_relay_boundary(self):
        for device in ("M1000","M1072","M1073"):
            with self.subTest(device=device):
                self.s.catalog_data["commands"]["fake_named_action"]=dict(selector=None,coils=[device],
                    ack_devices=["M140"],ack_expected=1,release_edge_devices=[],stop_guards=[[]],is_start=True)
                if device not in self.s.catalog_data["read_devices"]:
                    self.s.catalog_data["read_devices"].append(device)
                save(self.s.catalog,self.s.catalog_data)
                self.s.config["machine_catalog_sha256"]=sha(self.s.catalog.read_bytes())
                result=self.s.command("fake_named_action",command_id="system-boundary-"+device)
                self.blocked(result)

    def test_competing_same_owner_clients_cannot_interleave_target_transport(self):
        other=self.s.new_adapter()
        outcomes=[]
        def compete(state,number,value):
            if (number,value)==(100,1):
                outcomes.append(other.invoke("machine.command",dict(universal_target=TARGET,
                  _execution_context=self.s.context,command="temperature.channel.start",selector=2,
                  command_id="concurrent-same-owner")))
        self.s.state.on_write=compete
        self.success(self.s.command(channel=1))
        self.assertEqual(len(outcomes),1)
        self.assertEqual(outcomes[0]["code"],"MACHINE_TARGET_BUSY",outcomes)
        self.assertEqual(self.s.state.mutations,[(100,1),(100,0)])

    def test_late_authority_lease_source_or_revision_changes_after_intent_block_write(self):
        changes=("attendance","lease","source","context","route")
        for change in changes:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                s=Scenario(directory)
                def revoke_after_intent(state):
                    journals=list(Path(s.config["machine_state_dir"]).glob("command-*.json"))
                    if not journals or not any(m["phase"]=="request_intent"
                                    for j in journals for m in json.loads(j.read_text("utf-8"))["members"]):
                        return
                    state.on_reset=None
                    if change=="attendance":s.config["user_present"]=False
                    elif change=="lease":
                        s.lease_data["owner"]="human";save(s.lease,s.lease_data)
                    elif change=="source":s.source.write_bytes(b"SOURCE CHANGED AFTER INTENT")
                    elif change=="route":s.config["station"]=2
                    else:
                        manifest=s.workspace_root/"manifest.json"
                        data=json.loads(manifest.read_text("utf-8"));data["revision"]="unexpected"
                        save(manifest,data)
                s.state.on_reset=revoke_after_intent
                result=s.command(channel=1)
                self.assertFalse(result["ok"],result)
                self.assertEqual(s.state.mutations,[],result)

    def test_late_bridge_lease_revocation_prevents_even_cdc_initialization_bytes(self):
        self.s.config["cdc_initialize"]=True
        def human_reclaims_after_bridge_reset(state):
            state.on_reset=None
            self.s.lease_data["owner"]="human"
            save(self.s.lease,self.s.lease_data)
        self.s.state.on_reset=human_reclaims_after_bridge_reset
        result=self.s.command(channel=1)
        self.blocked(result)
        self.assertEqual(self.s.state.frames,[],"CDC bridge must refresh lease afterreset beforeanybytes")

    def test_late_revocation_after_on_retains_uncertainty_without_unpermitted_off(self):
        def revoke_after_on(state,number,value):
            if (number,value)==(100,1):self.s.config["user_present"]=False
        self.s.state.on_write=revoke_after_on
        failed=self.s.command(channel=1)
        self.assertFalse(failed["ok"],failed)
        self.assertEqual(failed["status"],"unknown_outcome",failed)
        self.assertEqual(self.s.state.mutations,[(100,1)])
        self.s.config["user_present"]=True
        self.s.adapter=self.s.new_adapter()
        self.assertFalse(self.s.command(command_id="fresh-authority-uncertain-target",channel=2)["ok"])
        self.assertEqual(self.s.state.mutations,[(100,1)])

    def test_late_external_request_is_not_cleared_when_own_on_never_sent(self):
        def human_command_after_intent(state):
            journals=list(Path(self.s.config["machine_state_dir"]).glob("command-*.json"))
            if not journals or not any(m["phase"]=="request_intent"
                               for j in journals for m in json.loads(j.read_text("utf-8"))["members"]):
                return
            state.on_reset=None
            state.m[100]=1  # Another operator owns this already asserted request.
        self.s.state.on_reset=human_command_after_intent
        result=self.s.command(channel=1)
        self.assertFalse(result["ok"],result)
        self.assertEqual(self.s.state.mutations,[],"Blocked unsent ON must not clear another operator's request")
        self.assertEqual(self.s.state.m[100],1)

    def test_expired_then_renewed_lease_does_not_clear_existing_uncertainty(self):
        self.s.state.drop_ack.add((100,0));self.s.state.drop_write.add((100,0))
        failed=self.s.command(channel=1)
        self.assertEqual(failed["status"],"unknown_outcome",failed)
        before=list(self.s.state.mutations)
        self.s.lease_data["expires_at"]=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        save(self.s.lease,self.s.lease_data)
        self.assertFalse(self.s.command(command_id="expired-lease",channel=2)["ok"])
        self.s.lease_data["expires_at"]=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
        save(self.s.lease,self.s.lease_data)
        self.s.adapter=self.s.new_adapter()
        self.assertFalse(self.s.command(command_id="renewed-lease-still-fenced",channel=2)["ok"])
        self.assertEqual(self.s.state.mutations,before)

    def test_failed_read_releases_owned_connection_after_lease_loss_or_expiry(self):
        for loss in ("lease-released","expiry"):
            with self.subTest(loss=loss),tempfile.TemporaryDirectory() as directory:
                s=Scenario(directory)
                self.success(s.call("machine.read",device="D100"))
                owned=s.state.connections[-1]
                self.assertFalse(owned.closed)
                before=len(s.state.frames)
                if loss=="lease-released":
                    s.lease_data["status"]="RELEASED";save(s.lease,s.lease_data)
                else:s.config["expires_at"]=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
                rejected=s.call("machine.read",device="D100")
                self.assertFalse(rejected["ok"],rejected)
                self.assertTrue(owned.closed,"Failed lease/expiry must release the port already owned by this adapter")
                self.assertIsNone(s.adapter.connection)
                self.assertEqual(len(s.state.frames),before,"Failure cleanup must issue no additional packets")

    def test_offline_profile_reports_existing_connection_without_open_or_packets(self):
        self.success(self.s.call("machine.read",device="D100"))
        before=len(self.s.state.frames)
        connections=len(self.s.state.connections)
        profile=self.s.call("machine.profile")
        self.success(profile)
        self.assertTrue(profile["connected"],profile)
        self.assertFalse(profile["port_opened"],profile)
        self.assertEqual(len(self.s.state.frames),before)
        self.assertEqual(len(self.s.state.connections),connections)
        self.s.lease_data["status"]="RELEASED";save(self.s.lease,self.s.lease_data)
        self.assertFalse(self.s.call("machine.read",device="D100")["ok"])
        after=self.s.call("machine.profile")
        self.success(after)
        self.assertFalse(after["connected"],after)
        self.assertFalse(after["port_opened"],after)
        self.assertEqual(len(self.s.state.frames),before)
        self.assertEqual(len(self.s.state.connections),connections)

    def test_unknown_m_failure_closes_port_and_keeps_target_fence_without_replay(self):
        self.s.state.drop_ack.add((100,0));self.s.state.drop_write.add((100,0))
        failed=self.s.command(channel=1)
        self.assertEqual(failed["status"],"unknown_outcome",failed)
        self.assertTrue(self.s.state.connections[-1].closed)
        self.assertIsNone(self.s.adapter.connection)
        before=list(self.s.state.mutations)
        self.assertFalse(self.s.command(command_id="after-m-cleanup-new-id",channel=2)["ok"])
        self.assertEqual(self.s.state.mutations,before)


class ParameterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.s=Scenario(self.temp.name);self.s.install_test_parameters()

    def success(self,result):
        self.assertTrue(result["ok"],result)
        self.assertTrue(result["readback_verified"],result)

    def blocked(self,result):
        self.assertFalse(result["ok"],result)
        self.assertEqual(self.s.state.parameter_mutations,[],result)
        self.assertEqual(self.s.state.mutations,[],result)

    def test_named_normal_d_write_has_exact_fc06_then_independent_readback_while_stop(self):
        self.s.state.m[1000]=0
        result=self.s.parameter()
        self.success(result)
        self.assertEqual(self.s.state.parameter_mutations,[(2000,2000)])
        self.assertEqual(self.s.state.d[2000],2000)
        self.assertEqual(self.s.state.mutations,[])
        functions=[bytes.fromhex(f[1:-2].decode())[1] for f in self.s.state.frames]
        write_index=functions.index(6)
        self.assertIn(3,functions[write_index+1:])
        frame=self.s.state.frames[write_index]
        self.assertEqual(frame,packet(bytes.fromhex("010617D007D0")))
        self.assertTrue(result["ack_verified"],result)

    def test_signed_unrelated_parameter_only_profile_needs_neither_mflag_nor_pulse(self):
        self.s.install_unrelated_profile()
        self.s.catalog_data["commands"]={}
        self.s.catalog_data["parameters"]={"pressure.limit":{"device":"D900","min":-1000,"max":1000,
          "signed":True,"unit":"0.01 bar","write_guards":[{"device":"M720","equals":0}],"relations":[]}}
        save(self.s.catalog,self.s.catalog_data)
        self.s.config.update(machine_catalog_sha256=sha(self.s.catalog.read_bytes()),allow_machine_parameters=True,
                             allow_machine_commands=False)
        self.s.config.pop("pulse_ms",None)
        self.s.adapter=self.s.new_adapter();self.s.state.m[1000]=0
        result=self.s.parameter("pressure.limit",-25)
        self.success(result)
        self.assertEqual(self.s.state.parameter_mutations,[(900,65511)])
        self.assertEqual(result["observed_value"],-25)

    def test_already_equal_is_noop_with_no_fc06(self):
        result=self.s.parameter(value=1500)
        self.assertTrue(result["ok"],result)
        self.assertTrue(result["already_equal"],result)
        self.assertEqual(self.s.state.parameter_mutations,[])

    def test_value_type_bounds_missing_profile_key_and_raw_overrides_block(self):
        for value in (-1,4001,True,2.0,"2000"):
            with self.subTest(value=value):self.blocked(self.s.parameter(value=value,command_id="bounds-"+str(value)))
        self.blocked(self.s.parameter(key="D2000",command_id="raw-address-alias"))
        self.blocked(self.s.parameter(command_id="raw-field",device="D2000"))
        self.blocked(self.s.parameter(command_id="raw-value-field",write_value=2000))

    def test_source_guard_checks_every_tuning_bit_and_comparison_margin(self):
        self.s.state.m[891]=1
        self.blocked(self.s.parameter(command_id="last-tuning-channel-active"))
        self.s.state.m[891]=0
        self.blocked(self.s.parameter(value=3996,command_id="equal-margin-must-fail-lt"))
        self.success(self.s.parameter(value=3995,command_id="inside-source-margin"))

    def test_separate_d_authority_and_source_proof_required(self):
        for key,value in (("allow_machine_parameters",False),("user_present",False),
                          ("heaters_isolated",False),("machine_control_enabled",False)):
            with self.subTest(key=key):
                old=self.s.config[key];self.s.config[key]=value
                self.blocked(self.s.parameter(command_id="authority-"+key));self.s.config[key]=old
        self.s.source.write_bytes(b"CHANGED SOURCE CANNOT GRANT D EDIT")
        self.blocked(self.s.parameter(command_id="source-sha-drift"))

    def test_profile_named_action_never_writes_d_system_region_or_non_d(self):
        for device in ("D1000","D1999","M100","Y0"):
            with self.subTest(device=device):
                self.s.catalog_data["parameters"]["platform_override"]={"device":device,"min":0,"max":1,
                         "signed":False,"write_guards":[],"relations":[]}
                if device not in self.s.catalog_data["read_devices"]:self.s.catalog_data["read_devices"].append(device)
                save(self.s.catalog,self.s.catalog_data)
                self.s.config["machine_catalog_sha256"]=sha(self.s.catalog.read_bytes())
                self.blocked(self.s.parameter("platform_override",1,command_id="platform-"+device))

    def test_lost_ack_can_be_proven_by_independent_value_without_rewriting(self):
        self.s.state.drop_parameter_ack.add((2000,2000))
        result=self.s.parameter()
        self.success(result)
        self.assertFalse(result["ack_verified"],result)
        self.assertEqual(self.s.state.parameter_mutations,[(2000,2000)])
        self.assertFalse(self.s.parameter()["ok"])
        self.assertEqual(self.s.state.parameter_mutations,[(2000,2000)])

    def test_ack_echo_without_effect_is_unknown_then_read_only_baseline_reconcile(self):
        self.s.state.drop_parameter_write.add((2000,2000))
        failed=self.s.parameter()
        self.assertFalse(failed["ok"],failed)
        self.assertEqual(failed["status"],"unknown_outcome",failed)
        self.assertEqual(self.s.state.d[2000],1500)
        self.assertFalse(self.s.parameter(command_id="different-id-while-unproved")["ok"])
        self.assertEqual(self.s.state.parameter_mutations,[(2000,2000)])
        self.s.state.m[1000]=0
        reconciled=self.s.call("machine.reconcile",journal_path=failed["journal_path"])
        self.assertTrue(reconciled["ok"],reconciled)
        self.assertEqual(reconciled["classification"],"baseline",reconciled)
        self.assertEqual(self.s.state.parameter_mutations,[(2000,2000)])

    def test_readback_loss_keeps_shared_fence_and_reconcile_classifies_other_then_candidate(self):
        self.s.state.fail_parameter_readback=True
        failed=self.s.parameter()
        self.assertEqual(failed["status"],"unknown_outcome",failed)
        before=list(self.s.state.parameter_mutations)
        self.s.adapter=self.s.new_adapter()
        self.assertFalse(self.s.command(command_id="m-after-unknown-d",channel=1)["ok"])
        self.s.state.fail_parameter_readback=False;self.s.state.d[2000]=1777
        other=self.s.call("machine.reconcile",journal_path=failed["journal_path"])
        self.assertFalse(other["ok"],other)
        self.assertEqual(other.get("classification",other.get("parameter_outcome")),"other",other)
        self.assertFalse(self.s.parameter(command_id="new-d-id-other-value")["ok"])
        self.s.state.d[2000]=2000;self.s.state.m[1000]=0
        candidate=self.s.call("machine.reconcile",journal_path=failed["journal_path"])
        self.assertTrue(candidate["ok"],candidate)
        self.assertEqual(candidate["classification"],"candidate",candidate)
        self.assertEqual(self.s.state.parameter_mutations,before)
        self.assertEqual(self.s.state.mutations,[])

    def test_unresolved_m_fence_also_blocks_d_write(self):
        self.s.state.drop_ack.add((100,0));self.s.state.drop_write.add((100,0))
        failed=self.s.command(channel=1)
        self.assertEqual(failed["status"],"unknown_outcome",failed)
        self.blocked_parameter_only(self.s.parameter())

    def blocked_parameter_only(self,result):
        self.assertFalse(result["ok"],result)
        self.assertEqual(self.s.state.parameter_mutations,[])

    def test_parameter_partial_write_never_retries_even_if_ack_missing(self):
        self.s.state.partial_parameter_write=True
        self.s.state.drop_parameter_write.add((2000,2000))
        failed=self.s.parameter()
        self.assertFalse(failed["ok"],failed)
        self.assertFalse(failed["retry_allowed"],failed)
        self.assertEqual(self.s.state.parameter_mutations,[(2000,2000)])
        self.assertFalse(self.s.parameter(command_id="new-id-after-partial")["ok"])
        self.assertEqual(self.s.state.parameter_mutations,[(2000,2000)])

    def test_late_d_write_guard_or_port_revocation_blocks_before_fc06(self):
        for mode in ("tuning","lease","authority"):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                s=Scenario(directory);s.install_test_parameters()
                def late(state):
                    journals=list(Path(s.config["machine_state_dir"]).glob("command-*.json"))
                    if not journals or not any("write_intent" in json.dumps(json.loads(j.read_text("utf-8"))) for j in journals):
                        return
                    state.on_reset=None
                    if mode=="tuning":state.m[891]=1
                    elif mode=="lease":s.lease_data["owner"]="human";save(s.lease,s.lease_data)
                    else:s.config["allow_machine_parameters"]=False
                s.state.on_reset=late
                result=s.parameter()
                self.assertFalse(result["ok"],result)
                self.assertEqual(s.state.parameter_mutations,[],result)

    def test_unknown_d_failure_closes_port_and_keeps_target_fence_without_replay(self):
        self.s.state.fail_parameter_readback=True
        failed=self.s.parameter()
        self.assertEqual(failed["status"],"unknown_outcome",failed)
        self.assertTrue(self.s.state.connections[-1].closed)
        self.assertIsNone(self.s.adapter.connection)
        before=list(self.s.state.parameter_mutations)
        self.assertFalse(self.s.parameter(command_id="after-d-cleanup-new-id")["ok"])
        self.assertEqual(self.s.state.parameter_mutations,before)


if __name__ == "__main__":
    unittest.main()
