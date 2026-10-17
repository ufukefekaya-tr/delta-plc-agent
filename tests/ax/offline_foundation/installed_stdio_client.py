from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


EXPECTED_OPERATIONS = (
    "environment.probe",
    "project.tree.read",
    "project.inventory.read",
    "source.read",
    "project.export.native",
)
EXPECTED_AUTONOMOUS_OPERATIONS = EXPECTED_OPERATIONS + (
    "workspace.create",
    "workspace.status",
    "source.upsert.st",
    "ladder.networks.apply",
    "project.build",
    "artifact.freeze",
    "deployment.prepare",
    "deployment.execute",
    "deployment.reconcile",
    "target.observe.start",
    "target.observe.read",
    "target.observe.stop",
    "job.status",
    "job.cancel",
)


async def _verify(installed_python: str, scratch_root: str | None = None) -> dict[str, object]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if scratch_root is None:
        env["AXMCP_MODE"] = "recorded_replay"
        expected_operations = EXPECTED_OPERATIONS
    else:
        env.update(
            {
                "AXMCP_MODE": "autonomous_file_bridge",
                "AXMCP_SCRATCH_ROOT": scratch_root,
                "AXMCP_AUTOSTART_WORKER": "1",
            }
        )
        expected_operations = EXPECTED_AUTONOMOUS_OPERATIONS
    params = StdioServerParameters(
        command=installed_python,
        args=["-m", "axmcp_offline", "serve"],
        env=env,
    )
    async with Client(params, read_timeout_seconds=10) as client:
        listed = await client.list_tools()
        names = tuple(tool.name for tool in listed.tools)
        if names != expected_operations:
            raise RuntimeError(f"installed tool surface mismatch: {names!r}")
        autonomous_read_only = {
            "environment.probe",
            "project.tree.read",
            "project.inventory.read",
            "source.read",
            "project.export.native",
            "workspace.status",
            "deployment.reconcile",
            "target.observe.read",
            "job.status",
        }
        for tool in listed.tools:
            annotations = tool.annotations
            if scratch_root is None:
                safe = (
                    annotations.read_only_hint
                    and not annotations.destructive_hint
                    and annotations.idempotent_hint
                    and not annotations.open_world_hint
                )
            else:
                safe = (
                    annotations.read_only_hint == (tool.name in autonomous_read_only)
                    and annotations.destructive_hint == (tool.name not in autonomous_read_only)
                    and not annotations.open_world_hint
                )
            if not safe:
                raise RuntimeError(f"unsafe tool annotations: {tool.name}")
        if scratch_root is None:
            called = await client.call_tool("source.read", {})
            if called.is_error or called.structured_content is None:
                raise RuntimeError("installed source.read call failed")
            if called.structured_content.get("evidence_class") != "SIMULATED":
                raise RuntimeError("installed replay result lost its evidence boundary")
            if called.structured_content.get("live_ax_verified") is not False:
                raise RuntimeError("installed replay result elevated live verification")
            called_name = "source.read"
            evidence_class = "SIMULATED"
            live_ax_verified = False
        else:
            root = Path(scratch_root)
            baselines = tuple((root / "v2" / "baselines").glob("*.project"))
            if len(baselines) != 1:
                raise RuntimeError("installed autonomous baseline enrollment is ambiguous")
            baseline = baselines[0]
            baseline_digest = "sha256:" + hashlib.sha256(baseline.read_bytes()).hexdigest()
            environment_digest = json.loads(
                (root / "autonomous-config.json").read_text(encoding="utf-8")
            )["environment_digest"]
            created = await client.call_tool(
                "workspace.create",
                {
                    "baseline_path": f"baselines/{baseline.name}",
                    "baseline_sha256": baseline_digest,
                    "environment_digest": environment_digest,
                },
            )
            if created.is_error or created.structured_content is None:
                raise RuntimeError("installed autonomous workspace.create failed")
            workspace_id = created.structured_content["workspace_id"]
            status = await client.call_tool("workspace.status", {"workspace_id": workspace_id})
            if status.is_error or status.structured_content is None:
                raise RuntimeError("installed autonomous workspace.status failed")
            if status.structured_content.get("current_project_digest") != baseline_digest:
                raise RuntimeError("installed autonomous scratch copy digest drifted")
            called_name = "workspace.create -> workspace.status"
            evidence_class = "SCRATCH_DIGEST_VERIFIED"
            live_ax_verified = False
        return {
            "status": "pass",
            "transport": "stdio",
            "tools": list(names),
            "called": called_name,
            "evidence_class": evidence_class,
            "live_ax_verified": live_ax_verified,
        }


def main() -> int:
    if len(sys.argv) not in {1, 2, 3}:
        raise SystemExit("usage: installed_stdio_client.py [installed-python] [scratch-root]")
    installed_python = sys.argv[1] if len(sys.argv) >= 2 else sys.executable
    scratch_root = sys.argv[2] if len(sys.argv) == 3 else None
    print(
        json.dumps(
            asyncio.run(_verify(installed_python, scratch_root)),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
