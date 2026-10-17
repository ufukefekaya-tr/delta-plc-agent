# -*- coding: utf-8 -*-
"""Read-only AX-308/SP18 target identity and runtime-marker adapter.

The adapter is repository-owned executable code, but its digest is deliberately
not release-approved.  It uses the CODESYS/DIADesigner ScriptEngine project,
device, communication-settings, and online-application surfaces directly.  It
never discovers a route, scans a network, or changes controller state.

The installed ScriptEngine surface has no authoritative read-only physical-
output isolation observer.  The boolean-only target contract therefore reports
that state as unsafe.  This adapter must not be release-approved until such an
observer is implemented and independently qualified.
"""
from __future__ import print_function

import hashlib
import json
import re
import time


ADAPTER_KIND = "DIADESIGNER_AX308_SP18_TARGET/1"

MODEL = "AX-308"
DEVICE_TYPE = 4102
DEVICE_ID = "16f7 0319"
DEVICE_DESCRIPTION_VERSION = "1.0.9.2"

RUNTIME_RELEASE_MARKER_EXPRESSION = "AXMCP_RuntimeMarker.ReleaseDigest"
RUNTIME_MARKER_EXPRESSIONS = (RUNTIME_RELEASE_MARKER_EXPRESSION,)

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

try:  # DIADesigner-AX 1.9 ScriptEngine is Python 2 compatible.
    TEXT_TYPES = (basestring,)
except NameError:  # pragma: no cover - exercised by the CPython test host
    TEXT_TYPES = (str,)


class AdapterError(Exception):
    """A required identity or read-only runtime observation is uncertain."""


def _canonical_bytes(value):
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    text += "\n"
    if not isinstance(text, bytes):
        text = text.encode("utf-8")
    return text


def _digest(value):
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _required_text(value, label):
    if not isinstance(value, TEXT_TYPES):
        raise AdapterError(label + " is unavailable")
    rendered = value.strip()
    if not rendered:
        raise AdapterError(label + " is unavailable")
    return rendered


def _required_digest(value, label):
    value = _required_text(value, label)
    if not DIGEST_RE.match(value):
        raise AdapterError(label + " is not a canonical sha256 digest")
    return value


def _normalize_device_id(value):
    return " ".join(_required_text(value, "device id").lower().split())


def _normalize_guid(value, label):
    rendered = _required_text(str(value), label).strip("{}").lower()
    if rendered == "00000000-0000-0000-0000-000000000000":
        raise AdapterError(label + " is empty")
    if not re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        rendered,
    ):
        raise AdapterError(label + " is not a GUID")
    return rendered


def _enum_name(value, label):
    rendered = _required_text(str(value), label).rsplit(".", 1)[-1].lower()
    if not re.match(r"^[a-z][a-z0-9_]*$", rendered):
        raise AdapterError(label + " is invalid")
    return rendered


def _object_name(value, label):
    getter = getattr(value, "get_name", None)
    if not callable(getter):
        raise AdapterError(label + " does not expose get_name")
    return _required_text(getter(), label + " name")


def _state_name(value):
    rendered = str(value).strip().lower()
    if rendered == "run" or rendered.endswith(".run"):
        return "run"
    if rendered == "stop" or rendered.endswith(".stop"):
        return "stop"
    raise AdapterError("online application state is neither RUN nor STOP")


def _marker_digest(value, label):
    rendered = _required_text(str(value), label)
    if len(rendered) >= 2 and rendered[0] == rendered[-1] and rendered[0] in ("'", '"'):
        rendered = rendered[1:-1].strip()
    return _required_digest(rendered, label)


def _unverified_physical_outputs():
    """Encode unavailable output-isolation evidence fail-closed."""

    return True


def _route_digest(device, settings):
    gateway_guid = _normalize_guid(getattr(settings, "gateway_guid", None), "gateway GUID")
    device_address = _required_text(
        getattr(settings, "device_address", None), "device address"
    )
    device_name = getattr(settings, "device_name", "")
    if device_name is None:
        device_name = ""
    if not isinstance(device_name, TEXT_TYPES):
        raise AdapterError("configured device name is invalid")
    tracking_mode = _enum_name(getattr(settings, "TrackingMode", None), "tracking mode")

    get_gateway = getattr(device, "get_gateway", None)
    get_address = getattr(device, "get_address", None)
    if not callable(get_gateway) or not callable(get_address):
        raise AdapterError("device communication API is incomplete")
    if _normalize_guid(get_gateway(), "device gateway") != gateway_guid:
        raise AdapterError("device gateway and communication settings disagree")
    if _required_text(get_address(), "device route address") != device_address:
        raise AdapterError("device address and communication settings disagree")

    return _digest(
        {
            "schema": "DIADESIGNER_AX_ROUTE/1",
            "gateway_guid": gateway_guid,
            "device_address": device_address,
            "device_name": device_name.strip(),
            "tracking_mode": tracking_mode,
        }
    )


