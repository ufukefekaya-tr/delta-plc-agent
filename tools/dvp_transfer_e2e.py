"""Real MCP client: observation or separately enrolled SS2 transfer.

No UI, background RUN, serial factory replacement, or automatic transfer retry.
The report records returned backend evidence; it never promotes a fixture to PLC PASS.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
TARGET = {"vendor": "delta", "family": "DVP", "model": "DVP14SS211R", "toolchain": "dvpserial"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approval(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("approved") is not True:
        raise ValueError("Current source is not approved")
    for key in ("revision", "source_path", "source_sha256", "payload_sha256",
                "approval_evidence", "vendor_compile_or_save_evidence", "current_plc_program_evidence"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("Current approval is missing " + key)
    source = Path(value["source_path"]).resolve()
    if not source.is_file() or sha(source) != value["source_sha256"]:
        raise ValueError("Current approved source SHA changed")
    value["source_path"] = str(source)
    return value


async def run(args, report, persist=lambda: None):
    config_path = Path(args.config).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    approved_path = Path(args.approved_manifest).resolve() if args.approved_manifest else None
    current = approval(approved_path) if approved_path else None
    if args.mode in ("deploy", "qualify"):
        if current is None:
            raise ValueError("deploy requires the current approved manifest")
        if args.mode == "deploy" and current.get("program_download_authorized") is False:
            raise ValueError("Current source owner explicitly disallows program download")
        if str(Path(config.get("approved_manifest_path", "")).resolve()) != str(approved_path):
            raise ValueError("Session approval path differs from latest source manifest")
        if config.get("approved_manifest_sha256") != sha(approved_path):
            raise ValueError("Session source approval is stale")
        flags = ("program_transfer_enabled", "allow_program_transfer", "heaters_isolated", "user_present")
        if args.mode == "deploy":
            flags += ("verified_contract",)
        for flag in flags:
            if config.get(flag) is not True:
                raise ValueError("Current transfer session is not enrolled: " + flag)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["UNIVERSAL_PLC_DVPSERIAL_CONFIG"] = str(config_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = ["-B", "-m", "universal_plc", "--root", str(Path(args.runtime_dir).resolve())]
    if current:
        cmd += ["--allow-read", str(Path(current["source_path"]).parent)]
    cmd += ["serve"]
    params = StdioServerParameters(command=sys.executable, args=cmd, cwd=ROOT, env=env)
    report.update(mode=args.mode, started_at=datetime.now(timezone.utc).isoformat(),
                  config_sha256=sha(config_path), physical_fixture=False, records=[])
    if current:
        report["source"] = current
        report["approved_manifest_sha256"] = sha(approved_path)
    async with Client(params, read_timeout_seconds=600) as client:
        async def call(name, arguments):
            response = await client.call_tool(name, arguments)
            if response.is_error or not isinstance(response.structured_content, dict):
                raise RuntimeError(f"MCP {name} failed: {response.content}")
            return response.structured_content

        tools = await client.list_tools()
        report["tools"] = [tool.name for tool in tools.tools]
        if args.workspace_id:
            ws = await call("workspace.status", {"workspace_id": args.workspace_id})
            if current and (ws["revision"] != current["revision"] or ws["baseline_sha256"] != current["source_sha256"]):
                raise ValueError("Existing workspace does not match the current approved source")
        else:
            request = dict(target=TARGET, task_id="dvp-transfer-e2e", revision=current["revision"] if current else "online-read")
            if current:
                request.update(baseline_path=current["source_path"], expected_sha256=current["source_sha256"])
            ws = await call("workspace.create", request)
        report["workspace"] = ws

        async def step(operation, arguments=None, workflow="W5"):
            if args.mode in ("deploy", "qualify"):
                # Re-check the original producer manifest, not only the workspace copy.
                if sha(approved_path) != report["approved_manifest_sha256"] or approval(approved_path) != current:
                    raise ValueError("Approved program changed during MCP workflow")
            wf = await call("workflow.start", {"workspace_id": ws["workspace_id"], "workflow": workflow})
            if operation == "deployment.execute":
                report["execute_admission_attempted"] = True
                report["program_downloaded"] = None
            report["current_operation"] = operation
            persist()
            result = await call("workflow.run", {"workspace_id": ws["workspace_id"], "run_id": wf["run_id"],
                               "steps": [{"operation": operation, "arguments": arguments or {}}]})
            report["records"].append(result)
            completed = result.get("completed", [])
            if operation == "deployment.execute" and len(completed) == 1:
                backend = completed[0].get("result", {})
                report["deployment"] = backend
                if backend.get("ok") is True and backend.get("readback_verified") is True:
                    report["program_downloaded"] = True
                elif backend.get("plc_program_changed") is False:
                    report["program_downloaded"] = False
            if len(completed) != 1 or completed[0].get("result", {}).get("ok") is not True:
                raise RuntimeError(f"{operation} did not succeed; inspect the recorded evidence")
            return completed[0]["result"]

        try:
            if args.mode == "reconcile":
                if not args.workspace_id or not args.journal_path:
                    raise ValueError("reconcile requires the original workspace-id and journal-path")
                result = await step("deployment.reconcile", {"journal_path": args.journal_path}, "W6")
                report["reconciliation"] = result
                report["outcome"] = "RECONCILIATION_RECORDED"
            else:
                report["identity"] = await step("target.identify", workflow="W1")
                report["observations"] = [await step("target.status"), await step("target.status")]
                report["outcome"] = "PASS_LIVE_READ"
                if args.mode == "qualify":
                    report["qualification"] = await step("program.contract.qualify", {"output_dir": "qualification"})
                    proof = report["qualification"]
                    files = Path(args.runtime_dir).resolve() / "workspaces" / ws["workspace_id"] / "files"
                    contract = (files / proof["contract_path"]).resolve()
                    if not contract.is_relative_to(files) or sha(contract) != proof["contract_sha256"]:
                        raise RuntimeError("Returned contract evidence does not match the workspace artifact")
                    if sha(config_path) != report["config_sha256"]:
                        raise RuntimeError("Host session changed; qualification was recorded but not enrolled")
                    enrolled = {**config, "verified_contract": True, "contract_path": str(contract),
                                "contract_sha256": proof["contract_sha256"]}
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                         dir=config_path.parent, delete=False) as stream:
                            temporary = Path(stream.name)
                            json.dump(enrolled, stream, ensure_ascii=False, indent=2)
                            stream.write("\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temporary, config_path)
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
                    report["enrolled_config_sha256"] = sha(config_path)
                    report["outcome"] = "PASS_ACK_SHAPE_OBSERVED"
                if args.mode == "deploy":
                    if any(obs["running"] for obs in report["observations"]):
                        raise RuntimeError("PLC is RUN; this client never issues an automatic STOP")
                    backup = await step("project.upload", {"output_dir": "backup"})
                    report["backup"] = backup
                    prepared = await step("deployment.prepare", {
                        "input_path": ws["baseline_file"], "expected_sha256": current["source_sha256"],
                        "backup_path": backup.get("manifest_path", backup.get("backup_path")),
                        "expected_backup_sha256": backup.get("manifest_sha256", backup.get("backup_sha256"))})
                    report["prepared"] = prepared
                    report["deployment"] = await step("deployment.execute", {
                        "plan_path": prepared["plan_path"], "expected_sha256": prepared["plan_sha256"]})
                    report["final_observation"] = await step("target.status")
                    if report["final_observation"]["running"]:
                        raise RuntimeError("Final STOP was not observed")
                    report["outcome"] = "PASS_LIVE_TRANSFER" if report["deployment"].get("readback_verified") is True else "TRANSFER_NOT_PROVED"
        finally:
            pending_error = sys.exc_info()[0] is not None
            try:
                response = await call("adapter.invoke", dict(workspace_id=ws["workspace_id"],
                                      operation="target.disconnect", arguments={}, expected_context_digest=ws["context_digest"]))
                report["disconnect"] = response
                if response.get("result", {}).get("ok") is not True:
                    raise RuntimeError("Disconnect was not confirmed")
            except Exception as exc:
                report["disconnect_error"] = str(exc)
                if not pending_error:
                    raise
    report["finished_at"] = datetime.now(timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--evidence-path", required=True)
    parser.add_argument("--approved-manifest")
    parser.add_argument("--mode", choices=("observe", "qualify", "deploy", "reconcile"), default="observe")
    parser.add_argument("--workspace-id")
    parser.add_argument("--journal-path")
    args = parser.parse_args()
    report = {"outcome": "NOT_RUN", "program_downloaded": False}
    path = Path(args.evidence_path).resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("x+", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(json.dumps({"outcome": "NOT_RUN", "code": "EVIDENCE_OUTPUT_UNAVAILABLE", "error": str(exc)}))
        return 1
    exit_code = 0
    with stream:
        def persist():
            stream.seek(0)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        persist()  # Reserve and prove writable evidence before starting any MCP/port action.
        try:
            asyncio.run(run(args, report, persist))
        except Exception as exc:
            report.update(outcome="INCOMPLETE", error=str(exc), retry_allowed=False)
            exit_code = 1
        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            persist()
    print(json.dumps({"outcome": report["outcome"], "evidence_path": str(path), "error": report.get("error")}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
