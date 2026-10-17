"""Run the existing Universal MCP over stdio, with durable W1/W5/W6/W7 evidence.

No PLC write or RUN/STOP command exists in this adapter. A profile must explicitly
authorize the read session and expire. Serial/HMI settings are never rewritten.
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

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
TARGET = {"vendor": "delta", "family": "DVP", "model": "DVP14SS211R", "toolchain": "dvpserial"}


async def run(profile: Path, candidate: Path, expected_sha256: str, output: Path) -> dict:
    if output.exists():
        raise ValueError('EVIDENCE_ALREADY_EXISTS: supply a new output file for each session')
    # The caller supplies the previously frozen digest; computing a new digest
    # here must never silently bless a different candidate.
    actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError("CANDIDATE_SHA_MISMATCH: native file changed before MCP startup")
    config = json.loads(profile.read_text(encoding="utf-8"))
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT / ".agent-state/deps"), env.get("PYTHONPATH", "")])
    env["UNIVERSAL_PLC_ROOT"] = str(ROOT / ".agent-state/dvpserial-runtime")
    env["UNIVERSAL_PLC_READ_ROOTS"] = str(candidate.parent)
    env["UNIVERSAL_PLC_DVPSERIAL_CONFIG"] = str(profile.resolve())
    params = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "universal_plc", "--root", env["UNIVERSAL_PLC_ROOT"],
              "--allow-read", str(candidate.parent), "serve"], cwd=str(ROOT), env=env)
    report = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "mode": "FAST_UNVERIFIED", "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
              "port": config.get("port"), "route": config.get("route"), "target": TARGET,
              "candidate_path": str(candidate), "candidate_sha256": expected_sha256,
              "program_transfer_verified": False, "online_verified": False, "calls": [], "workflows": []}

    def save() -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode('utf-8'))

    save()
    async with Client(params, read_timeout_seconds=40) as client:
        tools = await client.list_tools()
        report["mcp_tools"] = [t.name for t in tools.tools]

        async def call(name: str, arguments: dict) -> dict:
            result = await client.call_tool(name, arguments)
            if result.is_error or result.structured_content is None:
                raise RuntimeError(f"MCP_TOOL_ERROR {name}: {result.content}")
            data = result.structured_content
            report["calls"].append({"tool": name, "result": data})
            save()
            return data

        routing = await call("target.resolve", {"target": TARGET})
        if routing["adapter_id"] != "delta.dvpserial":
            raise RuntimeError("TARGET_ROUTE_MISMATCH")
        await call("capabilities.list", {"target": TARGET})
        workspace = await call("workspace.create", {"target": TARGET, "baseline_path": str(candidate.resolve()),
                                "expected_sha256": expected_sha256, "task_id": "dvpserial-fast-20261013", "revision": "v5"})
        wid = workspace["workspace_id"]
        context = workspace["context_digest"]
        report["workspace_id"] = wid

        async def workflow(kind: str, operations: list[tuple[str, dict]]) -> dict:
            started = await call("workflow.start", {"workspace_id": wid, "workflow": kind})
            result = await call("workflow.run", {"workspace_id": wid, "run_id": started["run_id"],
                               "steps": [{"operation": op, "arguments": args} for op, args in operations]})
            report["workflows"].append({"workflow": kind, "run_id": started["run_id"], "status": result["status"]})
            for completed in result.get("completed", []):
                await call("evidence.read", {"workspace_id": wid, "evidence_id": completed["evidence_id"]})
            save()
            return result

        await workflow("W1", [("serial.ports", {})])
        print("MCP ready; testing configured read endpoint", flush=True)
        try:
            observed = await workflow("W5", [("target.connect", {}), ("target.status", {})])
            readings = [c["result"] for c in observed.get("completed", [])]
            report["online_verified"] = len(readings) == 2 and all(r.get("ok") and r.get("endpoint_verified")
                and isinstance(r.get("running"), bool) and isinstance(r.get("capacity_steps"), int) for r in readings)
            report["readings"] = readings
            if report["online_verified"]:
                print(json.dumps({"online": True, "running": readings[-1]["running"],
                                  "capacity_steps": readings[-1]["capacity_steps"]}), flush=True)
            else:
                print(json.dumps({"online": False, "read_result": readings}), flush=True)
        finally:
            await workflow("W6", [("target.disconnect", {})])
        # Preparation checks the frozen file without accessing the port. The
        # failed backup guard ends W5; download is never scheduled after it.
        prepared = await call("adapter.invoke", {"workspace_id": wid, "operation": "deployment.prepare",
                             "arguments": {"input_path": "baseline.dvp", "expected_sha256": expected_sha256},
                             "expected_context_digest": context})
        report["deployment"] = prepared["result"]
        await workflow("W5", [("project.upload", {})])
        await workflow("W7", [("deployment.reconcile", {})])
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["program_result"] = "PROGRAM_TRANSFER_UNAVAILABLE"
        report["stop_command_sent"] = False
        save()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = asyncio.run(run(args.profile, args.candidate, args.sha256.lower(), args.output))
    print(json.dumps({"online_verified": report["online_verified"], "program_result": report["program_result"],
                      "evidence": str(args.output.resolve())}), flush=True)
    return 0 if report["online_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
