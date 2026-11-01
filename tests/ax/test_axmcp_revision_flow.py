"""Focused offline native-revision transaction and fail-closed tests."""
from __future__ import annotations

import json
import copy
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from axmcp_offline.autonomous import (
    AutonomousHost, configure_autonomous_environment, initialize_autonomous_root,
)
from axmcp_offline.ax_supervisor import AxWorkerSupervisor
from axmcp_offline.security import ProtocolViolation, atomic_write_replace, canonical_json_bytes, sha256_bytes
from axmcp_offline.protocol_v2 import _object_path
from tests.ax.offline_foundation.test_ax_worker import (
    Languages, Message, Node, PouTypes, Projects, System, WorkerFixture, _load_worker,
    _json_bytes,
)


class RevisionProjects(Projects):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        rows = [
            {"name": name, "declaration": f"PROGRAM {name}",
             "implementation": "old := TRUE;", "is_gvl": False}
            for name in ("Alpha", "Beta", "Untouched")
        ]
        self.store[:] = [dict(row) for row in rows]
        self.snapshots[path.read_bytes()] = [dict(row) for row in rows]
        self.fail_save_after_bytes = False
        self.fail_close_after_save = False

    def open(self, path: str, primary: bool = True):
        self.path = Path(path)
        project = super().open(path, primary=primary)
        project._children = [Node("Controller_1", [project.application])]

        def export_xml(**kwargs):
            root = ET.Element("project")
            ET.SubElement(root, "fileHeader", {"creationDateTime": "volatile"})
            ET.SubElement(root, "contentHeader", {"modificationDateTime": "volatile"})
            config = ET.SubElement(root, "data", {"name": "device-config"})
            config.text = "locked"
            for source in project.sources:
                tag = "globalVars" if source.is_global_variable_list else "pou"
                item = ET.SubElement(root, tag, {"name": source.name})
                ET.SubElement(item, "declaration").text = source.textual_declaration.text
                ET.SubElement(item, "implementation").text = source.textual_implementation.text
            Path(str(kwargs["path"])).write_bytes(ET.tostring(root, encoding="utf-8"))

        project.export_xml = export_xml
        original_save = project.save

        def save():
            original_save()
            if self.fail_save_after_bytes:
                raise RuntimeError("save failed after generation bytes changed")

        project.save = save
        original_close = project.close

        def close():
            if self.fail_close_after_save and not project.dirty and self.path.name == "project.project":
                raise RuntimeError("close failed after save")
            original_close()

        project.close = close
        return project


def _change(name: str, implementation: str) -> dict[str, object]:
    return {
        "object_path": ["Controller_1", "Application", name],
        "pou_kind": "program", "return_type": None,
        "declaration": f"PROGRAM {name}", "implementation": implementation,
    }


def ladder_recipe():
    return {"schema_version": "2", "record_type": "LadderRecipe", "project": "ExampleProject", "output_allowlist": ["RunReq"],
            "pou": "MachineSupervisor", "variables": [
                {"name": "JogRequest", "type": "BOOL", "direction": "input", "comment": "request"},
                {"name": "RunReq", "type": "BOOL", "direction": "output", "comment": "output"}],
            "networks": [{"id": "BENCH-JOG", "order": 1, "title": "Synthetic jog",
                          "comment": "Unscheduled fixture.",
                          "elements": [{"kind": "contact", "variable": "JogRequest", "polarity": "NO"}],
                          "coil": {"variable": "RunReq", "mode": "normal"}}]}


def native_fixture(name="MachineSupervisor", guid="target-guid", body="NO"):
    root = ET.Element("ExportFile")
    meta = ET.SubElement(root, "Single", Name="MetaObject")
    for key, value in (("Name", name), ("Guid", guid), ("ParentGuid", "parent-guid"), ("Timestamp", "1")):
        ET.SubElement(meta, "Single", Name=key).text = value
    obj = ET.SubElement(root, "Single", Name="Object", Type="program")
    interface = ET.SubElement(obj, "Single", Name="Interface")
    ET.SubElement(interface, "Single", Name="TextBlobForSerialisation").text = (
        "PROGRAM " + name + "\nVAR_INPUT\nJogRequest : BOOL;\nEND_VAR")
    ET.SubElement(interface, "Single", Name="LineInfoPersistence").text = guid
    ET.SubElement(obj, "Single", Name="Implementation").text = body
    ET.SubElement(obj, "Single", Name="UniqueIdGenerator").text = "42"
    return ET.tostring(root, encoding="utf-8")


