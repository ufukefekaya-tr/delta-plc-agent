# -*- coding: utf-8 -*-
"""Human-launched, visible-IDE AXQ-FILE/1 read-only probe.

This file is intentionally compatible with a conservative Python 2.7-style
ScriptEngine surface. It never opens a project, builds, saves, imports, logs in,
or touches online/PLC APIs. The already-open primary project must be a scratch
copy selected by the authorized human.
"""
from __future__ import print_function

import hashlib
import json
import os
import re
import sys
import time
import traceback
import calendar

PROTOCOL = "AXQ-FILE/1"
ALLOWED = (
    "environment.probe",
    "project.tree.read",
    "project.inventory.read",
    "source.read",
    "project.export.native",
)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REQUEST_KEYS = set(
    (
        "protocol",
        "message_type",
        "correlation_id",
        "operation",
        "created_at",
        "deadline_at",
        "environment_digest",
        "artifacts",
    )
)
CONFIG_KEYS = set(("config_version", "environment_digest", "source_target", "export_path"))


class ProbeError(Exception):
    pass


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _load_json(path):
    handle = open(path, "rb")
    try:
        raw = handle.read()
    finally:
        handle.close()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProbeError("UTF-8 BOM is forbidden")
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except Exception as exc:
        raise ProbeError("invalid JSON: " + str(exc))
    if not isinstance(value, dict):
        raise ProbeError("top-level JSON must be an object")
    return value, raw


