from __future__ import annotations

import copy
import importlib.util
import json
import re
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "backends" / "ax"
SRC_ROOT = PACKAGE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from axmcp_offline.autonomous import AutonomousHost, initialize_autonomous_root
from axmcp_offline.ladder import parse_plcopenxml, semantic_digest
from axmcp_offline.protocol_v2 import make_request_v2
from axmcp_offline.security import canonical_json_bytes, sha256_file, utc_now


ENVIRONMENT_DIGEST = "sha256:" + ("1" * 64)
WORKER_PATH = PACKAGE_ROOT / "adapters" / "ax_worker" / "worker.py"
HELPER_PATH = PACKAGE_ROOT / "adapters" / "ax_worker" / "ladder_import.py"
RECIPE_PATH = (
    PACKAGE_ROOT
    / "qualification"
    / "autonomous"
    / "fixtures"
    / "ladder_recipe.golden.json"
)
MULTI_NETWORK_RECIPE_PATH = (
    PACKAGE_ROOT / "contracts" / "v2" / "examples" / "ladder_recipe.valid.json"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _LadderObject:
    def __init__(self, name: str) -> None:
        self.name = name

    def get_name(self) -> str:
        return self.name

    def get_children(self, _recursive: bool) -> list[object]:
        return []


class _Application:
    name = "Application"
    is_application = True

    def __init__(self, project: "_Project") -> None:
        self.project = project

    def get_name(self) -> str:
        return self.name

    def get_children(self, _recursive: bool) -> list[object]:
        if self.project.owner.pou_name is None:
            return []
        return [_LadderObject(self.project.owner.pou_name)]

    def rebuild(self) -> None:
        return None

    def import_xml(
        self,
        *,
        dataOrPath: str,
        conflictResolve: object,
        import_folder_structure: bool,
    ) -> bool:
        if conflictResolve is not _ConflictResolve.Replace:
            raise AssertionError("worker did not use fixed ConflictResolve.Replace")
        if import_folder_structure is not False:
            raise AssertionError("worker widened the bounded import policy")
        if 'connectionPointOut formalParameter="none"' not in dataOrPath:
            raise AssertionError("left power rail is missing the Delta tc6_0200 formalParameter")
        recipe = parse_plcopenxml(dataOrPath)
        self.project.owner.import_calls.append(
            {
                "conflict": conflictResolve,
                "import_folder_structure": import_folder_structure,
                "recipe": recipe,
            }
        )
        self.project.owner.xml = dataOrPath
        self.project.owner.pou_name = recipe["pou"]
        self.project.dirty = True
        return self.project.owner.import_outcome


class _Project:
    name = "Scratch"

    def __init__(self, path: Path, owner: "_Projects") -> None:
        self.path = str(path)
        self.owner = owner
        self.dirty = False
        self.active_application = _Application(self)

    def get_name(self) -> str:
        return self.name

    def get_children(self, _recursive: bool) -> list[object]:
        return [self.active_application]

    def find(self, name: str, _recursive: bool) -> list[object]:
        if self.owner.project_find_enabled and self.owner.pou_name == name:
            return [_LadderObject(name)]
        return []

    def save(self) -> None:
        project_path = Path(self.path)
        project_path.write_bytes(project_path.read_bytes() + b"\nLADDER-SAVED")
        self.dirty = False

    def close(self) -> None:
        self.owner.primary = None

    def export_xml(
        self,
        *,
        objects: list[object],
        reporter: object | None,
        path: str,
        recursive: bool,
        export_folder_structure: bool,
        declarations_as_plaintext: bool,
    ) -> None:
        if (
            len(objects) != 1
            or objects[0].get_name() != self.owner.pou_name
            or reporter is not None
            or recursive is not True
            or export_folder_structure is not False
            or declarations_as_plaintext is not True
        ):
            raise AssertionError("worker widened the bounded round-trip export policy")
        if self.owner.xml is None:
            raise AssertionError("no imported Ladder XML is available")
        Path(path).write_text(self.owner.xml, encoding="utf-8")


class _Projects:
    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.primary: _Project | None = None
        self.xml: str | None = None
        self.pou_name: str | None = None
        self.project_find_enabled = True
        self.import_outcome = True
        self.import_calls: list[dict[str, object]] = []

    def open(self, path: str, primary: bool = True) -> _Project:
        if not primary or Path(path) != self.project_path:
            raise AssertionError("worker attempted to open a non-scratch project")
        self.primary = _Project(self.project_path, self)
        return self.primary


class _System:
    executable_filename = "DIADesigner-AX.exe"
    version = "1.9.0.9194"

    def get_message_categories(self, _active: bool) -> list[str]:
        return ["Build"]

    def clear_messages(self, _category: object) -> None:
        return None

    def get_message_objects(self, _category: object | None = None) -> list[object]:
        return [_BuildMessage()]


class _BuildMessage:
    severity = "Info"
    text = "Compile complete -- 0 errors, 0 warnings"
    source = "Application"
    line = 0
    column = 0


class _ConflictResolve:
    Replace = object()


def _host(root: Path) -> AutonomousHost:
    initialize_autonomous_root(root)
    config_path = root / "autonomous-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["environment_digest"] = ENVIRONMENT_DIGEST
    config_path.write_bytes(canonical_json_bytes(config))
    return AutonomousHost(root)


def _worker_config(v2: Path, workspace_id: str, baseline_digest: str) -> None:
    config = {
        "protocol": "AXQ-FILE/2",
        "config_version": "2",
        "worker_id": workspace_id,
        "environment_digest": ENVIRONMENT_DIGEST,
        "workspace_project": f"workspaces/{workspace_id}/project.project",
        "initial_project_sha256": baseline_digest,
        "ide_executable_sha256": "sha256:" + ("2" * 64),
        "script_engine_sha256": "sha256:" + ("3" * 64),
        "ladder_helper_sha256": sha256_file(HELPER_PATH),
        "ide_profile": "DIADesigner-AX 1.9",
        "allowed_operations": ["workspace.create", "ladder.networks.apply"],
        "heartbeat_seconds": 1,
        "lease_timeout_seconds": 5,
        "max_request_seconds": 300,
        "max_runtime_seconds": 300,
        "max_requests": 10,
        "idle_exit_seconds": 1,
    }
    (v2 / "worker-config.json").write_bytes(canonical_json_bytes(config))


def _setup(root: Path, conflict_api: object = _ConflictResolve):
    host = _host(root)
    baseline = host.v2 / "baselines" / "minimal.project"
    baseline.write_bytes(b"MINIMAL-AX-PROJECT")
    baseline_digest = sha256_file(baseline)
    workspace = host.workspace_create(
        "baselines/minimal.project", baseline_digest, ENVIRONMENT_DIGEST
    )
    workspace_id = workspace["workspace_id"]
    project_path = host.v2 / "workspaces" / workspace_id / "project.project"
    _worker_config(host.v2, workspace_id, baseline_digest)
    projects = _Projects(project_path)
    worker_module = _load_module(WORKER_PATH, "ax_worker_ladder_contract")
    worker = worker_module.AxWorker(
        str(host.v2),
        projects_api=projects,
        system_api=_System(),
        conflict_resolve_api=conflict_api,
    )
    preflight = make_request_v2(
        correlation_id=str(uuid.uuid4()),
        job_id=str(uuid.uuid4()),
        operation="workspace.create",
        created_at=utc_now(),
        timeout_seconds=60,
        environment_digest=ENVIRONMENT_DIGEST,
        workspace_digest=None,
        expected_revision=None,
        authorization_digest=None,
        artifacts=[],
        scratch_root=host.v2,
    )
    host.queue.submit(preflight)
    if not worker.process_next():
        raise AssertionError("worker did not consume dependency preflight")
    preflight_result = json.loads(
        (host.v2 / "results" / f"{preflight['job_id']}.result.json").read_bytes()
    )
    if preflight_result["status"] != "succeeded":
        raise AssertionError(preflight_result["diagnostics"])
    host.ingest_worker_result(preflight_result)
    return host, worker, projects, workspace_id, baseline_digest


class AxWorkerLadderDispatchTests(unittest.TestCase):
    def test_roundtrip_rejects_second_implementation_body(self) -> None:
        helper = _load_module(HELPER_PATH, "ax_worker_ladder_helper_second_body")
        recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
        root = ET.fromstring(helper.render_plcopenxml(recipe))
        pou = helper._pou(root, recipe["pou"])
        injected_body = ET.SubElement(pou, helper._q("body"))
        injected_st = ET.SubElement(injected_body, helper._q("ST"))
        injected_st.text = "Injected := TRUE;"

        with self.assertRaisesRegex(helper.LadderImportError, "exactly one body"):
            helper.verify_roundtrip(ET.tostring(root, encoding="utf-8"), recipe)

    def test_roundtrip_rejects_mixed_ld_and_st_body(self) -> None:
        helper = _load_module(HELPER_PATH, "ax_worker_ladder_helper_mixed_body")
        recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
        root = ET.fromstring(helper.render_plcopenxml(recipe))
        pou = helper._pou(root, recipe["pou"])
        body = pou.find(helper._q("body"))
        self.assertIsNotNone(body)
        injected_st = ET.SubElement(body, helper._q("ST"))
        injected_st.text = "Injected := TRUE;"

        with self.assertRaisesRegex(helper.LadderImportError, "exactly one LD"):
            helper.verify_roundtrip(ET.tostring(root, encoding="utf-8"), recipe)

    def test_multi_network_nc_parallel_writer_round_trip_is_exact(self) -> None:
        helper = _load_module(HELPER_PATH, "ax_worker_ladder_helper_parallel")
        recipe = json.loads(MULTI_NETWORK_RECIPE_PATH.read_text(encoding="utf-8"))
        rendered = helper.render_plcopenxml(recipe)
        verified = helper.verify_roundtrip(rendered, recipe)
        self.assertEqual(verified["network_count"], 2)

        root = ET.fromstring(rendered)
        pou = helper._pou(root, recipe["pou"])
        coil = next(
            node
            for node in pou.findall("./%s/%s/%s" % (
                helper._q("body"), helper._q("LD"), helper._q("coil")
            ))
            if node.findtext(helper._q("variable")) == "RestartRequired"
        )
        point = coil.find(helper._q("connectionPointIn"))
        self.assertIsNotNone(point)
        connections = point.findall(helper._q("connection"))
        self.assertEqual(len(connections), 2)
        point.remove(connections[-1])
        tampered = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        with self.assertRaisesRegex(
            helper.LadderImportError, "topology|orphan semantic nodes"
        ):
            helper.verify_roundtrip(tampered, recipe)

    def test_helper_matches_canonical_renderer_grammar_and_rejects_topology_tamper(self) -> None:
        helper = _load_module(HELPER_PATH, "ax_worker_ladder_helper_contract")
        recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
        rendered = helper.render_plcopenxml(recipe)
        self.assertEqual(parse_plcopenxml(rendered.decode("utf-8")), recipe)
        self.assertEqual(helper.semantic_digest(recipe), semantic_digest(recipe))
        self.assertEqual(helper.verify_roundtrip(rendered, recipe)["network_count"], 1)
        input_index = rendered.index(b"<inputVariables>")
        inout_index = rendered.index(b"<inOutVariables />", input_index)
        output_index = rendered.index(b"<outputVariables>", inout_index)
        self.assertLess(input_index, inout_index)
        self.assertLess(inout_index, output_index)

        delta_root = ET.fromstring(rendered)
        delta_pou = helper._pou(delta_root, recipe["pou"])
        delta_ld = delta_pou.find("./%s/%s" % (helper._q("body"), helper._q("LD")))
        self.assertIsNotNone(delta_ld)
        comment = delta_ld.find(helper._q("comment"))
        left_rail = delta_ld.find(helper._q("leftPowerRail"))
        self.assertIsNotNone(comment)
        self.assertIsNotNone(left_rail)
        delta_ld.remove(comment)
        delta_ld.insert(list(delta_ld).index(left_rail) + 1, comment)
        vendor = delta_ld.find(helper._q("vendorElement"))
        self.assertIsNotNone(vendor)
        element_type = vendor.find(
            "./%s/%s/ElementType" % (helper._q("addData"), helper._q("data"))
        )
        self.assertIsNotNone(element_type)
        right_rail = delta_ld.find(helper._q("rightPowerRail"))
        self.assertIsNotNone(right_rail)
        right_point = right_rail.find(helper._q("connectionPointIn"))
        self.assertIsNotNone(right_point)
        for connection in list(right_point.findall(helper._q("connection"))):
            right_point.remove(connection)
        delta_ordered = ET.tostring(delta_root, encoding="utf-8")
        self.assertEqual(
            helper.verify_roundtrip(delta_ordered, recipe)["network_count"], 1
        )
        element_type.text = "arbitrary-semantic-extension"
        vendor_tampered = ET.tostring(delta_root, encoding="utf-8")
        with self.assertRaisesRegex(
            helper.LadderImportError, "unsupported vendor extension"
        ):
            helper.verify_roundtrip(vendor_tampered, recipe)
        element_type.text = "networktitle"
        vendor.set("localId", left_rail.get("localId"))
        duplicate_local_id = ET.tostring(delta_root, encoding="utf-8")
        with self.assertRaisesRegex(helper.LadderImportError, "localId.*duplicated"):
            helper.verify_roundtrip(duplicate_local_id, recipe)

        tampered = rendered.replace(
            b'<connection refLocalId="4" />',
            b'<connection refLocalId="3" />',
            1,
        )
        with self.assertRaisesRegex(
            helper.LadderImportError, "topology|orphan semantic nodes"
        ):
            helper.verify_roundtrip(tampered, recipe)
        invalid = dict(recipe)
        invalid["unknown"] = True
        with self.assertRaises(helper.LadderImportError):
            helper.validate_recipe(invalid)

    def test_roundtrip_rejects_removed_fb_writers_and_swapped_network_labels(self) -> None:
        helper = _load_module(HELPER_PATH, "ax_worker_ladder_helper_semantic_tamper")
        recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
        rendered = helper.render_plcopenxml(recipe)
        without_writers = re.sub(
            rb"<outVariable\b.*?</outVariable>", b"", rendered, flags=re.DOTALL
        )
        with self.assertRaisesRegex(helper.LadderImportError, "topology"):
            helper.verify_roundtrip(without_writers, recipe)

        two_networks = {
            "schema_version": "2",
            "record_type": "LadderRecipe",
            "output_allowlist": ["RunReq", "StopReq"],
            "project": "SyntheticProject",
            "pou": "MachineSupervisor",
            "variables": [
                {
                    "name": "Enable",
                    "type": "BOOL",
                    "direction": "input",
                    "comment": "Bounded test permit.",
                },
                {
                    "name": "RunReq",
                    "type": "BOOL",
                    "direction": "output",
                    "comment": "Bounded run request.",
                },
                {
                    "name": "StopReq",
                    "type": "BOOL",
                    "direction": "output",
                    "comment": "Bounded stop request.",
                },
            ],
            "networks": [],
        }
        first = {
            "id": "MSUP-RUN",
            "order": 1,
            "title": "Run permit",
            "comment": "Independent first semantic network.",
            "elements": [
                {"kind": "contact", "variable": "Enable", "polarity": "NO"}
            ],
            "coil": {"variable": "RunReq", "mode": "normal"},
        }
        second = copy.deepcopy(first)
        second["id"] = "MSUP-STOP"
        second["order"] = 2
        second["title"] = "Stop permit"
        second["comment"] = "Independent second semantic network."
        second["coil"]["variable"] = "StopReq"
        two_networks["networks"] = [first, second]
        rendered_two = helper.render_plcopenxml(two_networks)
        first_label = helper._network_label(two_networks["networks"][0]).encode("utf-8")
        second_label = helper._network_label(two_networks["networks"][1]).encode("utf-8")
        placeholder = b"AXMCP_NETWORK_LABEL_SWAP_PLACEHOLDER"
        swapped = rendered_two.replace(first_label, placeholder, 1)
        swapped = swapped.replace(second_label, first_label, 1)
        swapped = swapped.replace(placeholder, second_label, 1)
        with self.assertRaisesRegex(helper.LadderImportError, "network identity"):
            helper.verify_roundtrip(swapped, two_networks)

        initialized_root = ET.fromstring(rendered_two)
        initialized_pou = helper._pou(initialized_root, two_networks["pou"])
        enable = initialized_pou.find(
            "./%s/%s/%s[@name='Enable']"
            % (helper._q("interface"), helper._q("inputVars"), helper._q("variable"))
        )
        self.assertIsNotNone(enable)
        initial_value = ET.SubElement(enable, helper._q("initialValue"))
        ET.SubElement(initial_value, helper._q("simpleValue"), {"value": "TRUE"})
        with self.assertRaisesRegex(helper.LadderImportError, "declarations changed"):
            helper.verify_roundtrip(
                ET.tostring(initialized_root, encoding="utf-8"), two_networks
            )

        inout_root = ET.fromstring(rendered_two)
        inout_pou = helper._pou(inout_root, two_networks["pou"])
        interface = inout_pou.find(helper._q("interface"))
        self.assertIsNotNone(interface)
        ET.SubElement(interface, helper._q("inOutVars"))
        with self.assertRaisesRegex(helper.LadderImportError, "unsupported interface group"):
            helper.verify_roundtrip(
                ET.tostring(inout_root, encoding="utf-8"), two_networks
            )

        pin_root = ET.fromstring(rendered)
        pin_pou = helper._pou(pin_root, recipe["pou"])
        block_pin = pin_pou.find(
            ".//%s/%s" % (helper._q("inputVariables"), helper._q("variable"))
        )
        self.assertIsNotNone(block_pin)
        block_pin.set("negated", "true")
        with self.assertRaisesRegex(helper.LadderImportError, "topology"):
            helper.verify_roundtrip(ET.tostring(pin_root, encoding="utf-8"), recipe)

        edge_root = ET.fromstring(rendered_two)
        edge_pou = helper._pou(edge_root, two_networks["pou"])
        edge_contact = edge_pou.find(".//%s" % helper._q("contact"))
        self.assertIsNotNone(edge_contact)
        edge_contact.set("edge", "rising")
        with self.assertRaisesRegex(helper.LadderImportError, "topology"):
            helper.verify_roundtrip(
                ET.tostring(edge_root, encoding="utf-8"), two_networks
            )

        call_type_root = ET.fromstring(rendered)
        call_type_pou = helper._pou(call_type_root, recipe["pou"])
        call_type_block = call_type_pou.find(".//%s" % helper._q("block"))
        self.assertIsNotNone(call_type_block)
        block_add_data = call_type_block.find(helper._q("addData"))
        self.assertIsNotNone(block_add_data)
        block_add_data.clear()
        call_data = ET.SubElement(
            block_add_data,
            helper._q("data"),
            {
                "name": "http://www.3s-software.com/plcopenxml/fbdcalltype",
                "handleUnknown": "implementation",
            },
        )
        call_type = ET.SubElement(call_data, "CallType")
        call_type.text = "functionblock"
        self.assertEqual(
            helper.verify_roundtrip(
                ET.tostring(call_type_root, encoding="utf-8"), recipe
            )["network_count"],
            1,
        )
        call_type.text = "function"
        with self.assertRaisesRegex(helper.LadderImportError, "call type"):
            helper.verify_roundtrip(
                ET.tostring(call_type_root, encoding="utf-8"), recipe
            )

        plaintext_root = ET.fromstring(rendered)
        plaintext_pou = helper._pou(plaintext_root, recipe["pou"])
        plaintext = plaintext_pou.find(
            "./%s/%s[@name='http://www.3s-software.com/plcopenxml/interfaceasplaintext']/%s/%s"
            % (
                helper._q("addData"),
                helper._q("data"),
                helper._q("InterfaceAsPlainText"),
                helper._xhtml("xhtml"),
            )
        )
        self.assertIsNotNone(plaintext)
        plaintext.text = (plaintext.text or "").replace(
            "PROGRAM PulseSequence", "PROGRAM TamperedProgram"
        )
        with self.assertRaisesRegex(helper.LadderImportError, "plaintext declaration changed"):
            helper.verify_roundtrip(
                ET.tostring(plaintext_root, encoding="utf-8"), recipe
            )

        moved_root = ET.fromstring(rendered_two)
        moved_pou = helper._pou(moved_root, two_networks["pou"])
        moved_ld = moved_pou.find(
            "./%s/%s" % (helper._q("body"), helper._q("LD"))
        )
        self.assertIsNotNone(moved_ld)
        comments = moved_ld.findall(helper._q("comment"))
        contacts = moved_ld.findall(helper._q("contact"))
        self.assertEqual(len(comments), 2)
        self.assertEqual(len(contacts), 2)
        first_contact = contacts[0]
        moved_ld.remove(first_contact)
        second_comment_index = list(moved_ld).index(comments[1])
        moved_ld.insert(second_comment_index + 2, first_contact)
        with self.assertRaisesRegex(helper.LadderImportError, "network boundary"):
            helper.verify_roundtrip(
                ET.tostring(moved_root, encoding="utf-8"), two_networks
            )

    def test_real_host_and_worker_apply_save_reopen_export_and_ingest(self) -> None:
        recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            host, worker, projects, workspace_id, baseline_digest = _setup(Path(temporary))
            queued = host.ladder_networks_apply(workspace_id, 0, recipe)
            request = json.loads(
                (
                    host.v2 / "inbox" / f"{queued['correlation_id']}.request.json"
                ).read_bytes()
            )
            descriptor = request["artifacts"][0]
            self.assertEqual(descriptor["role"], "input")
            self.assertEqual(descriptor["sha256"], sha256_file(host.v2 / descriptor["path"]))
            self.assertTrue(worker.process_next())
            result = json.loads(
                (host.v2 / "results" / f"{queued['job_id']}.result.json").read_bytes()
            )
            self.assertEqual(result["status"], "succeeded", result["diagnostics"])
            self.assertEqual(result["correlation_id"], queued["correlation_id"])
            self.assertEqual(result["job_id"], queued["job_id"])
            self.assertEqual(result["before_project_digest"], baseline_digest)
            self.assertEqual(result["revision"], 1)
            for artifact in result["artifacts"]:
                self.assertEqual(artifact["sha256"], sha256_file(host.v2 / artifact["path"]))
            payload_descriptor = next(
                artifact for artifact in result["artifacts"] if artifact["role"] == "output"
            )
            payload = json.loads((host.v2 / payload_descriptor["path"]).read_bytes())
            self.assertTrue(payload["reopen_verified"])
            self.assertTrue(payload["semantic_roundtrip_verified"])
            self.assertEqual(payload["recipe_digest"], semantic_digest(recipe))
            roundtrip = host.v2 / payload["roundtrip_artifact"]["path"]
            self.assertEqual(parse_plcopenxml(roundtrip.read_text(encoding="utf-8")), recipe)
            self.assertEqual(len(projects.import_calls), 1)
            self.assertIs(projects.import_calls[0]["conflict"], _ConflictResolve.Replace)
            host.ingest_worker_result(result)
            self.assertEqual(host.job_status(queued["job_id"])["status"], "succeeded")
            self.assertEqual(host.workspace_status(workspace_id)["revision"], 1)

    def test_imported_application_pou_is_found_by_bounded_tree_fallback(self) -> None:
        recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            host, worker, projects, workspace_id, _digest = _setup(Path(temporary))
            projects.project_find_enabled = False
            projects.import_outcome = False
            queued = host.ladder_networks_apply(workspace_id, 0, recipe)
            self.assertTrue(worker.process_next())
            result = json.loads(
                (host.v2 / "results" / f"{queued['job_id']}.result.json").read_bytes()
            )
            self.assertEqual(result["status"], "succeeded", result["diagnostics"])

    def test_digest_tamper_and_missing_conflict_api_fail_closed_before_import(self) -> None:
        recipe = json.loads(RECIPE_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            host, worker, projects, workspace_id, _digest = _setup(Path(temporary))
            queued = host.ladder_networks_apply(workspace_id, 0, recipe)
            request_path = host.v2 / "inbox" / f"{queued['correlation_id']}.request.json"
            request = json.loads(request_path.read_bytes())
            (host.v2 / request["artifacts"][0]["path"]).write_bytes(b"{}\n")
            self.assertTrue(worker.process_next())
            result = json.loads(
                (host.v2 / "results" / f"{queued['job_id']}.result.json").read_bytes()
            )
            self.assertEqual(result["status"], "blocked")
            self.assertIn("digest mismatch", result["diagnostics"][0]["message"])
            self.assertEqual(projects.import_calls, [])

        with tempfile.TemporaryDirectory() as temporary:
            host, worker, projects, workspace_id, _digest = _setup(Path(temporary))
            worker.conflict_resolve = None
            queued = host.ladder_networks_apply(workspace_id, 0, recipe)
            self.assertTrue(worker.process_next())
            result = json.loads(
                (host.v2 / "results" / f"{queued['job_id']}.result.json").read_bytes()
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["diagnostics"][0]["code"], "AX.SP18.CAPABILITY")
            self.assertEqual(projects.import_calls, [])

    def test_worker_and_helper_expose_no_eval_shell_or_generic_import_tool(self) -> None:
        source = WORKER_PATH.read_text(encoding="utf-8")
        helper = HELPER_PATH.read_text(encoding="utf-8")
        for text in (source, helper):
            self.assertNotIn("eval(", text)
            self.assertNotIn("exec(", text)
            self.assertNotIn("subprocess", text)
            self.assertNotIn("os.system", text)
        self.assertIn('"ladder.networks.apply"', source)
        self.assertNotIn("dataOrPath=arguments", source)


if __name__ == "__main__":
    unittest.main()