class NativeRevisionTests(unittest.TestCase):
    def test_subset_host_and_worker_match_and_reject_unsupported_graphs(self):
        from axmcp_offline.ladder import validate_revision_subset
        helper = self.module.ladder_import
        recipe = ladder_recipe()
        self.assertEqual(validate_revision_subset(recipe), helper.validate_revision_subset(recipe))
        for kind in ("contact", "coil", "variables", "networks"):
            bad = copy.deepcopy(recipe)
            if kind == "contact":
                bad["networks"][0]["elements"] *= 2
            elif kind == "coil":
                bad["networks"][0]["coil"]["mode"] = "set"
            elif kind == "variables":
                bad["variables"].append({"name": "Spare", "type": "BOOL", "direction": "input", "comment": ""})
            else:
                bad["networks"].append({**copy.deepcopy(bad["networks"][0]), "id": "BENCH-TWO", "order": 2})
            for validator in (validate_revision_subset, helper.validate_revision_subset):
                with self.subTest(kind=kind, validator=validator), self.assertRaises(ValueError):
                    validator(bad)
        requested = copy.deepcopy(recipe)
        requested["networks"][0]["elements"][0]["polarity"] = "NC"
        helper.verify_revision_baseline(helper.render_plcopenxml(recipe), requested)
        requested["networks"][0]["comment"] = "changed comment is outside qualified baseline"
        with self.assertRaises(ValueError):
            helper.verify_revision_baseline(helper.render_plcopenxml(recipe), requested)

    def test_native_preparation_preserves_target_metadata_and_rejects_donor_leaks(self):
        helper = self.module.ladder_import
        original = native_fixture()
        donor = native_fixture("Donor", "donor-guid", "NC")
        prepared = helper.prepare_native_revision(original, donor, "MachineSupervisor", "Donor", "donor-guid")
        self.assertEqual(helper.native_metadata(original), helper.native_metadata(prepared))
        self.assertIn(b">NC<", prepared)
        self.assertEqual(helper.native_identity(prepared)["Guid"], "target-guid")
        for kind in ("interface", "allocator", "missing", "duplicate", "name_leak", "guid_leak", "type"):
            bad = ET.fromstring(donor)
            obj = bad.find("./Single[@Name='Object']")
            if kind == "interface":
                obj.find(".//Single[@Name='TextBlobForSerialisation']").text += "\nX : BOOL;"
            elif kind == "allocator":
                obj.find("./Single[@Name='UniqueIdGenerator']").text = "43"
            elif kind == "missing":
                obj.remove(obj.find("./Single[@Name='UniqueIdGenerator']"))
            elif kind == "duplicate":
                ET.SubElement(obj, "Single", Name="Implementation").text = "extra"
            elif kind == "type":
                obj.set("Type", "other")
            else:
                obj.find("./Single[@Name='Implementation']").text = "Donor" if kind == "name_leak" else "donor-guid"
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                helper.prepare_native_revision(original, ET.tostring(bad), "MachineSupervisor", "Donor", "donor-guid")
        changed = original.replace(b"parent-guid", b"other-parent")
        self.assertNotEqual(helper.native_metadata(original), helper.native_metadata(changed))
        self.assertEqual(helper.native_metadata(original), helper.native_metadata(original.replace(b">1<", b">2<")))
        turkish = original.replace(b"NO", "Çene üfleme".encode())
        self.assertEqual(helper.native_metadata(turkish), helper.native_metadata(turkish.decode()))
        self.assertEqual(helper.native_metadata(turkish), helper.native_metadata(b"\xef\xbb\xbf" + turkish))
        crlf_target = original.replace(b"Supervisor\n", b"Supervisor&#13;\n")
        crlf_donor = donor.replace(b"Donor\n", b"Donor&#13;\n")
        helper.prepare_native_revision(crlf_target, crlf_donor, "MachineSupervisor", "Donor", "donor-guid")

    def test_callback_validation_requires_exact_actual_replace(self):
        helper = self.module.ladder_import
        rows = [{"event": "filter", "name": "MachineSupervisor", "guid": "g", "path": ["Application"], "valid": True},
                {"event": "conflict", "name": "MachineSupervisor", "guid": "g", "valid": True},
                {"event": "progress", "name": "MachineSupervisor", "guid": "g", "valid": True}]
        helper.validate_native_callbacks(rows, "MachineSupervisor", "g", ["Application"])
        bad_rows = [rows[:1], rows + [rows[1]], rows + [{"event": "skipped"}]]
        for key, value in (("guid", "foreign"), ("valid", False), ("path", ["ForeignApplication"])):
            bad = copy.deepcopy(rows)
            bad[0][key] = value
            bad_rows.append(bad)
        for bad in bad_rows:
            with self.subTest(rows=bad), self.assertRaises(ValueError):
                helper.validate_native_callbacks(bad, "MachineSupervisor", "g", ["Application"])

    def test_unqualified_ladder_rejected_before_project_open(self):
        self.worker._open_project = lambda: self.fail("must not open or mutate")
        bad = ladder_recipe()
        bad["networks"][0]["elements"] *= 2
        with self.assertRaisesRegex(ValueError, "LD_REVISION_UNSUPPORTED"):
            self.worker._revision_apply({}, {"ladder_changes": [{"recipe": bad}]})

    def test_ladder_prepare_failure_does_not_promote_prior_st_edit(self):
        before = self.fixture.workspace.read_bytes()
        self.worker._revision_ladder_prepare = lambda *args: (_ for _ in ()).throw(
            self.module.WorkerError("injected native import failure"))
        job, result = self.submit([_change("Alpha", "new := TRUE;")], [{
            "object_path": ["Controller_1", "Application", "MachineSupervisor"],
            "recipe": ladder_recipe()}])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(before, self.fixture.workspace.read_bytes())
        self.assertTrue(self.worker._generation_poisoned)
        self.assertFalse((self.root / "state" / "revision-transactions" / job / "receipt.json").exists())

    def test_ladder_reopen_failure_does_not_promote_saved_generation(self):
        before = self.fixture.workspace.read_bytes()
        self.worker._revision_ladder_prepare = lambda *args: {}
        self.worker._revision_ladder_readback = lambda *args: (_ for _ in ()).throw(
            self.module.WorkerError("injected reopened metadata drift"))
        _job, result = self.submit([_change("Alpha", "new := TRUE;")], [{
            "object_path": ["Controller_1", "Application", "MachineSupervisor"],
            "recipe": ladder_recipe()}])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(before, self.fixture.workspace.read_bytes())
        self.assertEqual(self.projects.events.count(("save",)), 1)

    def test_pinned_optional_warning_exception_is_exact_and_preserves_raw_messages(self):
        self.assertIsNone(self.module.OPTIONAL_EMPTY_SLOT_BASELINE_COUNT)
        self.module.OPTIONAL_EMPTY_SLOT_BASELINE_COUNT = 21
        self.module.OPTIONAL_EMPTY_SLOT_SECOND_BASELINE_SHA256 = "sha256:" + "9" * 64
        cases = ("exact", "20", "22", "extra", "source", "non_iec_source", "code",
                 "line", "compiler_warning", "unknown_baseline")
        for case in cases:
            with self.subTest(case=case):
                count = int(case) if case in ("20", "22") else 21
                messages = [Message("Warning", "Device description for '<Empty>' is missing")
                            for _ in range(count)]
                for msg in messages:
                    msg.source = None
                    msg.line = msg.column = 0
                    msg.prefix = msg.number = None
                if case in ("source", "non_iec_source"):
                    messages[0].source = "Application" if case == "source" else "Device path/invalid IEC"
                if case == "code":
                    messages[0].prefix = "C"
                if case == "line":
                    messages[0].line = -1
                if case == "extra":
                    messages.append(Message("Warning", "new distinct warning"))
                self.worker.config["initial_project_sha256"] = (
                    "sha256:" + "a" * 64 if case == "unknown_baseline"
                    else self.module.OPTIONAL_EMPTY_SLOT_SECOND_BASELINE_SHA256)
                self.worker.system = System(messages, summary_warnings=1 if case == "compiler_warning" else 0)
                diagnostics = self.worker._messages(strict=True)
                raw = self.worker._last_raw_message_evidence
                self.assertEqual(raw[0]["diagnostic"]["severity"], "warning")
                if case == "exact":
                    self.assertEqual(self.worker._verified_build_counts(diagnostics), (0, 0))
                    self.assertEqual(sum(x["code"] == "AX.BASELINE.OPTIONAL_EMPTY_SLOT"
                                         for x in diagnostics), 21)
                else:
                    with self.assertRaises(self.module.VendorCapabilityError):
                        self.worker._verified_build_counts(diagnostics)

    def test_stale_exact_shutdown_is_not_consumed_by_replacement_worker(self):
        path = self.root / "control" / "shutdown.request.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"protocol": "AXQ-FILE/2", "worker_id": self.fixture.worker_id,
                 "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "reason": "explicit_workspace_close", "pid": os.getpid(),
                 "process_start_fingerprint": "sha256:" + "0" * 64}
        path.write_bytes(_json_bytes(value))
        before = path.read_bytes()
        with self.assertRaisesRegex(self.module.WorkerError, "different worker launch"):
            self.worker._shutdown_requested()
        self.assertEqual(path.read_bytes(), before)

    def test_xml_bom_is_encoding_only_and_untouched_change_still_rejected(self):
        before = self.root / "before.xml"
        after = self.root / "after.xml"
        xml = '<project><pou name="Alpha"><body>old</body></pou><data>locked</data><text>Çene, rölanti, üfleme</text></project>'.encode("utf-8")
        before.write_bytes(b"\xef\xbb\xbf" + xml)
        after.write_bytes(xml.replace(b">old<", b">new<"))
        self.worker._revision_unchanged_xml(str(before), str(after), {"Alpha"})
        after.write_bytes(xml.replace(b">locked<", b">changed<"))
        with self.assertRaises(self.module.WorkerError):
            self.worker._revision_unchanged_xml(str(before), str(after), {"Alpha"})

    def test_vendor_container_space_does_not_relax_leaf_or_path_boundaries(self):
        self.assertEqual(_object_path(["Controller_1", "Plc Logic", "Application", "Alpha"], "path")[1], "Plc Logic")
        for invalid in ("../Plc Logic", "Plc/Logic", "Plc\\Logic", " Plc Logic", "Plc Logic ", " ", "Plc\nLogic"):
            with self.subTest(invalid=invalid), self.assertRaises(ProtocolViolation):
                _object_path(["Controller_1", invalid, "Alpha"], "path")
        with self.assertRaises(ProtocolViolation):
            _object_path(["Controller_1", "Plc Logic", "Alpha Beta"], "path")

    def setUp(self) -> None:
        self.module = _load_worker()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = WorkerFixture(self.root)
        self.fixture.config["allowed_operations"].append("revision.apply")
        (self.root / "worker-config.json").write_bytes(_json_bytes(self.fixture.config))
        self.projects = RevisionProjects(self.fixture.workspace)
        self.worker = self.module.AxWorker(
            str(self.root), projects_api=self.projects, system_api=System(),
            languages_api=Languages(), pou_types_api=PouTypes(),
        )

    def submit(self, changes: list[dict[str, object]], ladder=None) -> tuple[str, dict[str, object]]:
        _correlation, job = self.fixture.request(
            "revision.apply",
            payload={
                "workspace_id": self.fixture.worker_id, "expected_revision": 0,
                "idempotency_key": "revision-fixture-1", "st_changes": changes,
                "ladder_changes": ladder or [],
            },
            workspace_digest=self.fixture.project_digest(), expected_revision=0,
        )
        self.assertTrue(self.worker.process_next())
        result = json.loads((self.root / "results" / f"{job}.result.json").read_text())
        return job, result

    def test_two_st_changes_commit_once_to_closed_native_generation(self) -> None:
        before = self.fixture.workspace.read_bytes()
        job, result = self.submit([_change("Alpha", "new := TRUE;"), _change("Beta", "new := FALSE;")])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["revision"], 1)
        self.assertNotEqual(self.fixture.workspace.read_bytes(), before)
        self.assertEqual(self.projects.events.count(("save",)), 1)
        receipt = json.loads((self.root / "state" / "revision-transactions" / job / "receipt.json").read_text())
        self.assertTrue(receipt["closed"])
        self.assertTrue(receipt["reopen_verified"])
        self.assertEqual(receipt["save_count"], 1)
        self.assertEqual(receipt["st_count"], 2)
        self.assertEqual(len(receipt["st_readback"]), 2)

    def test_second_item_absent_does_not_save_or_promote_first(self) -> None:
        before = self.fixture.workspace.read_bytes()
        job, result = self.submit([_change("Alpha", "changed := TRUE;"), _change("Missing", "bad := TRUE;")])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.fixture.workspace.read_bytes(), before)
        self.assertEqual(self.projects.events.count(("save",)), 0)
        self.assertFalse((self.root / "state" / "revision-transactions" / job / "receipt.json").exists())
        self.assertTrue(self.worker._generation_poisoned)
        self.assertFalse(self.worker.process_next())

    def test_save_raises_after_generation_write_original_stays_unchanged(self) -> None:
        before = self.fixture.workspace.read_bytes()
        self.projects.fail_save_after_bytes = True
        job, result = self.submit([_change("Alpha", "changed := TRUE;")])
        self.assertNotEqual(result["status"], "succeeded")
        self.assertEqual(self.fixture.workspace.read_bytes(), before)
        self.assertFalse((self.root / "state" / "revision-transactions" / job / "receipt.json").exists())
        self.assertTrue(self.worker._generation_poisoned)

    def test_close_failure_after_save_never_promotes(self) -> None:
        before = self.fixture.workspace.read_bytes()
        self.projects.fail_close_after_save = True
        job, result = self.submit([_change("Alpha", "changed := TRUE;")])
        self.assertNotEqual(result["status"], "succeeded")
        self.assertEqual(self.fixture.workspace.read_bytes(), before)
        self.assertFalse((self.root / "state" / "revision-transactions" / job / "receipt.json").exists())

    def test_wrong_full_path_is_blocked(self) -> None:
        before = self.fixture.workspace.read_bytes()
        wrong = _change("Alpha", "changed := TRUE;")
        wrong["object_path"] = ["Controller_2", "Application", "Alpha"]
        _job, result = self.submit([wrong])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.fixture.workspace.read_bytes(), before)

    def test_duplicate_leaf_name_is_fail_closed_even_if_path_is_full(self) -> None:
        original = self.projects.open

        def open_with_ambiguous_find(path: str, primary: bool = True):
            project = original(path, primary)
            first = project.sources[0]
            project._children.append(Node("Controller_2", [Node("Application", [first])]))
            return project

        self.projects.open = open_with_ambiguous_find
        before = self.fixture.workspace.read_bytes()
        _job, result = self.submit([_change("Alpha", "changed := TRUE;")])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.fixture.workspace.read_bytes(), before)

    def test_wrong_application_guid_is_blocked_even_when_names_match(self) -> None:
        original = self.projects.open

        def open_wrong_guid(path: str, primary: bool = True):
            project = original(path, primary)
            project.application.guid = "ACTIVE_APPLICATION"
            other = Node("Application", project.application.get_children(False))
            other.guid = "OTHER_APPLICATION"
            project._children[0]._children[0] = other
            return project

        self.projects.open = open_wrong_guid
        before = self.fixture.workspace.read_bytes()
        _job, result = self.submit([_change("Alpha", "changed := TRUE;")])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.fixture.workspace.read_bytes(), before)

    def test_first_build_after_edit_only_revision_uses_real_build_path(self) -> None:
        _job, result = self.submit([_change("Alpha", "compiled := TRUE;")])
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse((self.root / "state" / "dependency-preflight.json").exists())
        self.fixture.config["allowed_operations"].append("project.build") if "project.build" not in self.fixture.config["allowed_operations"] else None
        correlation, _build_job = self.fixture.request(
            "project.build",
            payload={
                "workspace_id": self.fixture.worker_id, "expected_revision": 1,
                "mode": "build", "max_attempts": 1,
                "runtime_release_digest": "sha256:" + "a" * 64,
            },
            workspace_digest=self.fixture.project_digest(), expected_revision=1,
        )
        request = json.loads((self.root / "inbox" / f"{correlation}.request.json").read_text())
        calls = []

        def actual_build_path(kind, request, workspace_id, runtime_release_digest):
            calls.append((kind, request["expected_revision"], workspace_id))
            return {"status": "passed"}

        self.worker._build = actual_build_path
        artifacts, status, _diagnostics = self.worker._execute(request)
        self.assertEqual(status, "succeeded")
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(calls, [("build", 1, self.fixture.worker_id)])
        self.assertEqual(self.projects.rebuild_calls, 0)


