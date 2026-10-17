# -*- coding: utf-8 -*-
"""Offline project creation using the installed ScriptEngine API (Python 2/3).

No login, communication settings, download, force or controller start occurs.
Run inside ScriptEngine with its projects/PouType/ImplementationLanguages/
KindOfTask objects. Device.add must provide its native application skeleton.
"""
from __future__ import print_function

import hashlib
import os
import re

try:
    TEXT = (basestring,)
except NameError:
    TEXT = (str,)


class ProjectCreateError(ValueError):
    pass


def _identifier(value, label):
    if not isinstance(value, TEXT) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
        raise ProjectCreateError(label + " must be an IEC identifier")
    return value


def _one(objects, predicate, label):
    matches = [item for item in objects if predicate(item)]
    if len(matches) != 1:
        raise ProjectCreateError("expected exactly one " + label)
    return matches[0]


def _verify(project, config):
    device = _one(project.get_children(False), lambda item: item.get_name() == config["controller_name"], "controller")
    identity = device.get_device_identification()
    profile = config["device_profile"]
    if (int(identity.type), str(identity.id).lower(), str(identity.version)) != (profile["device_type"], profile["device_id"].lower(), profile["device_version"]):
        raise ProjectCreateError("saved controller identity differs from device profile")
    application = _one(device.get_children(True), lambda item: bool(getattr(item, "is_application", False)), "native application")
    return application


def create_project(projects_api, pou_type, languages, task_kind, config):
    """Create a native project, controller, called ST POU and cyclic task.

    The saved output is reopened and inspected. Compilation and deployment
    acceptance are separate operations. Existing output files are never replaced.
    """
    fields = {"output_path", "project_id", "device_profile", "controller_name", "pou_name", "task_name", "interval_ms", "declaration", "implementation"}
    if not isinstance(config, dict) or set(config) != fields:
        raise ProjectCreateError("project.create configuration fields are not exact")
    for key in ("project_id", "controller_name", "pou_name", "task_name"):
        _identifier(config[key], key)
    profile = config["device_profile"]
    if not isinstance(profile, dict) or set(profile) != {"profile_id", "model", "device_type", "device_id", "device_version"}:
        raise ProjectCreateError("explicit versioned device profile required")
    for key in ("profile_id", "model", "device_id", "device_version"):
        if not isinstance(profile[key], TEXT) or not profile[key].strip():
            raise ProjectCreateError("device profile field is empty: " + key)
    if type(profile["device_type"]) is not int or profile["device_type"] <= 0:
        raise ProjectCreateError("device_type must be a positive integer")
    if type(config["interval_ms"]) is not int or not 1 <= config["interval_ms"] <= 1000:
        raise ProjectCreateError("interval_ms outside bounded range")
    for key in ("declaration", "implementation"):
        if not isinstance(config[key], TEXT) or len(config[key]) > 262144 or "\x00" in config[key]:
            raise ProjectCreateError("invalid ST " + key)
    output = config["output_path"]
    if not isinstance(output, TEXT) or not os.path.isabs(output) or not output.lower().endswith(".project"):
        raise ProjectCreateError("absolute .project output path required")
    if os.path.exists(output) or not os.path.isdir(os.path.dirname(output)) or os.path.islink(os.path.dirname(output)):
        raise ProjectCreateError("output must be new in an existing non-symlink directory")
    if getattr(projects_api, "primary", None) is not None:
        raise ProjectCreateError("use a dedicated IDE process without an open primary project")
    project = None
    try:
        project = projects_api.create(output, primary=True)
        project.add(config["controller_name"], profile["device_type"], profile["device_id"], profile["device_version"])
        application = _verify(project, config)
        existing = application.get_children(True)
        if any(item.get_name() == config["pou_name"] for item in existing):
            raise ProjectCreateError("native device template already contains requested POU")
        pou = application.create_pou(config["pou_name"], pou_type.Program, languages.st)
        pou.textual_declaration.replace(config["declaration"])
        pou.textual_implementation.replace(config["implementation"])
        configs = [item for item in application.get_children(False) if bool(getattr(item, "is_task_configuration", False))]
        if len(configs) > 1:
            raise ProjectCreateError("multiple task configurations are unsupported")
        task_config = configs[0] if configs else application.create_task_configuration()
        original_tasks = list(task_config.get_children(False))
        original_task_names = sorted(item.get_name() for item in original_tasks)
        if config["task_name"] in original_task_names:
            raise ProjectCreateError("native template already contains requested task name; choose a unique task name")
        original_calls = {}
        for item in original_tasks:
            if bool(getattr(item, "is_task", False)):
                original_calls[item.get_name()] = [item.pous[index] for index in range(len(item.pous))]
        task = task_config.create_task(config["task_name"])
        task.kind_of_task = task_kind.Cyclic
        task.interval = str(config["interval_ms"])
        task.interval_unit = "ms"
        task.pous.add(config["pou_name"], "Explicit universal project entry point")
        project.save()
        project.close()
        project = None
        project = projects_api.open(output, primary=True)
        application = _verify(project, config)
        reopened_pou = _one(application.get_children(True), lambda item: item.get_name() == config["pou_name"] and getattr(item, "textual_declaration", None) is not None and getattr(item, "textual_implementation", None) is not None, "saved textual POU")
        if reopened_pou.textual_declaration.text != config["declaration"] or reopened_pou.textual_implementation.text != config["implementation"]:
            raise ProjectCreateError("saved POU source readback mismatch")
        saved_task = _one(application.get_children(True), lambda item: bool(getattr(item, "is_task", False)) and item.get_name() == config["task_name"], "saved task")
        if str(saved_task.interval) != str(config["interval_ms"]):
            raise ProjectCreateError("saved task interval mismatch")
        calls = [saved_task.pous[index][0] for index in range(len(saved_task.pous))]
        if calls != [config["pou_name"]] or saved_task.kind_of_task != task_kind.Cyclic or str(saved_task.interval_unit).lower() != "ms":
            raise ProjectCreateError("saved task call, kind or interval unit mismatch")
        reopened_config = _one(application.get_children(False), lambda item: bool(getattr(item, "is_task_configuration", False)), "saved task configuration")
        reopened_tasks = list(reopened_config.get_children(False))
        if sorted(item.get_name() for item in reopened_tasks) != sorted(original_task_names + [config["task_name"]]):
            raise ProjectCreateError("native task inventory changed unexpectedly")
        for item in reopened_tasks:
            if item.get_name() in original_calls:
                calls = [item.pous[index] for index in range(len(item.pous))]
                if calls != original_calls[item.get_name()]:
                    raise ProjectCreateError("native task POU bindings changed unexpectedly")
        project.close()
        project = None
        with open(output, "rb") as handle:
            digest = "sha256:" + hashlib.sha256(handle.read()).hexdigest()
        return {"operation": "project.create", "status": "created", "evidence_class": "IDE_READBACK", "project_sha256": digest, "output_path": output, "profile_id": profile["profile_id"], "model": profile["model"], "compile_verified": False, "live_verified": False}
    finally:
        if project is not None:
            # Only this new scratch project is owned by this process. Do not save
            # a partial failure and never close another user's IDE session.
            project.close()
