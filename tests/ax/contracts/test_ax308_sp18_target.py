from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = ROOT / "backends" / "ax" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from axmcp_offline import ax_deployment_supervisor


WORKER_PATH = (
    ROOT
    / "backends" / "ax"
    / "adapters"
    / "ax_deployment_worker"
    / "worker.py"
)
ADAPTER_PATH = WORKER_PATH.with_name("ax308_sp18_target.py")

ISOLATION_DIGEST = "sha256:" + "1" * 64
CANDIDATE_RELEASE_DIGEST = "sha256:" + "2" * 64
ROLLBACK_RELEASE_DIGEST = "sha256:" + "3" * 64
OTHER_DIGEST = "sha256:" + "5" * 64
GATEWAY_GUID = "01234567-89ab-cdef-0123-456789abcdef"
APPLICATION_GUID = "11111111-2222-3333-4444-555555555555"


def _load_worker():
    spec = importlib.util.spec_from_file_location("ax308_target_worker_contract", WORKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_digest(value: object) -> str:
    raw = (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _route_digest() -> str:
    return _canonical_digest(
        {
            "schema": "DIADESIGNER_AX_ROUTE/1",
            "gateway_guid": GATEWAY_GUID,
            "device_address": "[056D]",
            "device_name": "AX308-LAB-001",
            "tracking_mode": "systemaddress",
        }
    )


def _fingerprint(
    *,
    device_identity: str = "4102/16f7 0319/1.0.9.2",
    runtime_version: str = "3.5.18.40",
    route_digest: str | None = None,
    application_identity: str | None = None,
) -> str:
    return _canonical_digest(
        {
            "schema": "DIADESIGNER_AX308_SP18_TARGET_FINGERPRINT/1",
            "model": "AX-308",
            "device_identity": device_identity,
            "runtime_version": runtime_version,
            "device_description_version": "1.0.9.2",
            "gateway_route_digest": route_digest or _route_digest(),
            "application_identity": application_identity
            or (
                "Controller_1/Plc Logic/Application|guid=" + APPLICATION_GUID
            ),
            "forced_values_present": False,
            "physical_outputs_energized": False,
            "isolation_evidence_digest": ISOLATION_DIGEST,
        }
    )


class ChangeOptions:
    Keep = "KEEP"
    Never = "NEVER"


class ApplicationStates:
    run = "run"
    stop = "stop"


class DeviceIdentification:
    def __init__(self) -> None:
        self.type = 4102
        self.id = "16f7 0319"
        self.version = "1.0.9.2"


class CommunicationSettings:
    def __init__(self) -> None:
        self.gateway_guid = "{" + GATEWAY_GUID.upper() + "}"
        self.device_address = "[056D]"
        self.device_name = "AX308-LAB-001"
        self.TrackingMode = "DeviceTrackingMode.SystemAddress"
        self.scanned_target_name = "Delta AX-308EA0MA1T"
        self.scanned_target_type = "4102"
        self.scanned_target_id = "16F7   0319"
        self.scanned_target_version = "3.5.18.40"


class NamedObject:
    is_root = False
    is_device = False
    is_application = False
    is_active_application = False

    def __init__(self, name: str, parent=None) -> None:
        self._name = name
        self.parent = parent

    def get_name(self) -> str:
        return self._name


class Controller(NamedObject):
    is_device = True

    def __init__(self, parent, events: list[tuple]) -> None:
        super().__init__("Controller_1", parent)
        self.events = events
        self.identification = DeviceIdentification()
        self.settings = CommunicationSettings()

    def get_device_identification(self):
        self.events.append(("device", "get_device_identification"))
        return self.identification

    def get_device_communication_settings(self):
        self.events.append(("device", "get_device_communication_settings"))
        return self.settings

    def get_gateway(self):
        self.events.append(("device", "get_gateway"))
        return self.settings.gateway_guid

    def get_address(self):
        self.events.append(("device", "get_address"))
        return self.settings.device_address


class Application(NamedObject):
    is_application = True

    def __init__(self, parent, project, *, active: bool = True) -> None:
        super().__init__("Application", parent)
        self.project = project
        self.guid = "{" + APPLICATION_GUID.upper() + "}"
        self.is_active_application = active


class Project:
    is_root = True
    primary = True

    def __init__(self, events: list[tuple]) -> None:
        self.events = events
        self.controller = Controller(self, events)
        self.logic = NamedObject("Plc Logic", self.controller)
        self.active_application = Application(self.logic, self)
        self.children = [self.controller, self.logic, self.active_application]

    def get_children(self, recursive: bool = False):
        self.events.append(("project", "get_children", recursive))
        return list(self.children)


class Projects:
    def __init__(self, project: Project) -> None:
        self.primary = project


class OnlineApplication:
    def __init__(self, owner: "Fixture", *, logged_in: bool = False) -> None:
        self.owner = owner
        self.is_logged_in = logged_in
        self.application_state = owner.online_state
        self.timeout = None

    def login(self, option, delete_foreign_apps: bool) -> None:
        self.owner.events.append(("online", "login", option, delete_foreign_apps))
        if option != ChangeOptions.Keep or delete_foreign_apps is not False:
            raise AssertionError("adapter attempted a non-Keep login")
        self.is_logged_in = True

    def get_forced_expressions(self):
        self.owner.events.append(("online", "get_forced_expressions"))
        return tuple(self.owner.forced_expressions)

    def read_values(self, expressions):
        self.owner.events.append(("online", "read_values", tuple(expressions)))
        return list(self.owner.marker_values)

    def logout(self) -> None:
        self.owner.events.append(("online", "logout"))
        self.is_logged_in = False

    def Dispose(self) -> None:
        self.owner.events.append(("online", "Dispose"))

    def start(self) -> None:  # pragma: no cover - safety tripwire
        raise AssertionError("adapter must not start an application")

    def stop(self) -> None:  # pragma: no cover - safety tripwire
        raise AssertionError("adapter must not stop an application")

    def write_prepared_values(self) -> None:  # pragma: no cover - safety tripwire
        raise AssertionError("adapter must not write values")


class Online:
    def __init__(self, owner: "Fixture") -> None:
        self.owner = owner

    def create_online_application(self, application):
        self.owner.events.append(("online", "create_online_application", application))
        return OnlineApplication(self.owner)


class Fixture:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.project = Project(self.events)
        self.projects = Projects(self.project)
        self.online_state = ApplicationStates.run
        self.forced_expressions: list[str] = []
        self.marker_values = ["'" + CANDIDATE_RELEASE_DIGEST + "'"]
        self.enrollment = {
            "schema_version": "1",
            "record_type": "TargetEnrollment",
            "status": "enrolled",
            "captured_at": "2026-09-03T00:00:00Z",
            "model": "AX-308",
            "device_identity": "4102/16f7 0319/1.0.9.2",
            "runtime_version": "3.5.18.40",
            "firmware_version": "1.0.9.2",
            "gateway_route_digest": _route_digest(),
            "application_identity": (
                "Controller_1/Plc Logic/Application|guid=" + APPLICATION_GUID
            ),
            "forced_values_present": False,
            "physical_outputs_energized": False,
            "isolation_evidence_digest": ISOLATION_DIGEST,
            "fingerprint": _fingerprint(),
        }

    def context(self) -> dict:
        return {
            "root": str(ROOT),
            "enrollment": self.enrollment,
            "projects": self.projects,
            "online": Online(self),
            "system": object(),
            "OnlineChangeOption": ChangeOptions,
            "ApplicationState": ApplicationStates,
            "VersionUpdateFlags": object(),
            "CredentialSourceKind": object(),
        }


class Ax308Sp18TargetContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = _load_worker()
        cls.adapter_digest = cls.worker._sha256_file(str(ADAPTER_PATH.resolve()))

    def load_adapter(self, fixture: Fixture):
        context = fixture.context()
        self.assertNotIn("target", context)
        return self.worker._load_target_adapter(
            str(ADAPTER_PATH.resolve()), self.adapter_digest, context
        )

    def test_production_loader_derives_identity_route_versions_and_fingerprint(self) -> None:
        fixture = Fixture()
        target = self.load_adapter(fixture)

        record = target.probe(fixture.project.active_application)

        self.assertEqual(record["model"], "AX-308")
        self.assertEqual(record["device_identity"], "4102/16f7 0319/1.0.9.2")
        self.assertEqual(record["runtime_version"], "3.5.18.40")
        self.assertEqual(record["firmware_version"], "1.0.9.2")
        self.assertEqual(record["gateway_route_digest"], _route_digest())
        self.assertIs(record["forced_values_present"], False)
        self.assertIs(record["physical_outputs_energized"], True)
        self.assertIs(fixture.enrollment["physical_outputs_energized"], False)
        self.assertEqual(
            record["application_identity"],
            "Controller_1/Plc Logic/Application|guid=" + APPLICATION_GUID,
        )
        self.assertEqual(record["fingerprint"], _fingerprint())
        self.assertEqual(record["fingerprint"], fixture.enrollment["fingerprint"])
        self.assertIn(("device", "get_device_identification"), fixture.events)
        self.assertIn(("device", "get_device_communication_settings"), fixture.events)
        self.assertIn(("device", "get_gateway"), fixture.events)
        self.assertIn(("device", "get_address"), fixture.events)
        self.assertFalse(any(event[0] == "online" for event in fixture.events))

    def test_enrollment_fingerprint_tamper_is_rejected_during_factory_load(self) -> None:
        fixture = Fixture()
        fixture.enrollment["fingerprint"] = OTHER_DIGEST
        with self.assertRaisesRegex(Exception, "fingerprint"):
            self.load_adapter(fixture)

    def test_wrong_or_missing_ax308_identity_fields_fail_closed(self) -> None:
        cases = {
            "device-type": lambda f: setattr(f.project.controller.identification, "type", 4096),
            "device-id": lambda f: setattr(
                f.project.controller.identification, "id", "16f7 9999"
            ),
            "description-version": lambda f: setattr(
                f.project.controller.identification, "version", "1.0.9.1"
            ),
            "missing-description-version": lambda f: setattr(
                f.project.controller.identification, "version", None
            ),
            "target-model": lambda f: setattr(
                f.project.controller.settings, "scanned_target_name", "Control Win"
            ),
            "target-type": lambda f: setattr(
                f.project.controller.settings, "scanned_target_type", "4096"
            ),
            "target-id": lambda f: setattr(
                f.project.controller.settings, "scanned_target_id", "16f7 9999"
            ),
            "missing-runtime": lambda f: setattr(
                f.project.controller.settings, "scanned_target_version", ""
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                fixture = Fixture()
                target = self.load_adapter(fixture)
                mutate(fixture)
                with self.assertRaises(Exception):
                    target.probe(fixture.project.active_application)

    def test_route_or_application_tamper_cannot_reuse_enrollment_fingerprint(self) -> None:
        cases = {
            "route": lambda f: setattr(
                f.project.controller.settings, "device_address", "[9999]"
            ),
            "application-guid": lambda f: setattr(
                f.project.active_application,
                "guid",
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                fixture = Fixture()
                target = self.load_adapter(fixture)
                mutate(fixture)
                with self.assertRaisesRegex(Exception, "mismatch"):
                    target.probe(fixture.project.active_application)

    def test_current_application_requires_one_primary_active_application(self) -> None:
        fixture = Fixture()
        target = self.load_adapter(fixture)
        second = Application(fixture.project.logic, fixture.project, active=True)
        fixture.project.children.append(second)
        with self.assertRaisesRegex(Exception, "unique active"):
            target.current_application()

        fixture = Fixture()
        target = self.load_adapter(fixture)
        fixture.project.primary = False
        with self.assertRaisesRegex(Exception, "primary project"):
            target.current_application()

    def test_verify_running_artifact_is_fixed_read_only_and_accepts_stop_or_run(self) -> None:
        for state in (ApplicationStates.stop, ApplicationStates.run):
            with self.subTest(state=state):
                fixture = Fixture()
                target = self.load_adapter(fixture)
                fixture.online_state = state
                application = OnlineApplication(fixture, logged_in=True)
                self.assertIs(
                    target.verify_running_artifact(
                        application, CANDIDATE_RELEASE_DIGEST
                    ),
                    True,
                )
                self.assertEqual(
                    [event[1] for event in fixture.events if event[0] == "online"],
                    ["get_forced_expressions", "read_values"],
                )
                read = next(event for event in fixture.events if event[:2] == ("online", "read_values"))
                self.assertEqual(
                    read[2],
                    ("AXMCP_RuntimeMarker.ReleaseDigest",),
                )

    def test_verify_running_artifact_rejects_absent_mismatch_force_and_unknown_state(self) -> None:
        cases = {
            "absent": lambda f: setattr(f, "marker_values", []),
            "release-mismatch": lambda f: setattr(
                f, "marker_values", [OTHER_DIGEST]
            ),
            "forced": lambda f: f.forced_expressions.append("PLC_PRG.Test"),
            "unknown-state": lambda f: setattr(f, "online_state", "unknown"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                fixture = Fixture()
                target = self.load_adapter(fixture)
                mutate(fixture)
                application = OnlineApplication(fixture, logged_in=True)
                with self.assertRaises(Exception):
                    target.verify_running_artifact(
                        application, CANDIDATE_RELEASE_DIGEST
                    )

    def test_reconcile_uses_keep_marker_reads_and_blocks_unobservable_outputs(self) -> None:
        fixture = Fixture()
        target = self.load_adapter(fixture)

        result = target.reconcile(
            CANDIDATE_RELEASE_DIGEST, ROLLBACK_RELEASE_DIGEST
        )

        self.assertEqual(result["target_fingerprint"], _fingerprint())
        self.assertEqual(
            result["runtime_release_digest"], CANDIDATE_RELEASE_DIGEST
        )
        self.assertEqual(result["application_state"], "run")
        self.assertIs(result["forced_values_present"], False)
        self.assertIs(result["physical_outputs_energized"], True)
        online_events = [event for event in fixture.events if event[0] == "online"]
        self.assertEqual(
            [event[1] for event in online_events],
            [
                "create_online_application",
                "login",
                "get_forced_expressions",
                "read_values",
                "logout",
                "Dispose",
            ],
        )
        self.assertEqual(online_events[1][2:], (ChangeOptions.Keep, False))

    def test_reconcile_marker_mismatch_or_absence_remains_unknown(self) -> None:
        fixture = Fixture()
        target = self.load_adapter(fixture)
        fixture.marker_values = [OTHER_DIGEST]
        mismatch = target.reconcile(
            CANDIDATE_RELEASE_DIGEST, ROLLBACK_RELEASE_DIGEST
        )
        self.assertEqual(mismatch["runtime_release_digest"], OTHER_DIGEST)
        self.assertNotIn(
            mismatch["runtime_release_digest"],
            (CANDIDATE_RELEASE_DIGEST, ROLLBACK_RELEASE_DIGEST),
        )
        self.assertEqual(mismatch["application_state"], "unknown")
        self.assertIs(mismatch["forced_values_present"], True)
        self.assertIs(mismatch["physical_outputs_energized"], True)

        fixture = Fixture()
        target = self.load_adapter(fixture)
        fixture.marker_values = []
        unknown = target.reconcile(
            CANDIDATE_RELEASE_DIGEST, ROLLBACK_RELEASE_DIGEST
        )
        self.assertIsNone(unknown["runtime_release_digest"])
        self.assertEqual(unknown["application_state"], "unknown")
        self.assertIs(unknown["forced_values_present"], True)
        self.assertIs(unknown["physical_outputs_energized"], True)

    def test_source_passes_release_static_gate_but_digest_is_deliberately_unapproved(self) -> None:
        ax_deployment_supervisor._validate_target_adapter_source(ADAPTER_PATH)
        self.assertNotIn(
            self.adapter_digest,
            ax_deployment_supervisor.APPROVED_TARGET_ADAPTER_SHA256,
        )
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        self.assertIn("must not be release-approved", source)
        self.assertIn("_unverified_physical_outputs()", source)
        self.assertIn("AXMCP_RuntimeMarker.ReleaseDigest", source)
        self.assertNotIn("AXMCP_RuntimeMarker.ArtifactDigest", source)
        self.assertNotIn("AXMCP_RuntimeMarker.BuildDigest", source)
        tree = ast.parse(source)
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertTrue(
            called_attributes.isdisjoint(
                {
                    "perform_network_scan",
                    "get_cached_network_scan_result",
                    "find_address_by_ip",
                    "start",
                    "stop",
                    "reset_origin",
                    "set_prepared_value",
                    "write_prepared_values",
                    "force_prepared_values",
                    "download_file",
                    "source_download",
                }
            )
        )
        self.assertNotIn('context.get("target")', source)


if __name__ == "__main__":
    unittest.main()