class NativeCandidateHostTests(unittest.TestCase):
    def test_old_shutdown_blocks_spawn_until_explicit_reconciliation(self):
        supervisor = object.__new__(AxWorkerSupervisor)
        supervisor.v2 = self.host.v2
        supervisor._live_lease = lambda _: None
        supervisor._job_launch_records = lambda *_: []
        path = self.host.v2 / "control" / "shutdown.request.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"old":"unreconciled"}')
        supervisor._write_worker_config = lambda *_: self.fail("spawn preflight passed")
        job = "18927035-bfbf-42df-bdfb-acf9a9b09943"
        with self.assertRaisesRegex(ProtocolViolation, "unreconciled AX shutdown request"):
            supervisor.ensure_running(self.workspace, job, _launcher_fenced=True)
        # Simulate separately authorized exact-old-launch reconciliation by
        # archiving evidence, never silently deleting it inside ensure_running.
        path.rename(path.with_name("shutdown.reconciled.json"))
        class ReachedConfig(Exception):
            pass
        def reached_config(*_):
            raise ReachedConfig()
        supervisor._write_worker_config = reached_config
        with self.assertRaises(ReachedConfig):
            supervisor.ensure_running(self.workspace, job, _launcher_fenced=True)

    def test_shutdown_does_not_overwrite_different_launch_request(self):
        supervisor = object.__new__(AxWorkerSupervisor)
        supervisor.v2 = self.host.v2
        supervisor._load_workspace = lambda _: ({"pending_job_id": None}, None)
        supervisor._live_lease = lambda _: {
            "pid": 4242, "process_start_fingerprint": "sha256:" + "2" * 64}
        supervisor.guard_health = lambda _: {"pid": 4242}
        path = self.host.v2 / "control" / "shutdown.request.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes({
            "worker_id": self.workspace, "pid": 4241,
            "process_start_fingerprint": "sha256:" + "1" * 64}))
        before = path.read_bytes()
        with self.assertRaisesRegex(ProtocolViolation, "stale AX shutdown request"):
            supervisor.request_shutdown(self.workspace)
        self.assertEqual(path.read_bytes(), before)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        initialize_autonomous_root(self.root)
        configure_autonomous_environment(self.root, "sha256:" + "1" * 64)
        self.host = AutonomousHost(self.root)
        baseline = self.host.v2 / "baselines" / "mini.project"
        baseline.write_bytes(b"NATIVE_BASELINE")
        self.digest = sha256_bytes(baseline.read_bytes())
        self.workspace = self.host.workspace_create(
            "baselines/mini.project", self.digest, self.host.environment_digest,
        )["workspace_id"]

    def test_snapshot_rejects_unverified_native_workspace(self) -> None:
        with self.assertRaisesRegex(ProtocolViolation, "no verified native revision receipt"):
            self.host.candidate_snapshot(self.workspace, 0)

    def test_second_workspace_rejected_before_copy(self) -> None:
        before = sorted(p.name for p in (self.host.v2 / "workspaces").iterdir())
        with self.assertRaisesRegex(ProtocolViolation, "one workspace only"):
            self.host.workspace_create("baselines/mini.project", self.digest,
                                       self.host.environment_digest)
        self.assertEqual(before, sorted(p.name for p in (self.host.v2 / "workspaces").iterdir()))

    def test_unqualified_ladder_rejected_before_idempotency_or_queue(self):
        with patch("axmcp_offline.autonomous.validate_arguments"):
            with self.assertRaisesRegex(ProtocolViolation, "LD_REVISION_UNSUPPORTED"):
                self.host.revision_apply(self.workspace, 0, "ld-unqualified", [], [{}])
        self.assertFalse((self.host.v2 / "workspaces" / self.workspace / "idempotency").exists())
        self.assertIsNone(self.host._load_workspace(self.workspace)["pending_job_id"])

    def test_workspace_creation_race_is_fenced(self) -> None:
        with self.host._workspace_fence("00000000-0000-4000-8000-000000000000"):
            with self.assertRaisesRegex(ProtocolViolation, "fence is held"):
                self.host.workspace_create("baselines/mini.project", self.digest,
                                           self.host.environment_digest)

    def test_legacy_multi_workspace_root_blocks_mutation_not_read(self) -> None:
        (self.host.v2 / "workspaces" / "00000000-0000-0000-0000-000000000001").mkdir()
        self.assertEqual(self.host.workspace_status(self.workspace)["workspace_id"], self.workspace)
        with self.assertRaisesRegex(ProtocolViolation, "one workspace only"):
            self.host.project_build(self.workspace, 0, "rebuild", 1)

    def test_legacy_multi_workspace_root_blocks_launch_before_adoption(self) -> None:
        (self.host.v2 / "workspaces" / "00000000-0000-4000-8000-000000000001").mkdir()
        supervisor = object.__new__(AxWorkerSupervisor)
        supervisor.v2 = self.host.v2
        supervisor._live_lease = lambda *_: self.fail("must not adopt or launch")
        with self.assertRaisesRegex(ProtocolViolation, "one workspace only"):
            supervisor.ensure_running(self.workspace,
                "18927035-bfbf-42df-bdfb-acf9a9b09943", _launcher_fenced=True)

    def test_multi_workspace_rejects_before_reservation_snapshot_or_freeze_write(self):
        (self.host.v2 / "workspaces" / "00000000-0000-4000-8000-000000000001").mkdir()
        before = {str(p.relative_to(self.host.v2)): p.read_bytes()
                  for p in self.host.v2.rglob("*") if p.is_file()}
        operations = (
            lambda: self.host.revision_apply(self.workspace, 0, "legacy-root", [_change("Alpha", "x := 1;")], []),
            lambda: self.host.candidate_snapshot(self.workspace, 0),
            lambda: self.host.artifact_freeze(self.workspace, 0),
        )
        for operation in operations:
            with self.subTest(operation=operation), self.assertRaisesRegex(
                    ProtocolViolation, "one workspace only"):
                operation()
            after = {str(p.relative_to(self.host.v2)): p.read_bytes()
                     for p in self.host.v2.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
        self.assertFalse((self.host.v2 / "workspaces" / self.workspace / "idempotency").exists())

    def test_snapshot_requires_closed_receipt_and_matching_bytes(self) -> None:
        manifest = self.host._load_workspace(self.workspace)
        manifest["revision"] = 1
        job = "18927035-bfbf-42df-bdfb-acf9a9b09943"
        transaction = self.host.v2 / "state" / "revision-transactions" / job
        transaction.mkdir(parents=True)
        (transaction / "project.project").write_bytes(b"NATIVE_BASELINE")
        receipt = {
            "record_type": "RevisionClosedReceipt", "job_id": job,
            "workspace_id": self.workspace, "expected_revision": 0,
            "before_project_digest": self.digest, "after_project_digest": self.digest,
            "environment_digest": self.host.environment_digest,
            "closed": True, "reopen_verified": True, "save_count": 1,
        }
        (transaction / "receipt.json").write_bytes(canonical_json_bytes(receipt))
        manifest["last_native_save"] = receipt
        self.host._write_workspace(manifest)
        candidate = self.host.candidate_snapshot(self.workspace, 1)
        self.assertEqual(candidate["status"], "NATIVE_SAVED_UNCOMPILED")
        self.assertFalse(candidate["deployable"])
        self.assertIsNone(candidate["build_digest"])
        self.assertEqual((self.host.v2 / candidate["path"]).read_bytes(), b"NATIVE_BASELINE")
        (transaction / "receipt.json").write_bytes(canonical_json_bytes({**receipt, "closed": False}))
        with self.assertRaisesRegex(ProtocolViolation, "missing or changed"):
            self.host.candidate_snapshot(self.workspace, 1)

    def test_same_idempotency_key_reuses_job_and_different_payload_rejects(self) -> None:
        change = _change("Alpha", "new := TRUE;")
        first = self.host.revision_apply(self.workspace, 0, "same-key", [change], [])
        same = self.host.revision_apply(self.workspace, 0, "same-key", [change], [])
        self.assertEqual(same["job_id"], first["job_id"])
        self.assertTrue(same["idempotent_replay"])
        with self.assertRaisesRegex(ProtocolViolation, "different revision payload"):
            self.host.revision_apply(self.workspace, 0, "same-key", [_change("Alpha", "different := TRUE;")], [])

    def test_prequeue_reservation_prevents_blind_replay(self) -> None:
        def fail_before_queue(*args, **kwargs):
            raise RuntimeError("interrupted before queue publication")

        self.host._queue_workspace_operation = fail_before_queue
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.host.revision_apply(self.workspace, 0, "prequeue-key", [_change("Alpha", "x := TRUE;")], [])
        identity_files = list((self.host.v2 / "workspaces" / self.workspace / "idempotency").glob("*.json"))
        self.assertEqual(len(identity_files), 1)
        self.assertEqual(len(identity_files[0].stem), 32)
        with self.assertRaisesRegex(ProtocolViolation, "no reconcilable queue state"):
            self.host.revision_apply(self.workspace, 0, "prequeue-key", [_change("Alpha", "x := TRUE;")], [])
        with self.assertRaisesRegex(ProtocolViolation, "unreconciled revision reservation"):
            self.host.revision_apply(self.workspace, 0, "another-key", [_change("Alpha", "y := TRUE;")], [])

    def test_stale_revision_and_workspace_drift_do_not_reserve_a_key(self) -> None:
        identity_dir = self.host.v2 / "workspaces" / self.workspace / "idempotency"
        with self.assertRaisesRegex(ProtocolViolation, "stable revision"):
            self.host.revision_apply(self.workspace, 1, "stale-key", [_change("Alpha", "x := TRUE;")], [])
        self.assertFalse(identity_dir.exists())
        project = self.host.v2 / "workspaces" / self.workspace / "project.project"
        project.write_bytes(b"UNEXPECTED_CHANGE")
        with self.assertRaisesRegex(ProtocolViolation, "outside the worker protocol"):
            self.host.revision_apply(self.workspace, 0, "drift-key", [_change("Alpha", "x := TRUE;")], [])
        self.assertFalse(identity_dir.exists())

    def test_same_key_cannot_race_the_workspace_fence(self) -> None:
        change = _change("Alpha", "new := TRUE;")
        with self.host._workspace_fence(self.workspace):
            with self.assertRaisesRegex(ProtocolViolation, "fence is held"):
                self.host.revision_apply(self.workspace, 0, "race-key", [change], [])
        self.assertFalse((self.host.v2 / "workspaces" / self.workspace / "idempotency").exists())
        first = self.host.revision_apply(self.workspace, 0, "race-key", [change], [])
        replay = self.host.revision_apply(self.workspace, 0, "race-key", [change], [])
        self.assertEqual(replay["job_id"], first["job_id"])

    def test_snapshot_reconciles_candidate_after_manifest_write_interruption(self) -> None:
        manifest = self.host._load_workspace(self.workspace)
        manifest["revision"] = 1
        job = "18927035-bfbf-42df-bdfb-acf9a9b09943"
        transaction = self.host.v2 / "state" / "revision-transactions" / job
        transaction.mkdir(parents=True)
        (transaction / "project.project").write_bytes(b"NATIVE_BASELINE")
        receipt = {
            "record_type": "RevisionClosedReceipt", "job_id": job,
            "workspace_id": self.workspace, "expected_revision": 0,
            "before_project_digest": self.digest, "after_project_digest": self.digest,
            "environment_digest": self.host.environment_digest,
            "closed": True, "reopen_verified": True, "save_count": 1,
        }
        (transaction / "receipt.json").write_bytes(canonical_json_bytes(receipt))
        manifest["last_native_save"] = receipt
        self.host._write_workspace(manifest)
        original = self.host._write_workspace

        def interrupted(_manifest):
            raise RuntimeError("manifest interrupted after candidate bytes")

        self.host._write_workspace = interrupted
        with self.assertRaisesRegex(RuntimeError, "manifest interrupted"):
            self.host.candidate_snapshot(self.workspace, 1)
        self.host._write_workspace = original
        candidate = self.host.candidate_snapshot(self.workspace, 1)
        self.assertEqual((self.host.v2 / candidate["path"]).read_bytes(), b"NATIVE_BASELINE")
        self.assertEqual(len(self.host._load_workspace(self.workspace)["native_candidates"]), 1)

    def test_cancel_queued_revision_clears_pending_workspace_job(self) -> None:
        queued = self.host.revision_apply(
            self.workspace, 0, "cancel-key", [_change("Alpha", "x := TRUE;")], [],
        )
        self.host.job_cancel(queued["job_id"])
        self.assertIsNone(self.host._load_workspace(self.workspace)["pending_job_id"])

    def test_partial_generation_is_not_replayed_after_worker_loss(self) -> None:
        supervisor = object.__new__(AxWorkerSupervisor)
        supervisor.v2 = self.host.v2
        supervisor._job_queue_state = lambda _job: "running"
        supervisor._live_lease = lambda _workspace: None
        supervisor._launch_process_active = lambda _workspace, _job: False
        supervisor.ensure_running = lambda *_args: self.fail("partial generation replayed")
        job = "18927035-bfbf-42df-bdfb-acf9a9b09943"
        transaction = self.host.v2 / "state" / "revision-transactions" / job
        transaction.mkdir(parents=True)
        (transaction / "intent.json").write_bytes(b"partial")
        outcome = supervisor.recover_workspace_job(self.workspace, job, self.digest, "revision.apply")
        self.assertEqual(outcome["status"], "blocked")
        project = self.host.v2 / "workspaces" / self.workspace / "project.project"
        project.write_bytes(b"CHANGED_CANONICAL")
        outcome = supervisor.recover_workspace_job(self.workspace, job, self.digest, "revision.apply")
        self.assertEqual(outcome["status"], "unknown_outcome")

    def test_active_guard_without_lease_cannot_attest_snapshot(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows guard evidence")
        supervisor = object.__new__(AxWorkerSupervisor)
        supervisor.v2 = self.host.v2
        supervisor.config = {"lease_timeout_seconds": 10}
        supervisor._latest_workspace_launch = lambda _workspace: {
            "pid": 4242, "window_guard_pid": 9001,
            "window_guard_status": "evidence/stale.window-guard.json",
        }
        supervisor._live_lease = lambda _workspace: None
        evidence = self.host.v2 / "evidence" / "stale.window-guard.json"
        evidence.write_bytes(canonical_json_bytes({
            "record_type": "AxWindowGuard", "pid": 4242,
            "status": "active", "checked_at": "2020-01-01T00:00:00Z",
        }))
        with self.assertRaisesRegex(ProtocolViolation, "no live bound guard"):
            supervisor.guard_health(self.workspace)

    def test_freeze_rechecks_project_digest_after_exact_shutdown(self) -> None:
        manifest = self.host._load_workspace(self.workspace)
        manifest["revision"] = 1
        build_job = "18927035-bfbf-42df-bdfb-acf9a9b09943"
        build_digest = "sha256:" + "b" * 64
        manifest["last_successful_build"] = {
            "job_id": build_job, "revision": 1,
            "project_digest": self.digest, "completed_at": "2026-10-20T00:00:00Z",
            "build_digest": build_digest,
            "runtime_release_digest": "sha256:" + "a" * 64,
        }
        self.host._write_workspace(manifest)
        (self.host.v2 / "results" / f"{build_job}.result.json").write_bytes(
            canonical_json_bytes({"job_id": build_job})
        )
        project = self.host.v2 / "workspaces" / self.workspace / "project.project"

        class ClosingSupervisor:
            def launcher_fence(self):
                return nullcontext()

            def _latest_workspace_launch(self, _workspace):
                return {"pid": 4242}

            def request_shutdown(self, _workspace):
                return {"status": "requested"}

            def wait_for_idle_shutdown(self, _workspace):
                project.write_bytes(b"DRIFT_AFTER_CLOSE")
                return {"status": "stopped"}

            def _live_lease(self, _workspace):
                return None

        self.host.supervisor = ClosingSupervisor()
        self.host._load_build_result = lambda *_args: ({}, build_digest)
        with self.assertRaisesRegex(ProtocolViolation, "changed during AX shutdown"):
            self.host.artifact_freeze(self.workspace, 1)
        self.assertFalse(list((self.host.v2 / "artifacts").glob("*.project")))

    def test_guard_status_replace_retries_only_bounded_windows_sharing_errors(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows replace behavior")
        target = self.root / "guard.json"
        original_replace = os.replace
        calls = []

        def transient(source, destination):
            calls.append(1)
            if len(calls) == 1:
                error = OSError("sharing violation")
                error.winerror = 32
                raise error
            return original_replace(source, destination)

        with patch("axmcp_offline.security.os.replace", side_effect=transient):
            atomic_write_replace(target, b"ok", replace_retry_seconds=0.1)
        self.assertEqual(target.read_bytes(), b"ok")
        self.assertEqual(len(calls), 2)

        def denied(_source, _destination):
            error = OSError("not a sharing/access error")
            error.winerror = 87
            raise error

        with patch("axmcp_offline.security.os.replace", side_effect=denied):
            with self.assertRaises(OSError):
                atomic_write_replace(target, b"bad", replace_retry_seconds=0.1)
        self.assertFalse((self.root / "guard.json.partial").exists())

if __name__ == "__main__":
    unittest.main()