def _json_bytes(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if not isinstance(text, bytes):
        text = text.encode("utf-8")
    return text


def _digest_bytes(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _digest_file(path):
    digest = hashlib.sha256()
    handle = open(path, "rb")
    try:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        handle.close()
    return "sha256:" + digest.hexdigest()


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_relative_path(value):
    if not isinstance(value, str) or not value:
        raise ProbeError("artifact path must be non-empty text")
    if value.startswith("/") or value.startswith("\\") or "\\" in value or ":" in value:
        raise ProbeError("absolute, drive, UNC and backslash paths are forbidden")
    parts = value.split("/")
    for part in parts:
        if part in ("", ".", "..") or not PATH_SEGMENT_RE.match(part):
            raise ProbeError("invalid artifact path segment")
    return parts


def _contained_path(root, relative):
    parts = _validate_relative_path(relative)
    root_abs = os.path.abspath(root)
    target = os.path.abspath(os.path.join(root_abs, *parts))
    prefix = root_abs.rstrip(os.sep) + os.sep
    if target != root_abs and not target.startswith(prefix):
        raise ProbeError("artifact path escapes scratch root")
    return target


def _atomic_publish(path, raw):
    if os.path.exists(path):
        raise ProbeError("final output already exists")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    partial = path + ".partial"
    if os.path.exists(partial):
        raise ProbeError("partial output already exists")
    handle = open(partial, "wb")
    try:
        handle.write(raw)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except Exception:
            pass
    finally:
        handle.close()
    if os.path.exists(path):
        os.remove(partial)
        raise ProbeError("final output appeared during publish")
    os.rename(partial, path)


def _safe_name(obj):
    try:
        return str(obj.get_name())
    except Exception:
        return str(getattr(obj, "name", "<unnamed>"))


def _primary_project():
    project = projects.primary
    if project is None:
        raise ProbeError("no already-open primary scratch project")
    return project


def _walk_tree(obj, depth, max_depth):
    entry = {
        "name": _safe_name(obj),
        "type": str(getattr(obj, "type", "")),
        "guid": str(getattr(obj, "guid", "")),
        "is_folder": bool(getattr(obj, "is_folder", False)),
    }
    if depth < max_depth:
        try:
            children = list(obj.get_children(False))
        except Exception:
            children = []
        if children:
            entry["children"] = [_walk_tree(child, depth + 1, max_depth) for child in children]
    return entry


def _find_source(project, target):
    matches = list(project.find(target, True))
    textual = [
        item
        for item in matches
        if hasattr(item, "textual_declaration") or hasattr(item, "textual_implementation")
    ]
    if len(textual) != 1:
        raise ProbeError("source target must resolve to exactly one textual object")
    return textual[0]


def _text(value):
    if value is None:
        return ""
    if hasattr(value, "text"):
        return str(value.text)
    if hasattr(value, "get_text"):
        return str(value.get_text())
    return ""


def _environment_payload():
    executable = str(getattr(system, "executable_filename", "unknown"))
    product_version = str(getattr(system, "version", "unknown"))
    project = _primary_project()
    return {
        "executable": executable,
        "product_version": product_version,
        "script_engine": sys.version,
        "project_path": str(getattr(project, "path", "")),
        "project_dirty": bool(getattr(project, "dirty", False)),
        "real_target_contacted": False,
    }


def _tree_payload():
    project = _primary_project()
    return {
        "tree": [_walk_tree(child, 1, 5) for child in project.get_children(False)],
        "real_target_contacted": False,
    }


def _inventory_payload():
    project = _primary_project()
    devices = []
    libraries = []
    tasks = []
    stack = list(project.get_children(False))
    while stack:
        item = stack.pop(0)
        name = _safe_name(item)
        type_text = str(getattr(item, "type", "")).lower()
        class_text = str(type(item)).lower()
        summary = {"name": name, "type": str(getattr(item, "type", ""))}
        if "device" in class_text or "device" in type_text:
            devices.append(summary)
        if "library" in class_text or "library" in type_text:
            libraries.append(summary)
        if "task" in class_text or "task" in type_text:
            tasks.append(summary)
        try:
            stack.extend(list(item.get_children(False)))
        except Exception:
            pass
    return {
        "devices": devices,
        "libraries": libraries,
        "tasks": tasks,
        "real_target_contacted": False,
    }


def _source_payload(config):
    item = _find_source(_primary_project(), config["source_target"])
    return {
        "target": config["source_target"],
        "name": _safe_name(item),
        "declaration": _text(getattr(item, "textual_declaration", None)),
        "implementation": _text(getattr(item, "textual_implementation", None)),
        "real_target_contacted": False,
    }


def _native_export(root, config):
    project = _primary_project()
    export_path = _contained_path(root, config["export_path"])
    if os.path.exists(export_path):
        raise ProbeError("native export destination already exists")
    parent = os.path.dirname(export_path)
    if not os.path.isdir(parent):
        os.makedirs(parent)
    method = getattr(project, "export_native", None)
    if method is None:
        raise ProbeError("exact AX project has no export_native method")
    objects = list(project.get_children(False))
    errors = []
    attempts = (
        lambda: method(objects, export_path, True),
        lambda: method(objects, export_path),
        lambda: method(export_path, objects),
        lambda: method(export_path),
    )
    completed = False
    for attempt in attempts:
        try:
            attempt()
            completed = True
            break
        except TypeError as exc:
            errors.append(str(exc))
    if not completed:
        raise ProbeError("no compatible export_native signature: " + " | ".join(errors))
    if not os.path.isfile(export_path):
        raise ProbeError("export_native returned without an output file")
    return export_path


def _validate_config(value):
    if set(value) != CONFIG_KEYS or value["config_version"] != "1":
        raise ProbeError("probe config fields or version are invalid")
    if not DIGEST_RE.match(value["environment_digest"]):
        raise ProbeError("probe config environment digest is invalid")
    if value["environment_digest"] == "sha256:" + ("0" * 64):
        raise ProbeError("probe config environment digest is still the placeholder")
    if not isinstance(value["source_target"], str) or not value["source_target"]:
        raise ProbeError("source_target must be non-empty text")
    _validate_relative_path(value["export_path"])


def _validate_request(value, root, config):
    if set(value) != REQUEST_KEYS:
        raise ProbeError("request fields do not match AXQ-FILE/1")
    if value["protocol"] != PROTOCOL or value["message_type"] != "request":
        raise ProbeError("unsupported request protocol")
    if value["operation"] not in ALLOWED:
        raise ProbeError("operation is not allowlisted")
    if not UUID_RE.match(value["correlation_id"]):
        raise ProbeError("invalid correlation id")
    if not DIGEST_RE.match(value["environment_digest"]):
        raise ProbeError("invalid environment digest")
    if value["environment_digest"] != config["environment_digest"]:
        raise ProbeError("request/config environment digest mismatch")
    try:
        created = calendar.timegm(time.strptime(value["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
        deadline = calendar.timegm(time.strptime(value["deadline_at"], "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        raise ProbeError("invalid request timestamp")
    if deadline <= created or time.time() > deadline:
        raise ProbeError("request deadline is invalid or stale")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ProbeError("request must contain exactly one config artifact")
    artifact = artifacts[0]
    if not isinstance(artifact, dict) or set(artifact) != set(("path", "sha256")):
        raise ProbeError("invalid request artifact descriptor")
    if artifact["path"] != "probe-config.json" or not DIGEST_RE.match(artifact["sha256"]):
        raise ProbeError("request config artifact is invalid")
    config_path = _contained_path(root, artifact["path"])
    if _digest_file(config_path) != artifact["sha256"]:
        raise ProbeError("request config artifact digest mismatch")


def _execute(root, request, config):
    operation = request["operation"]
    correlation = request["correlation_id"]
    if operation == "project.export.native":
        artifact_path = _native_export(root, config)
        relative = config["export_path"]
    else:
        if operation == "environment.probe":
            payload = _environment_payload()
        elif operation == "project.tree.read":
            payload = _tree_payload()
        elif operation == "project.inventory.read":
            payload = _inventory_payload()
        elif operation == "source.read":
            payload = _source_payload(config)
        else:
            raise ProbeError("unreachable operation")
        relative = "outputs/" + correlation + ".payload.json"
        artifact_path = _contained_path(root, relative)
        _atomic_publish(artifact_path, _json_bytes(payload))
    return {"path": relative, "sha256": _digest_file(artifact_path)}


def run_once(root):
    config, _ = _load_json(os.path.join(root, "probe-config.json"))
    _validate_config(config)
    request_dir = os.path.join(root, "requests")
    names = [name for name in os.listdir(request_dir) if name.endswith(".request.json")]
    if not names:
        raise ProbeError("no final request found")
    names.sort()
    request_path = os.path.join(request_dir, names[0])
    request, request_raw = _load_json(request_path)
    _validate_request(request, root, config)
    correlation = request["correlation_id"]
    if os.path.basename(request_path) != correlation + ".request.json":
        raise ProbeError("request filename/correlation mismatch")
    result_path = os.path.join(root, "results", correlation + ".result.json")
    evidence_path = os.path.join(root, "evidence", correlation + ".evidence.json")
    status = "unverified"
    diagnostics = [
        "Visible scratch probe completed; human isolation and before/after evidence remain required.",
        "real_target_contacted=false was asserted by the probe path; human confirmation remains required.",
    ]
    artifacts = []
    try:
        artifacts = [_execute(root, request, config)]
    except Exception as exc:
        status = "blocked"
        diagnostics = ["Probe operation blocked: " + str(exc)]
    result = {
        "protocol": PROTOCOL,
        "message_type": "result",
        "correlation_id": correlation,
        "operation": request["operation"],
        "created_at": _utc_now(),
        "status": status,
        "environment_digest": request["environment_digest"],
        "request_digest": _digest_bytes(request_raw),
        "artifacts": artifacts,
        "diagnostics": diagnostics,
    }
    result_raw = _json_bytes(result)
    _atomic_publish(result_path, result_raw)
    evidence = {
        "schema_version": "1",
        "record_type": "EvidenceRecord",
        "created_at": result["created_at"],
        "correlation_id": correlation,
        "producer": "agent",
        "evidence_class": "CLAIM",
        "input_digest": result["request_digest"],
        "output_digest": _digest_bytes(result_raw),
        "environment_digest": request["environment_digest"],
        "diagnostics": [
            "Vendor-side artifact exists but has not been promoted by independent human evidence review."
        ],
    }
    _atomic_publish(evidence_path, _json_bytes(evidence))
    return status


def main():
    root = os.environ.get("AXMCP_SCRATCH_ROOT")
    if not root:
        print("AXMCP_SCRATCH_ROOT is required", file=sys.stderr)
        return 2
    try:
        status = run_once(os.path.abspath(root))
        return 0 if status == "unverified" else 3
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