def _fingerprint_material(
    device_identity,
    runtime_version,
    gateway_route_digest,
    application_identity,
    isolation_evidence_digest,
):
    return {
        "schema": "DIADESIGNER_AX308_SP18_TARGET_FINGERPRINT/1",
        "model": MODEL,
        "device_identity": device_identity,
        "runtime_version": runtime_version,
        "device_description_version": DEVICE_DESCRIPTION_VERSION,
        "gateway_route_digest": gateway_route_digest,
        "application_identity": application_identity,
        "forced_values_present": False,
        "physical_outputs_energized": False,
        "isolation_evidence_digest": isolation_evidence_digest,
    }


def _compute_fingerprint(
    device_identity,
    runtime_version,
    gateway_route_digest,
    application_identity,
    isolation_evidence_digest,
):
    """Return the stable enrollment fingerprint; capture time is excluded."""

    return _digest(
        _fingerprint_material(
            device_identity,
            runtime_version,
            gateway_route_digest,
            application_identity,
            isolation_evidence_digest,
        )
    )


class Ax308Sp18Target(object):
    def __init__(self, context):
        if not isinstance(context, dict):
            raise AdapterError("adapter context must be a dictionary")
        self.projects = context.get("projects")
        self.online = context.get("online")
        self.change_options = context.get("OnlineChangeOption")
        self.application_states = context.get("ApplicationState")
        self.enrollment = context.get("enrollment")
        if self.projects is None or self.online is None:
            raise AdapterError("project and online ScriptEngine APIs are required")
        if self.change_options is None or not hasattr(self.change_options, "Keep"):
            raise AdapterError("OnlineChangeOption.Keep is required")
        if self.application_states is None:
            raise AdapterError("ApplicationState API is required")
        if not isinstance(self.enrollment, dict):
            raise AdapterError("target enrollment is required")
        self._validate_enrollment_shape()

    def _validate_enrollment_shape(self):
        required = set(
            (
                "schema_version",
                "record_type",
                "status",
                "captured_at",
                "model",
                "device_identity",
                "runtime_version",
                "firmware_version",
                "gateway_route_digest",
                "application_identity",
                "forced_values_present",
                "physical_outputs_energized",
                "isolation_evidence_digest",
                "fingerprint",
            )
        )
        if set(self.enrollment) != required:
            raise AdapterError("target enrollment shape mismatch")
        if (
            self.enrollment["schema_version"] != "1"
            or self.enrollment["record_type"] != "TargetEnrollment"
            or self.enrollment["status"] != "enrolled"
            or self.enrollment["model"] != MODEL
            or self.enrollment["forced_values_present"] is not False
            or self.enrollment["physical_outputs_energized"] is not False
        ):
            raise AdapterError("target enrollment is not an isolated AX-308")
        for field in (
            "device_identity",
            "runtime_version",
            "firmware_version",
            "application_identity",
        ):
            _required_text(self.enrollment[field], "enrollment " + field)
        for field in (
            "gateway_route_digest",
            "isolation_evidence_digest",
            "fingerprint",
        ):
            _required_digest(self.enrollment[field], "enrollment " + field)
        if self.enrollment["firmware_version"] != DEVICE_DESCRIPTION_VERSION:
            raise AdapterError("enrollment device-description version is not 1.0.9.2")
        expected = _compute_fingerprint(
            self.enrollment["device_identity"],
            self.enrollment["runtime_version"],
            self.enrollment["gateway_route_digest"],
            self.enrollment["application_identity"],
            self.enrollment["isolation_evidence_digest"],
        )
        if self.enrollment["fingerprint"] != expected:
            raise AdapterError("enrollment fingerprint does not match its identity fields")

    def current_application(self):
        primary = getattr(self.projects, "primary", None)
        if primary is None or getattr(primary, "primary", None) is not True:
            raise AdapterError("one primary project is required")
        active = getattr(primary, "active_application", None)
        children = getattr(primary, "get_children", None)
        if active is None or not callable(children):
            raise AdapterError("primary project active application is unavailable")
        applications = [
            item
            for item in list(children(True))
            if bool(getattr(item, "is_application", False))
            and bool(getattr(item, "is_active_application", False))
        ]
        if len(applications) != 1 or applications[0] != active:
            raise AdapterError("primary project must have one unique active application")
        if getattr(active, "project", None) != primary:
            raise AdapterError("active application does not belong to the primary project")
        return active

    def _require_current_application(self, application):
        if application is None or application != self.current_application():
            raise AdapterError("application is not the unique primary active application")

    def _owning_ax_device(self, application):
        matching = []
        current = getattr(application, "parent", None)
        visited = set()
        while current is not None and not bool(getattr(current, "is_root", False)):
            marker = id(current)
            if marker in visited:
                raise AdapterError("application parent graph contains a cycle")
            visited.add(marker)
            if bool(getattr(current, "is_device", False)):
                getter = getattr(current, "get_device_identification", None)
                if not callable(getter):
                    raise AdapterError("device identification API is unavailable")
                identity = getter()
                try:
                    device_type = int(getattr(identity, "type"))
                except Exception:
                    raise AdapterError("device type is unavailable")
                device_id = _normalize_device_id(getattr(identity, "id", None))
                version = _required_text(
                    getattr(identity, "version", None), "device-description version"
                )
                if (
                    device_type == DEVICE_TYPE
                    and device_id == DEVICE_ID
                    and version == DEVICE_DESCRIPTION_VERSION
                ):
                    matching.append((current, device_type, device_id, version))
            current = getattr(current, "parent", None)
        if len(matching) != 1:
            raise AdapterError(
                "application must have one AX-308 ancestor with 4102 / 16f7 0319 / 1.0.9.2"
            )
        return matching[0]

    def _identity(self, application):
        self._require_current_application(application)
        device, device_type, device_id, description_version = self._owning_ax_device(
            application
        )
        settings_getter = getattr(device, "get_device_communication_settings", None)
        if not callable(settings_getter):
            raise AdapterError("device communication settings API is unavailable")
        settings = settings_getter()
        if settings is None:
            raise AdapterError("device communication settings are unavailable")

        scanned_name = _required_text(
            getattr(settings, "scanned_target_name", None), "scanned target model"
        )
        if MODEL not in scanned_name.upper():
            raise AdapterError("connected target model is not AX-308")
        try:
            scanned_type = int(
                _required_text(
                    str(getattr(settings, "scanned_target_type", None)),
                    "scanned target type",
                )
            )
        except Exception:
            raise AdapterError("scanned target type is unavailable")
        scanned_id = _normalize_device_id(getattr(settings, "scanned_target_id", None))
        if scanned_type != DEVICE_TYPE or scanned_id != DEVICE_ID:
            raise AdapterError("connected target is not 4102 / 16f7 0319")

        runtime_version = _required_text(
            getattr(settings, "scanned_target_version", None), "runtime version"
        )
        device_identity = "%d/%s/%s" % (
            device_type,
            device_id,
            description_version,
        )
        route_digest = _route_digest(device, settings)

        names = []
        current = application
        while current is not None and current != device:
            names.append(_object_name(current, "application path object"))
            current = getattr(current, "parent", None)
        if current != device:
            raise AdapterError("application is not contained by the AX-308 device")
        names.append(_object_name(device, "AX-308 device"))
        names.reverse()
        application_guid = _normalize_guid(
            getattr(application, "guid", None), "application GUID"
        )
        application_identity = "/".join(names) + "|guid=" + application_guid
        isolation_digest = _required_digest(
            self.enrollment["isolation_evidence_digest"], "isolation evidence digest"
        )
        fingerprint = _compute_fingerprint(
            device_identity,
            runtime_version,
            route_digest,
            application_identity,
            isolation_digest,
        )
        return {
            "device_identity": device_identity,
            "runtime_version": runtime_version,
            "firmware_version": description_version,
            "gateway_route_digest": route_digest,
            "application_identity": application_identity,
            "isolation_evidence_digest": isolation_digest,
            "fingerprint": fingerprint,
        }

    def probe(self, application):
        identity = self._identity(application)
        for field in (
            "device_identity",
            "runtime_version",
            "firmware_version",
            "gateway_route_digest",
            "application_identity",
            "isolation_evidence_digest",
            "fingerprint",
        ):
            if identity[field] != self.enrollment[field]:
                raise AdapterError("live target identity mismatch: " + field)
        return {
            "schema_version": "1",
            "record_type": "TargetEnrollment",
            "status": "enrolled",
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": MODEL,
            "device_identity": identity["device_identity"],
            "runtime_version": identity["runtime_version"],
            "firmware_version": identity["firmware_version"],
            "gateway_route_digest": identity["gateway_route_digest"],
            "application_identity": identity["application_identity"],
            "forced_values_present": False,
            "physical_outputs_energized": _unverified_physical_outputs(),
            "isolation_evidence_digest": identity["isolation_evidence_digest"],
            "fingerprint": identity["fingerprint"],
        }

    def _read_runtime_release_digest(self, online_application):
        if online_application is None or not bool(
            getattr(online_application, "is_logged_in", False)
        ):
            raise AdapterError("online application is not logged in")
        forced_getter = getattr(online_application, "get_forced_expressions", None)
        reader = getattr(online_application, "read_values", None)
        if not callable(forced_getter) or not callable(reader):
            raise AdapterError("online read-only APIs are incomplete")
        if list(forced_getter()):
            raise AdapterError("forced values are present")
        _state_name(getattr(online_application, "application_state", None))
        values = list(reader(RUNTIME_MARKER_EXPRESSIONS))
        if len(values) != 1:
            raise AdapterError("runtime marker read is incomplete")
        return _marker_digest(values[0], "runtime release marker")

    def verify_running_artifact(self, online_application, runtime_release_digest):
        runtime_release_digest = _required_digest(
            runtime_release_digest, "expected runtime release digest"
        )
        observed = self._read_runtime_release_digest(online_application)
        if observed != runtime_release_digest:
            raise AdapterError("runtime release marker mismatch")
        return True

    def _unknown_reconciliation(
        self, target_fingerprint=None, runtime_release_digest=None
    ):
        return {
            "target_fingerprint": target_fingerprint,
            "runtime_release_digest": runtime_release_digest,
            "application_state": "unknown",
            "forced_values_present": True,
            "physical_outputs_energized": True,
        }

    def reconcile(
        self, candidate_runtime_release_digest, rollback_runtime_release_digest
    ):
        candidate_runtime_release_digest = _required_digest(
            candidate_runtime_release_digest, "candidate runtime release digest"
        )
        rollback_runtime_release_digest = _required_digest(
            rollback_runtime_release_digest, "rollback runtime release digest"
        )
        target_fingerprint = None
        online_application = None
        try:
            application = self.current_application()
            live = self.probe(application)
            target_fingerprint = live["fingerprint"]
            creator = getattr(self.online, "create_online_application", None)
            if not callable(creator):
                raise AdapterError("online application factory is unavailable")
            online_application = creator(application)
            if online_application is None:
                raise AdapterError("online application factory returned nothing")
            online_application.timeout = 30
            online_application.login(self.change_options.Keep, False)
            if not bool(getattr(online_application, "is_logged_in", False)):
                raise AdapterError("Keep login did not complete")
            state = _state_name(getattr(online_application, "application_state", None))
            runtime_release_digest = self._read_runtime_release_digest(
                online_application
            )
            if runtime_release_digest not in (
                candidate_runtime_release_digest,
                rollback_runtime_release_digest,
            ):
                return self._unknown_reconciliation(
                    target_fingerprint, runtime_release_digest
                )
            # ScriptEngine exposes the fixed marker, force list, and application
            # state read-only, but no authoritative physical-output-energy
            # observation.  The boolean-only reconciliation contract therefore
            # uses the unsafe value as its explicit fail-closed blocker.
            return {
                "target_fingerprint": target_fingerprint,
                "runtime_release_digest": runtime_release_digest,
                "application_state": state,
                "forced_values_present": False,
                "physical_outputs_energized": _unverified_physical_outputs(),
            }
        except Exception:
            return self._unknown_reconciliation(target_fingerprint)
        finally:
            if online_application is not None:
                try:
                    if bool(getattr(online_application, "is_logged_in", False)):
                        online_application.logout()
                except Exception:
                    pass
                try:
                    disposer = getattr(online_application, "Dispose", None)
                    if callable(disposer):
                        disposer()
                except Exception:
                    pass


def create_target_api(context):
    return Ax308Sp18Target(context)
