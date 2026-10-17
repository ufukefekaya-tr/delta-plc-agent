# -*- coding: utf-8 -*-
"""One owned offline creation request, executed only inside vendor ScriptEngine."""
from __future__ import print_function
import codecs
import hashlib
import imp
import json
import os


def run(projects_api, pou_type, languages, task_kind):
    request_path = os.environ["AXMCP_CREATE_REQUEST"]
    result_path = os.environ["AXMCP_CREATE_RESULT"]
    with open(request_path, "rb") as handle:
        raw = handle.read()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if digest != os.environ["AXMCP_CREATE_REQUEST_SHA256"]:
        raise ValueError("project factory request digest mismatch")
    request = json.loads(raw.decode("utf-8"))
    factory_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project_factory.py")
    with open(factory_path, "rb") as handle:
        if "sha256:" + hashlib.sha256(handle.read()).hexdigest() != request["factory_sha256"]:
            raise ValueError("project factory source changed")
    try:
        factory = imp.load_source("axmcp_native_project_factory", factory_path)
        result = factory.create_project(projects_api, pou_type, languages, task_kind, request["create"])
        result["job_id"] = request["job_id"]
        result["request_digest"] = digest
    except Exception as exc:
        try:
            message = unicode(exc)
        except NameError:
            message = str(exc)
        result = {"operation": "project.create", "job_id": request["job_id"], "request_digest": digest, "status": "failed", "error": message, "live_verified": False, "compile_verified": False}
    if os.path.exists(result_path):
        raise ValueError("result already exists")
    with codecs.open(result_path, "w", "utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        run(projects, PouType, ImplementationLanguages, KindOfTask)
    finally:
        system.exit()
