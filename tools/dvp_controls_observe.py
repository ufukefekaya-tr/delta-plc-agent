"""Universal DVP MCP profile inspection and separately enrolled read observation.

This client has no command, program transfer, RUN/STOP or force mode. It reserves
evidence before starting MCP. A project profile supplies all signals and units.
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
import time

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


async def run(args, report, persist):
    if args.observe and not args.workspace_id:
        raise ValueError('Observe requires --workspace-id from the current live enrollment')
    path = Path(args.config).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    model = config.get('target_model')
    if not isinstance(model, str) or not model:
        raise ValueError('Explicit target_model required; no default PLC')
    target = dict(vendor='delta', family='DVP', model=model, toolchain='dvpserial')
    source = Path(config["machine_source_path"]).resolve()
    catalog = Path(config["machine_catalog_path"]).resolve()
    if sha(source) != config["machine_source_sha256"] or sha(catalog) != config["machine_catalog_sha256"]:
        raise ValueError("Selected source/catalog SHA changed")
    contract = json.loads(catalog.read_text(encoding="utf-8"))
    config_sha = sha(path)
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update({
           "UNIVERSAL_PLC_DVPSERIAL_CONFIG": str(path), "PYTHONDONTWRITEBYTECODE": "1"}
    )
    server = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "universal_plc", "--root", str(Path(args.runtime_dir).resolve()),
              "--allow-read", str(source.parent), "serve"], cwd=ROOT, env=env)
    report.update(source_sha256=sha(source), catalog_sha256=sha(catalog),
                  config_sha256=config_sha, mode="observe" if args.observe else "profile_only",
                  records=[], physical_commands=False, program_transfer=False)
    async with Client(server, read_timeout_seconds=600) as client:
        async def call(name, arguments):
            reply = await client.call_tool(name, arguments)
            if reply.is_error or not isinstance(reply.structured_content, dict):
                raise RuntimeError(f"MCP {name} failed: {reply.content}")
            return reply.structured_content

        if args.workspace_id:
            ws = await call("workspace.status", dict(workspace_id=args.workspace_id))
            if (ws.get('workspace_id') != args.workspace_id
                    or ws.get('adapter_id') != 'delta.dvpserial'
                    or any(ws.get('target', {}).get(k) != v for k, v in target.items())
                    or ws.get('revision') != contract['revision']
                    or ws.get('baseline_sha256') != sha(source)
                    or not ws.get('context_digest')):
                raise ValueError('Existing workspace target/source/revision/context mismatch; no port opened')
        else:
            ws = await call("workspace.create", dict(target=target, task_id="dvp-profile-observe",
                revision=contract["revision"], baseline_path=str(source), expected_sha256=sha(source)))
        report["workspace"] = ws

        async def step(operation, workflow):
            if sha(path) != config_sha or sha(source) != report["source_sha256"] or sha(catalog) != report["catalog_sha256"]:
                raise RuntimeError("Current profile/source/catalog changed; no next step issued")
            started = await call("workflow.start", dict(workspace_id=ws["workspace_id"], workflow=workflow))
            report["current_operation"] = operation
            persist()
            result = await call("workflow.run", dict(workspace_id=ws["workspace_id"],
                run_id=started["run_id"], steps=[dict(operation=operation, arguments={})]))
            report["records"].append(result)
            persist()
            completed = result.get("completed", [])
            if len(completed) != 1 or completed[0].get("result", {}).get("ok") is not True:
                raise RuntimeError(f"{operation} incomplete; inspect evidence, no automatic retry")
            return completed[0]["result"]

        try:
            report["profile"] = await step("machine.profile", "W1")
            report["outcome"] = "PASS_PROFILE_ONLY_NO_HARDWARE"
            if args.observe:
                report["observations"] = []
                began = time.monotonic()
                while True:
                    report["observations"].append(await step("machine.observe", "W5"))
                    persist()
                    if report['observations'][-1].get('live_revision_verified') is not True:
                        raise RuntimeError('LIVE_REVISION_UNVERIFIED: raw sample retained; watch incomplete')
                    remaining = args.duration_seconds - (time.monotonic() - began)
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(args.interval_seconds, remaining))
                report["outcome"] = "OBSERVATIONS_RECORDED"
        finally:
            pending_error = sys.exc_info()[0] is not None
            try:
                report["disconnect"] = await call("adapter.invoke", dict(workspace_id=ws["workspace_id"],
                    operation="target.disconnect", arguments={}, expected_context_digest=ws["context_digest"]))
                if report["disconnect"].get("result", {}).get("ok") is not True:
                    raise RuntimeError("Disconnect was not confirmed")
            except Exception as exc:
                report["disconnect_error"] = str(exc)
                if not pending_error:
                    raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--workspace-id", help="Existing workspace in this runtime; required for verified observation")
    parser.add_argument("--evidence-path", required=True)
    parser.add_argument("--observe", action="store_true", help="Requires a current released port lease; default only inspects profile")
    parser.add_argument("--duration-seconds", type=int, default=90)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 300 or not .5 <= args.interval_seconds <= 5:
        parser.error("Duration must be 1..300 seconds; interval .5..5 seconds")
    report = dict(outcome="NOT_RUN", started_at=datetime.now(timezone.utc).isoformat())
    path = Path(args.evidence_path).resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("x+", encoding="utf-8", newline="\n")
    except OSError as exc:
        print(json.dumps(dict(outcome="NOT_RUN", code="EVIDENCE_OUTPUT_UNAVAILABLE", error=str(exc))))
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
        persist()
        try:
            asyncio.run(run(args, report, persist))
        except Exception as exc:
            report.update(outcome="INCOMPLETE", error=str(exc), retry_allowed=False)
            exit_code = 1
        finally:
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            persist()
    print(json.dumps(dict(outcome=report["outcome"], evidence_path=str(path), error=report.get("error")), ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
