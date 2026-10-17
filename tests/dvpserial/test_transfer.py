"""Independent software-only SS2 transfer transaction and fault model.

Synthetic D8 byte vectors are neutral fixtures, not a machine project or evidence
of PLC deployment. Every NativeProgramTransport instance is replaced here.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from universal_plc.dvp_serial import DvpSerialAdapter, DvpSerialError, control_frame
from universal_plc.dvp_transfer import NativeProgramTransport, ProgramTransfer, identity_frame, program_mode_frame, program_read_frame
from universal_plc.service import Service
if __package__:
    from .test_serial import PortFixture, SerialFixture
else:
    from test_serial import PortFixture, SerialFixture


TARGET = dict(vendor="delta", family="DVP", model="DVP14SS211R", toolchain="dvpserial")
PAYLOAD = bytes.fromhex("e82b0fc00f00")
CAPACITY_BYTES = 15840
REVISION = "V7-software-fixture"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_vector(checksum=0x06EB):
    # LD M1000, OUT S15, END; independent checksum E82B + 0FC0 + 0F00.
    header = bytearray(256)
    header[:3] = b"W\x01\x01"
    struct.pack_into("<H", header, 0x0C, 3960)
    struct.pack_into("<HH", header, 0x4A, 5, 0xD8)
    struct.pack_into("<H", header, 0x56, checksum)
    struct.pack_into("<I", header, 0x5E, 0x014D00B5)
    image = PAYLOAD + b"\xFF" * (CAPACITY_BYTES - len(PAYLOAD))
    passwords = (bytes(range(8)) + b"\x00\x00") * 2
    tables = b"\x00\x00\x04\x00" + bytes(range(4)) + b"\x10\x00" + b"\xFF" * 16 + b"\x18\x00" + b"\xFF" * 24
    initial = b"<TypeBegin>\nFunction=Initial Value\n<TypeEnd>\n<DataBegin>\n<DataEnd>\n"
    return bytes(header) + bytes(value ^ 0xB5 for value in image) + passwords + tables + initial + b"\nV7 NEUTRAL SOFTWARE FIXTURE\n"


class SoftwareState:
    def __init__(self):
        old_payload = bytes.fromhex("e82b10c00f00")
        self.image = bytearray(old_payload + bytes((i * 37 + 13) & 255 for i in range(CAPACITY_BYTES - len(old_payload))))
        self.original = bytes(self.image)
        self.descriptor = [0xFFFF, 0xFFFF]
        self.identity_sha256 = sha(bytes.fromhex("88401ef00001000e0000"))
        self.running = False
        self.protected = False
        self.plc_id_enabled = False
        self.parameters = {"D2000": [200, 201], "D2450": [10, 11]}
        self.events = []
        self.write_calls = []
        self.fail_write = False
        self.fail_mode = False
        self.corrupt_readback = False
        self.change_parameters_on_write = False
        self.on_mode = None
        self.on_write = None
        self.syntax_code = 0


class SoftwareTransport:
    def __init__(self, adapter, state):
        self.adapter = adapter
        self.state = state
        self.trace = []

    def identify(self):
        self.state.events.append("identify")
        return dict(ok=True, status="ok", family_verified=True, native_family="SS2",
                    capacity_words=7920, exact_model_verified=False,
                    identity_words=[0x8840, 7920, 1, 14, 0],
                    identity_sha256=self.state.identity_sha256, trace=list(self.trace))

    def preflight(self):
        self.state.events.append("preflight")
        return dict(running=self.state.running, protected=self.state.protected,
                    plc_id_enabled=self.state.plc_id_enabled)

    def id_status(self):
        return int(self.state.plc_id_enabled)

    def mode(self, enabled):
        self.state.events.append(("mode", enabled))
        if self.state.on_mode:
            self.state.on_mode(enabled)
        request = b":0164011901017F\r\n" if enabled else b":01640119010080\r\n"
        self.trace.append(dict(function=0x64, tx_hex=request.hex(), rx_hex=request.hex()))
        if self.state.fail_mode:
            self.state.fail_mode = False
            raise DvpSerialError("MODE_ACK_MISMATCH", "Software fixture lost mode ACK")
        return dict(ok=True, status="ok")

    def read_image(self):
        self.state.events.append("read_image")
        image = bytes(self.state.image)
        if self.state.corrupt_readback and self.state.write_calls:
            image = image[:-1] + bytes([image[-1] ^ 1])
        return image

    def read_descriptor(self):
        self.state.events.append("read_descriptor")
        return list(self.state.descriptor)

    def read_parameters(self, specs):
        self.state.events.append("read_parameters")
        return [dict(device=spec["device"], count=spec["count"],
                     values=self.state.parameters[spec["device"]][:spec["count"]]) for spec in specs]

    def write_program(self, offset_words, payload_le):
        assert type(offset_words) is int and isinstance(payload_le, bytes)
        assert len(payload_le) % 2 == 0 and 0 <= offset_words * 2 <= CAPACITY_BYTES - len(payload_le)
        self.state.events.append(("write_program", offset_words, len(payload_le)))
        if self.state.on_write:
            self.state.on_write(offset_words, payload_le)
        self.state.write_calls.append((offset_words, payload_le))
        self.state.image[offset_words * 2:offset_words * 2 + len(payload_le)] = payload_le
        if self.state.change_parameters_on_write:
            self.state.parameters["D2000"][0] += 1
        if self.state.fail_write:
            self.state.fail_write = False
            raise OSError("Software fixture lost ACK after accepting a program block")

    def write_descriptor(self):
        self.state.events.append("write_descriptor")
        self.state.descriptor = [0xFFFF, 0xFFFF]

    def finalize(self):
        self.state.events.append("finalize")
        return dict(syntax_code=self.state.syntax_code)


class Scenario:
    """Portable approval/context/files + shared simulated PLC state."""
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        self.source = self.root / "approved-v7.dvp"
        self.source.write_bytes(source_vector())
        self.source_sha256 = sha(self.source.read_bytes())
        self.state = SoftwareState()
        self.approval = self.root / "approved-v7.json"
        self.approval_data = dict(schema_version=1, approved=True, revision=REVISION,
                                  source_path=str(self.source.resolve()), source_sha256=self.source_sha256,
                                  payload_sha256=sha(PAYLOAD), approval_evidence="SOFTWARE FIXTURE ONLY; current V7 revision")
        self.approval.write_text(json.dumps(self.approval_data), encoding="utf-8")
        self.config = dict(enabled=True, port="COM6", baudrate=9600, station=1,
                           expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                           target_model="DVP14SS211R", expected_vid=0x05DD, expected_pid=0x0100,
                           expected_hwid="SOFTWARE SS2 FIXTURE USB ONLY",
                           route="directlink_cdc", allow_read_probe=True,
                           program_transfer_enabled=True, allow_program_transfer=True,
                           user_present=True, heaters_isolated=True, verified_contract=True,
                           approved_manifest_path=str(self.approval.resolve()),
                           approved_manifest_sha256=sha(self.approval.read_bytes()),
                           expected_identity_sha256=self.state.identity_sha256,
                           target_lease_dir=str((self.root / "target-leases").resolve()),
                           parameter_reads=[dict(device="D2000", count=2), dict(device="D2450", count=2)])
        self.service = Service(self.root / "runtime", read_roots=[self.root])
        self.workspace = self.service.workspace_create(TARGET, str(self.source), self.source_sha256,
                                                        task_id="ss2-software-transfer", revision=REVISION)
        self.workspace_root, _ = self.service.workspace(self.workspace["workspace_id"])
        self.files = self.workspace_root / "files"
        self.candidate = self.files / "baseline.dvp"
        self.context = dict(workspace_id=self.workspace["workspace_id"], task_id=self.workspace["task_id"],
                            revision=REVISION, context_digest=self.workspace["context_digest"],
                            workspace_root=str(self.workspace_root), baseline_sha256=self.source_sha256)
        self.adapter = DvpSerialAdapter(config=self.config, serial_factory=self.forbid_serial,
                                        ports_provider=lambda: [])
        self.adapter._read = self.read_stop

    @staticmethod
    def forbid_serial(**kwargs):
        raise AssertionError("A software transfer test attempted to open physical serial")

    def read_stop(self, device, count):
        assert device in ("M1000", "D1004") and count == 1
        return [int(self.state.running)] if device == "M1000" else [0]

    def transport(self, adapter):
        return SoftwareTransport(adapter, self.state)

    def call(self, operation, **arguments):
        args = dict(_execution_context=dict(self.context), universal_target=TARGET, **arguments)
        with patch("universal_plc.dvp_transfer.NativeProgramTransport", self.transport):
            return ProgramTransfer(self.adapter).invoke(operation, args)

    def path(self, value):
        path = Path(value)
        return path if path.is_absolute() else self.files / path

    def backup(self):
        if not self.adapter.config.get("contract_path"):
            qualified = self.qualify()
            if not qualified["ok"]:
                return qualified
        return self.call("project.upload", output_dir=str(self.files / "backup"))

    def qualify(self):
        result = self.call("program.contract.qualify", output_dir=str(self.files / "contract"))
        if result["ok"]:
            self.config.update(contract_path=str(self.path(result["contract_path"])),
                               contract_sha256=result["contract_sha256"], verified_contract=True)
        return result

    def prepare(self, backup):
        return self.call("deployment.prepare", input_path=str(self.candidate), expected_sha256=self.source_sha256,
                         backup_path=str(self.path(backup["backup_path"])),
                         expected_backup_sha256=backup["backup_sha256"])

    def execute(self, plan):
        return self.call("deployment.execute", plan_path=str(self.path(plan["plan_path"])),
                         expected_sha256=plan["plan_sha256"])


class TransferTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scenario = Scenario(self.temp.name)

    def assert_ok(self, result):
        self.assertIs(result["ok"], True, result)

    def ready_plan(self):
        backup = self.scenario.backup()
        self.assert_ok(backup)
        plan = self.scenario.prepare(backup)
        self.assert_ok(plan)
        return backup, plan

    def test_complete_backup_and_zero_wire_prepare(self):
        backup = self.scenario.backup()
        self.assert_ok(backup)
        manifest = json.loads(self.scenario.path(backup["backup_path"]).read_text("utf-8"))
        image = self.scenario.path(manifest["program_image_path"]).read_bytes()
        self.assertEqual(image, self.scenario.state.original)
        self.assertEqual(manifest["image_sha256"], sha(image))
        self.assertEqual(len(image), CAPACITY_BYTES)
        self.assertEqual(manifest["initial_value_descriptor"], [0xFFFF, 0xFFFF])
        self.scenario.state.events.clear()
        plan = self.scenario.prepare(backup)
        self.assert_ok(plan)
        self.assertFalse(self.scenario.state.events)
        self.assertFalse(self.scenario.state.write_calls)

    def test_full_transfer_independent_readback_preserves_tail_and_parameters(self):
        _, plan = self.ready_plan()
        result = self.scenario.execute(plan)
        self.assert_ok(result)
        for key in ("readback_verified", "program_verified", "stop_verified", "parameters_preserved"):
            self.assertIs(result[key], True)
        self.assertEqual(bytes(self.scenario.state.image), PAYLOAD + self.scenario.state.original[len(PAYLOAD):])
        self.assertEqual(self.scenario.state.parameters, {"D2000": [200, 201], "D2450": [10, 11]})
        self.assertFalse(self.scenario.state.running)
        self.assertGreater(self.scenario.state.events.count("read_image"), 1)

    def test_current_presence_isolation_and_enrollment_are_not_inferred(self):
        for flag in ("user_present", "heaters_isolated", "allow_program_transfer", "program_transfer_enabled"):
            with tempfile.TemporaryDirectory() as directory:
                scenario = Scenario(directory)
                scenario.config[flag] = False
                with self.subTest(flag=flag):
                    result = scenario.backup()
                    self.assertFalse(result["ok"])
                    self.assertFalse(scenario.state.events)
                    self.assertFalse(scenario.state.write_calls)

    def test_explicit_source_download_denial_overrides_host_transfer_enrollment(self):
        self.scenario.approval_data["program_download_authorized"] = False
        self.scenario.approval.write_text(json.dumps(self.scenario.approval_data), encoding="utf-8")
        self.scenario.config["approved_manifest_sha256"] = sha(self.scenario.approval.read_bytes())
        _, plan = self.ready_plan()
        self.scenario.state.events.clear()
        result = self.scenario.execute(plan)
        self.assertEqual(result["code"], "PROGRAM_DOWNLOAD_NOT_AUTHORIZED")
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.scenario.state.events)
        self.assertFalse(self.scenario.state.write_calls)

    def test_password_id_and_nonempty_initial_descriptor_block_before_mode(self):
        for key, value in (("protected", True), ("plc_id_enabled", True), ("descriptor", [0x100, 0x20])):
            with tempfile.TemporaryDirectory() as directory:
                scenario = Scenario(directory)
                self.assert_ok(scenario.qualify())
                scenario.state.events.clear()
                setattr(scenario.state, key, value)
                with self.subTest(key=key):
                    result = scenario.backup()
                    self.assertFalse(result["ok"])
                    self.assertFalse(any(isinstance(event, tuple) and event[0] == "mode" for event in scenario.state.events))
                    self.assertFalse(scenario.state.write_calls)

    def test_source_or_current_approval_changed_after_plan_blocks_before_mode(self):
        for changed in ("source", "approval"):
            with tempfile.TemporaryDirectory() as directory:
                scenario = Scenario(directory)
                backup = scenario.backup(); self.assert_ok(backup)
                plan = scenario.prepare(backup); self.assert_ok(plan)
                if changed == "source":
                    scenario.source.write_bytes(scenario.source.read_bytes() + b"UNAPPROVED REVISION")
                else:
                    data = dict(scenario.approval_data, revision="SUPERSEDED-V5")
                    scenario.approval.write_text(json.dumps(data), encoding="utf-8")
                scenario.state.events.clear()
                result = scenario.execute(plan)
                with self.subTest(changed=changed):
                    self.assertFalse(result["ok"])
                    self.assertFalse(scenario.state.events)
                    self.assertFalse(scenario.state.write_calls)

    def test_tampered_backup_or_stale_context_cannot_prepare(self):
        backup = self.scenario.backup(); self.assert_ok(backup)
        path = self.scenario.path(backup["backup_path"])
        path.write_bytes(path.read_bytes() + b" ")
        result = self.scenario.prepare(backup)
        self.assertFalse(result["ok"])
        self.assertFalse(self.scenario.state.write_calls)

    def test_qualification_requires_exact_ack_and_never_reads_program(self):
        result = self.scenario.qualify()
        self.assert_ok(result)
        self.assertEqual([e for e in self.scenario.state.events if isinstance(e, tuple)],
                         [("mode", True), ("mode", False)])
        self.assertNotIn("read_image", self.scenario.state.events)
        self.assertFalse(self.scenario.state.write_calls)
        proof = json.loads(self.scenario.path(result["contract_path"]).read_text("utf-8"))
        self.assertEqual((proof["entry_ack"], proof["exit_ack"]), ("01190101", "01190100"))
        for record in (proof, result):
            self.assertEqual(record["qualification_level"], "ACK_SHAPE_ONLY")
            self.assertIs(record["mode_effect_verified"], False)
            self.assertIs(record["program_transfer_verified"], False)
        self.assertFalse(proof["program_write"])
        self.assertTrue(proof["stop_verified"])

    def test_qualification_lost_ack_is_mode_unknown_and_explicit_recovery_only(self):
        self.scenario.state.fail_mode = True
        failed = self.scenario.qualify()
        self.assertEqual(failed["status"], "unknown_outcome")
        self.assertIs(failed["plc_program_changed"], False)
        self.assertTrue(failed["runtime_state_unknown"])
        self.assertNotIn("read_image", self.scenario.state.events)
        before = list(self.scenario.state.events)
        repeated = self.scenario.qualify()
        self.assertFalse(repeated["ok"])
        self.assertEqual(self.scenario.state.events, before)
        recovered = self.scenario.call("deployment.reconcile", journal_path=failed["journal_path"],
                                      expected_sha256=failed["journal_sha256"])
        self.assert_ok(recovered)
        self.assertEqual(recovered["program_outcome"], "observed_only")
        self.assertFalse(recovered["readback_verified"])
        self.assertFalse(self.scenario.state.write_calls)

    def test_tampered_contract_or_missing_context_blocks_before_native(self):
        _, plan = self.ready_plan()
        proof = Path(self.scenario.config["contract_path"])
        proof.write_bytes(proof.read_bytes() + b" ")
        self.scenario.state.events.clear()
        result = self.scenario.execute(plan)
        self.assertFalse(result["ok"])
        self.assertFalse(self.scenario.state.events)
        missing = ProgramTransfer(self.scenario.adapter).invoke("project.upload", dict(universal_target=TARGET))
        self.assertFalse(missing["ok"])
        self.assertFalse(self.scenario.state.events)

    def test_wrong_current_revision_context_and_native_checksum_are_rejected(self):
        backup = self.scenario.backup(); self.assert_ok(backup)
        self.scenario.state.events.clear()
        self.scenario.context["revision"] = "OBSOLETE-V5"
        self.assertFalse(self.scenario.prepare(backup)["ok"])
        self.assertFalse(self.scenario.state.events)
        self.scenario.context["revision"] = REVISION
        self.scenario.candidate.write_bytes(source_vector(checksum=0xA828))
        bad_sha = sha(self.scenario.candidate.read_bytes())
        rejected = self.scenario.call("deployment.prepare", input_path=str(self.scenario.candidate),
                                     expected_sha256=bad_sha, backup_path=backup["backup_path"],
                                     expected_backup_sha256=backup["backup_sha256"])
        self.assertFalse(rejected["ok"])
        self.assertFalse(self.scenario.state.events)

    def test_presence_or_original_source_revoked_after_mode_blocks_program_write(self):
        for change in ("presence", "source"):
            with tempfile.TemporaryDirectory() as directory:
                scenario = Scenario(directory)
                backup = scenario.backup(); self.assert_ok(backup)
                plan = scenario.prepare(backup); self.assert_ok(plan)
                def revoke(enabled):
                    if enabled:
                        if change == "presence":
                            scenario.config["user_present"] = False
                        else:
                            scenario.source.write_bytes(scenario.source.read_bytes() + b"new-unapproved-source")
                scenario.state.on_mode = revoke
                result = scenario.execute(plan)
                with self.subTest(change=change):
                    self.assertEqual(result["status"], "unknown_outcome")
                    self.assertIs(result["plc_program_changed"], False)
                    self.assertFalse(scenario.state.write_calls)

    def test_host_profile_revoked_on_disk_after_mode_is_refreshed_before_write(self):
        _, plan = self.ready_plan()
        profile = self.scenario.root / "current-host-profile.json"
        profile.write_text(json.dumps(self.scenario.config), encoding="utf-8")
        self.scenario.adapter.config_path = profile
        def revoke(enabled):
            if enabled:
                current = json.loads(profile.read_text("utf-8"))
                current["user_present"] = False
                profile.write_text(json.dumps(current), encoding="utf-8")
        self.scenario.state.on_mode = revoke
        result = self.scenario.execute(plan)
        self.assertEqual(result["code"], "ENROLLMENT_CHANGED")
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertIs(result["plc_program_changed"], False)
        self.assertFalse(self.scenario.state.write_calls)

    def test_revocation_after_program_intent_stops_actual_native_exchange_before_write(self):
        scenario = self.scenario
        scenario.config["expected_hwid"] = PortFixture.hwid
        _, plan = self.ready_plan()
        profile = scenario.root / "current-session.json"
        profile.write_text(json.dumps(scenario.config), encoding="utf-8")
        scenario.adapter.config_path = profile
        serial = SerialFixture([native_reply(0x10, bytes.fromhex("20000003"))])
        scenario.adapter.serial_factory = serial.factory
        scenario.adapter.ports_provider = lambda: [PortFixture()]
        real_native = NativeProgramTransport(scenario.adapter)
        class BoundaryTransport(SoftwareTransport):
            def write_program(self, offset_words, payload_le):
                current = json.loads(profile.read_text("utf-8"))
                current["user_present"] = False
                profile.write_text(json.dumps(current), encoding="utf-8")
                real_native.authority_guard = self.authority_guard
                real_native.write_program(offset_words, payload_le)
                super().write_program(offset_words, payload_le)
        args = dict(universal_target=TARGET, _execution_context=scenario.context,
                    plan_path=plan["plan_path"], expected_sha256=plan["plan_sha256"])
        with patch("universal_plc.dvp_transfer.NativeProgramTransport",
                   lambda adapter: BoundaryTransport(adapter, scenario.state)):
            result = ProgramTransfer(scenario.adapter).invoke("deployment.execute", args)
        self.assertEqual(result["code"], "ENROLLMENT_CHANGED")
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertFalse(result["retry_allowed"])
        self.assertEqual(serial.writes, [])
        self.assertEqual(scenario.state.write_calls, [])
        journal = json.loads(scenario.path(result["journal_path"]).read_text("utf-8"))
        self.assertTrue(journal["program_write_attempted"])
        self.assertIn("program_write_intent", [event["action"] for event in journal["events"]])
        for mutation in (lambda: real_native.mode(True), real_native.write_descriptor, real_native.finalize):
            with self.subTest(mutation=mutation), self.assertRaises(DvpSerialError) as caught:
                mutation()
            self.assertEqual(caught.exception.code, "ENROLLMENT_CHANGED")
            self.assertFalse(serial.writes)

    def test_lost_ack_reconcile_verifies_candidate_and_releases_target_lease(self):
        _, plan = self.ready_plan()
        self.scenario.state.fail_write = True
        failed = self.scenario.execute(plan)
        self.assertEqual(failed["status"], "unknown_outcome")
        count = len(self.scenario.state.write_calls)
        recovered = self.scenario.call("deployment.reconcile", journal_path=failed["journal_path"],
                                      expected_sha256=failed["journal_sha256"])
        self.assert_ok(recovered)
        self.assertEqual(recovered["program_outcome"], "candidate_current")
        self.assertTrue(recovered["readback_verified"])
        self.assertFalse(recovered["retry_allowed"])
        self.assertEqual(len(self.scenario.state.write_calls), count)
        self.assertFalse(list(Path(self.scenario.config["target_lease_dir"]).glob("target-*.json")))

    def test_reconcile_partial_image_keeps_pending_lease_and_never_rewrites(self):
        _, plan = self.ready_plan()
        self.scenario.state.fail_write = True
        failed = self.scenario.execute(plan)
        self.scenario.state.image[100] ^= 1
        count = len(self.scenario.state.write_calls)
        recovered = self.scenario.call("deployment.reconcile", journal_path=failed["journal_path"],
                                      expected_sha256=failed["journal_sha256"])
        self.assertEqual(recovered["program_outcome"], "partial_or_unknown")
        self.assertEqual(recovered["status"], "unknown_outcome")
        self.assertEqual(len(self.scenario.state.write_calls), count)
        self.assertTrue(list(Path(self.scenario.config["target_lease_dir"]).glob("target-*.json")))

    def test_read_only_reconcile_survives_external_producer_source_change(self):
        _, plan = self.ready_plan()
        self.scenario.state.fail_write = True
        failed = self.scenario.execute(plan)
        self.scenario.source.write_bytes(self.scenario.source.read_bytes() + b"NEW CURRENT PRODUCER SOURCE")
        count = len(self.scenario.state.write_calls)
        recovered = self.scenario.call("deployment.reconcile", journal_path=failed["journal_path"],
                                      expected_sha256=failed["journal_sha256"])
        self.assert_ok(recovered)
        self.assertEqual(recovered["program_outcome"], "candidate_current")
        self.assertEqual(len(self.scenario.state.write_calls), count)

    def test_consumed_plan_is_rejected_from_a_new_process_before_native(self):
        _, plan = self.ready_plan()
        self.assert_ok(self.scenario.execute(plan))
        state_path = self.scenario.root / "restart-fixture.json"
        state_path.write_text(json.dumps(dict(config=self.scenario.config, context=self.scenario.context,
                                              plan_path=str(self.scenario.path(plan["plan_path"])),
                                              plan_sha256=plan["plan_sha256"])), encoding="utf-8")
        code = '''
import json,sys
from pathlib import Path
from unittest.mock import patch
from test_transfer import SoftwareState,SoftwareTransport,Scenario,TARGET
from universal_plc.dvp_serial import DvpSerialAdapter
from universal_plc.dvp_transfer import ProgramTransfer
data=json.loads(Path(sys.argv[1]).read_text('utf-8'));state=SoftwareState()
adapter=DvpSerialAdapter(config=data['config'],serial_factory=Scenario.forbid_serial,ports_provider=lambda:[])
adapter._read=lambda device,count:[0]
with patch('universal_plc.dvp_transfer.NativeProgramTransport',lambda adapter:SoftwareTransport(adapter,state)):
 result=ProgramTransfer(adapter).invoke('deployment.execute',dict(_execution_context=data['context'],
 universal_target=TARGET,plan_path=data['plan_path'],expected_sha256=data['plan_sha256']))
print(json.dumps(dict(result=result,events=state.events,writes=len(state.write_calls))))
'''
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.run([sys.executable, "-B", "-c", code, str(state_path)], env=env,
                                 check=True, capture_output=True, text=True, timeout=15)
        returned = json.loads(process.stdout)
        self.assertEqual(returned["result"]["code"], "PLAN_ALREADY_ADMITTED")
        self.assertFalse(returned["events"])
        self.assertEqual(returned["writes"], 0)

    def test_nonzero_syntax_never_claims_verified_program(self):
        _, plan = self.ready_plan()
        self.scenario.state.syntax_code = 2
        result = self.scenario.execute(plan)
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertEqual(result["code"], "PLC_SYNTAX_FAULT")
        self.assertFalse(result.get("program_verified", False))

    def test_program_drift_since_backup_blocks_before_program_write(self):
        _, plan = self.ready_plan()
        self.scenario.state.image[100] ^= 1
        result = self.scenario.execute(plan)
        self.assertFalse(result["ok"])
        self.assertFalse(self.scenario.state.write_calls)

    def test_durable_mutation_marker_precedes_mode_and_program_write(self):
        _, plan = self.ready_plan()
        seen = []
        def inspect_durable(expected_action):
            journals = [json.loads(path.read_text("utf-8")) for path in self.scenario.files.rglob("*.json")]
            committed = [entry for entry in journals if entry.get("kind") == "ss2_deployment_journal"
                         and entry.get("operation") == "deployment.execute"
                         and entry.get("plan_sha256") == plan["plan_sha256"]
                         and entry.get("mutation_attempted") is True]
            self.assertTrue(committed, "Mutation began before the durable journal marker")
            self.assertEqual(len(committed), 1)
            self.assertEqual(committed[0]["events"][-1]["action"], expected_action)
            if expected_action == "program_write_intent":
                self.assertTrue(committed[0]["program_write_attempted"])
            seen.append(True)
        self.scenario.state.on_mode = lambda enabled: inspect_durable("mode_enter_intent" if enabled else "mode_exit_intent")
        self.scenario.state.on_write = lambda *args: inspect_durable("program_write_intent")
        result = self.scenario.execute(plan)
        self.assert_ok(result)
        self.assertTrue(seen)

    def test_journal_fsync_failure_prevents_first_native_mode(self):
        self.assert_ok(self.scenario.qualify())
        self.scenario.state.events.clear()
        with patch.object(ProgramTransfer, "_write_journal", side_effect=OSError("software disk full")):
            result = self.scenario.backup()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.scenario.state.events)
        self.assertFalse(self.scenario.state.write_calls)

    def test_reconcile_cannot_join_another_process_holding_target_lock(self):
        _, plan = self.ready_plan()
        self.scenario.state.fail_write = True
        failed = self.scenario.execute(plan)
        before = list(self.scenario.state.events)
        lock_path = next(Path(self.scenario.config["target_lease_dir"]).glob("target-*.lock"))
        ready = self.scenario.root / "lock-held.ready"
        code = '''
import os,sys
from pathlib import Path
with Path(sys.argv[1]).open('a+b') as stream:
 stream.seek(0)
 if os.name=='nt':
  import msvcrt
  msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
 else:
  import fcntl
  fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
 Path(sys.argv[2]).write_text('READY')
 sys.stdin.readline()
'''
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(lock_path), str(ready)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "Software lock-holder did not start")
            result = self.scenario.call("deployment.reconcile", journal_path=failed["journal_path"],
                                        expected_sha256=failed["journal_sha256"])
            self.assertEqual(result["code"], "TARGET_BUSY")
            self.assertEqual(self.scenario.state.events, before)
        finally:
            child.communicate("release\n", timeout=5)
            self.assertEqual(child.returncode, 0)

    def test_lost_write_ack_unknown_and_new_engine_does_not_replay(self):
        _, plan = self.ready_plan()
        self.scenario.state.fail_write = True
        failed = self.scenario.execute(plan)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["status"], "unknown_outcome")
        self.assertIs(failed["retry_allowed"], False)
        self.assertIsNot(failed.get("plc_program_changed"), False)
        count = len(self.scenario.state.write_calls)
        repeated = self.scenario.execute(plan)
        self.assertFalse(repeated["ok"])
        self.assertEqual(len(self.scenario.state.write_calls), count)

    def test_independent_program_or_parameter_mismatch_is_not_success(self):
        for flag in ("corrupt_readback", "change_parameters_on_write"):
            with tempfile.TemporaryDirectory() as directory:
                scenario = Scenario(directory)
                backup = scenario.backup(); self.assert_ok(backup)
                plan = scenario.prepare(backup); self.assert_ok(plan)
                setattr(scenario.state, flag, True)
                result = scenario.execute(plan)
                with self.subTest(flag=flag):
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], "unknown_outcome")
                    self.assertFalse(scenario.state.running)


def native_reply(function, payload, station=1):
    body = bytes([station, function]) + payload
    return b":" + (body + bytes([(-sum(body)) & 255])).hex().upper().encode() + b"\r\n"


class NativeWireContracts(unittest.TestCase):
    """Real native encoder/parser/serial reader; responses are offline byte vectors."""
    def transport(self, responses):
        serial = SerialFixture(responses)
        config = dict(enabled=True, port="COM6", station=1, baudrate=9600,
                      expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
                      expected_vid=PortFixture.vid, expected_pid=PortFixture.pid, expected_hwid=PortFixture.hwid,
                      route="directlink_cdc", allow_read_probe=True)
        adapter = DvpSerialAdapter(config=config, serial_factory=serial.factory,
                                   ports_provider=lambda: [PortFixture()])
        self.addCleanup(adapter._close)
        transport = NativeProgramTransport(adapter)
        transport.authority_guard = lambda: None  # Explicit software-only byte-vector fixture authority.
        return transport, serial

    def test_known_native_request_vectors_are_fixed_and_bounded(self):
        self.assertEqual(identity_frame(1), b":01670010000583\r\n")
        self.assertEqual(program_mode_frame(1, True), b":0164011901017F\r\n")
        self.assertEqual(program_mode_frame(1, False), b":01640119010080\r\n")
        self.assertEqual(program_read_frame(1, 0, 16), b":010320000010CC\r\n")
        for station, offset, count in ((0, 0, 16), (1, 7920, 1), (1, 7919, 2), (1, 0, 17), (1, True, 1)):
            with self.subTest(values=(station, offset, count)), self.assertRaises(ValueError):
                program_read_frame(station, offset, count)

    def test_native_identity_uses_ss2_marker_not_an_assumed_model_suffix(self):
        payload = bytes.fromhex("0a88401ef00001000e0000")
        transport, serial = self.transport([native_reply(0x67, payload)])
        identity = transport.identify()
        self.assertEqual(identity["identity_sha256"], sha(payload[1:]))
        self.assertEqual(identity["native_family"], "SS2")
        self.assertFalse(identity["exact_model_verified"])
        self.assertEqual(len(serial.writes), 1)
        bad, _ = self.transport([native_reply(0x67, bytes.fromhex("0a88401ef0000100050000"))])
        with self.assertRaises(DvpSerialError) as caught:
            bad.identify()
        self.assertEqual(caught.exception.code, "NATIVE_MODEL_MISMATCH")

    def test_private_mode_ack_must_match_payload_and_is_never_retried(self):
        missing_guard, unattempted = self.transport([])
        missing_guard.authority_guard = None
        for mutation in (lambda: missing_guard.mode(True), lambda: missing_guard.write_program(0, PAYLOAD),
                         missing_guard.write_descriptor, missing_guard.finalize):
            with self.subTest(mutation=mutation), self.assertRaises(DvpSerialError) as caught:
                mutation()
            self.assertEqual(caught.exception.code, "PROGRAM_AUTHORITY_GUARD_REQUIRED")
            self.assertFalse(unattempted.writes)
        frames = [native_reply(0x64, bytes.fromhex("01190100")), b":0164011901017E\r\n",
                  native_reply(0x64, bytes.fromhex("01190101"), station=2),
                  native_reply(0xE4, b"\x01")]
        for frame in frames:
            with self.subTest(frame=frame):
                transport, serial = self.transport([frame])
                with self.assertRaises(DvpSerialError):
                    transport.mode(True)
                self.assertEqual(serial.writes, [b":0164011901017F\r\n"])

    def test_native_program_write_keeps_le_payload_and_checks_exact_ack(self):
        transport, serial = self.transport([native_reply(0x10, bytes.fromhex("20000003"))])
        transport.write_program(0, PAYLOAD)
        raw = bytes.fromhex(serial.writes[0][1:-2].decode())
        self.assertEqual(raw[:-1], bytes.fromhex("01102000000306") + PAYLOAD)
        wrong, failed = self.transport([native_reply(0x10, bytes.fromhex("20010003"))])
        with self.assertRaises(DvpSerialError) as caught:
            wrong.write_program(0, PAYLOAD)
        self.assertEqual(caught.exception.code, "PROGRAM_WRITE_ACK_MISMATCH")
        self.assertEqual(len(failed.writes), 1)
        before = len(serial.writes)
        for offset, payload in ((0, b"x"), (-1, PAYLOAD), (7919, PAYLOAD), (0, b"x" * 34)):
            with self.subTest(offset=offset), self.assertRaises(DvpSerialError):
                transport.write_program(offset, payload)
        self.assertEqual(len(serial.writes), before)

    def test_complete_native_readback_has_exact_495_chunks_without_word_swapping(self):
        expected = b"".join(value.to_bytes(2, "little") for value in range(7920))
        responses = [native_reply(3, bytes([32]) + expected[i:i + 32]) for i in range(0, len(expected), 32)]
        transport, serial = self.transport(responses)
        self.assertEqual(transport.read_image(), expected)
        self.assertEqual(len(serial.writes), 495)
        first = bytes.fromhex(serial.writes[0][1:-2].decode())
        last = bytes.fromhex(serial.writes[-1][1:-2].decode())
        self.assertEqual(first[2:6], bytes.fromhex("20000010"))
        self.assertEqual(last[2:6], (0x2000 + 7904).to_bytes(2, "big") + b"\x00\x10")

    def test_native_descriptor_and_finalize_do_not_issue_run_or_arbitrary_writes(self):
        responses = [native_reply(3, bytes.fromhex("04ffffffff")),
                     native_reply(0x10, bytes.fromhex("15800002")),
                     native_reply(5, bytes.fromhex("0c31ff00")), native_reply(3, b"\x02\x00\x00")]
        transport, serial = self.transport(responses)
        self.assertEqual(transport.read_descriptor(), [65535, 65535])
        transport.write_descriptor()
        self.assertEqual(transport.finalize(), {"syntax_code": 0})
        bodies = [bytes.fromhex(frame[1:-2].decode())[1:-1] for frame in serial.writes]
        self.assertEqual(bodies[:3], [bytes.fromhex("0315800002"), bytes.fromhex("101580000204ffffffff"),
                                     bytes.fromhex("050c31ff00")])
        self.assertNotIn(bytes.fromhex("050c30ff00"), bodies)

    def test_current_host_route_change_closes_existing_connection_before_request(self):
        transport, serial = self.transport([native_reply(0x67, bytes.fromhex("0a88401ef00001000e0000"))])
        transport.identify()
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "current-profile.json"
            config = dict(transport.adapter.config, route="serial_ascii")
            profile.write_text(json.dumps(config), encoding="utf-8")
            transport.adapter.config_path = profile
            with self.assertRaises(DvpSerialError) as caught:
                transport.identify()
            self.assertEqual(caught.exception.code, "SESSION_PROFILE_CHANGED")
            self.assertTrue(serial.closed)
            self.assertIsNone(transport.adapter.connection)
            self.assertEqual(len(serial.writes), 1)


class ControlProfileBoundaries(unittest.TestCase):
    """Actual fixed M1072 command path; every serial byte stays inside a fixture."""
    def adapter(self, directory):
        config = dict(enabled=True, port="COM6", station=1, baudrate=9600,
                      expected_vid=PortFixture.vid, expected_pid=PortFixture.pid, expected_hwid=PortFixture.hwid,
                      route="directlink_cdc", allow_read_probe=True, allow_run_stop=True,
                      user_present=True, heaters_isolated=True,
                      expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
        profile = Path(directory) / "current-session.json"
        profile.write_text(json.dumps(config), encoding="utf-8")
        serial = SerialFixture([control_frame(1, False), native_reply(1, b"\x01\x00")])
        adapter = DvpSerialAdapter(config=config, serial_factory=serial.factory,
                                   ports_provider=lambda: [PortFixture()])
        adapter.config_path = profile
        self.addCleanup(adapter._close)
        return adapter, serial, profile, config

    def test_profile_drift_before_open_or_final_reset_emits_no_control_bytes(self):
        for moment in ("first_profile", "after_reset"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(moment=moment):
                adapter, serial, profile, config = self.adapter(directory)
                if moment == "first_profile":
                    original = adapter._profile
                    calls = []
                    def changed_profile():
                        current = original()
                        calls.append(True)
                        if len(calls) == 1:
                            profile.write_text(json.dumps(dict(current, station=2)), encoding="utf-8")
                        return current
                    adapter._profile = changed_profile
                else:
                    original_reset = serial.reset_input_buffer
                    def revoke_after_reset():
                        original_reset()
                        profile.write_text(json.dumps(dict(config, user_present=False)), encoding="utf-8")
                    serial.reset_input_buffer = revoke_after_reset
                result = adapter.invoke("target.stop", dict(universal_target=TARGET))
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(serial.writes)
                self.assertFalse(result["plc_run_state_unknown"])

    def test_profile_drift_after_ack_or_verification_read_cannot_claim_control_success(self):
        for moment in ("before_read", "after_read"):
            with tempfile.TemporaryDirectory() as directory, self.subTest(moment=moment):
                adapter, serial, profile, config = self.adapter(directory)
                original = adapter._read
                def drift_read(device, count):
                    if moment == "before_read":
                        profile.write_text(json.dumps(dict(config, station=2)), encoding="utf-8")
                    values = original(device, count)
                    if moment == "after_read":
                        profile.write_text(json.dumps(dict(config, station=2)), encoding="utf-8")
                    return values
                adapter._read = drift_read
                result = adapter.invoke("target.stop", dict(universal_target=TARGET))
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "unknown_outcome")
                self.assertTrue(result["plc_run_state_unknown"])
                self.assertFalse(result["retry_allowed"])
                self.assertFalse(result.get("state_verified", False))
                self.assertEqual([frame for frame in serial.writes if int(frame[3:5], 16) == 5],
                                 [control_frame(1, False)])
                self.assertTrue(all(bytes.fromhex(frame[1:-2].decode())[0] == 1 for frame in serial.writes))
                self.assertTrue(serial.closed)


if __name__ == "__main__":
    unittest.main()
