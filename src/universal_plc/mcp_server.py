from __future__ import annotations

from typing import Any
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .environment import probe
from .service import Service


def create_server(service: Service | None = None) -> MCPServer:
    backend = service or Service.from_environment()
    server = MCPServer(name="Universal PLC MCP", version=__version__, log_level="WARNING",
                       instructions="Resolve the exact vendor/model/toolchain first. Use digest-bound workspaces. Capability, implementation, native qualification and session authority are distinct. Do not replay unknown outcomes.")
    read = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

    @server.tool(name="environment.probe", annotations=read, structured_output=True)
    def environment_probe() -> dict[str, Any]:
        """Inventory local installations without opening an IDE or contacting a PLC."""
        return probe()

    @server.tool(name="target.resolve", annotations=read, structured_output=True)
    def target_resolve(target: dict[str, Any], operation: str | None = None,
                       project_target: dict[str, Any] | None = None) -> dict[str, Any]:
        """Resolve exact model and compatible adapter; ambiguous selection stays explicit."""
        return backend.resolve(target, operation, project_target)

    @server.tool(name="capabilities.list", annotations=read, structured_output=True)
    def capabilities_list(target: dict[str, Any]) -> dict[str, Any]:
        """Read implementation and qualification separately; this grants no authority."""
        return backend.capabilities(target)

    @server.tool(name="workspace.create", annotations=write, structured_output=True)
    def workspace_create(target: dict[str, Any], baseline_path: str | None = None,
                         expected_sha256: str | None = None,
                         project_target: dict[str, Any] | None = None,
                         task_id: str | None = None, revision: str | None = None) -> dict[str, Any]:
        """Create isolated file workspace. This is not native project.create; optional baseline is copied from configured read roots."""
        return backend.workspace_create(target, baseline_path, expected_sha256, project_target, task_id, revision)

    @server.tool(name="workspace.status", annotations=read, structured_output=True)
    def workspace_status(workspace_id: str) -> dict[str, Any]:
        """Verify context and original baseline hashes and read workspace identity."""
        return backend.workspace(workspace_id)[1]

    @server.tool(name="workspace.import", annotations=write, structured_output=True)
    def workspace_import(workspace_id: str, source_path: str, destination: str, expected_sha256: str) -> dict[str, Any]:
        """Copy a hash-locked input from configured read roots; destination is workspace-relative and must not exist."""
        return backend.import_file(workspace_id, source_path, destination, expected_sha256)

    @server.tool(name="adapter.invoke", annotations=write, structured_output=True)
    def adapter_invoke(workspace_id: str, operation: str, arguments: dict[str, Any],
                       expected_context_digest: str) -> dict[str, Any]:
        """Invoke an explicitly registered typed capability. No arbitrary shell/eval; backend qualification and authority gates remain in force."""
        return backend.invoke(workspace_id, operation, arguments, expected_context_digest)

    @server.tool(name="evidence.read", annotations=read, structured_output=True)
    def evidence_read(workspace_id: str, evidence_id: str) -> dict[str, Any]:
        """Read durable operation evidence, including incomplete intents after restart."""
        return backend.evidence_read(workspace_id, evidence_id)

    @server.tool(name="workflow.start", annotations=write, structured_output=True)
    def workflow_start(workspace_id: str, workflow: str) -> dict[str, Any]:
        """Create a durable W1..W7 workflow; does not execute or grant authority."""
        return backend.workflow_start(workspace_id, workflow)

    @server.tool(name="workflow.run", annotations=write, structured_output=True)
    def workflow_run(workspace_id: str, run_id: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        """Execute explicit allowed steps once; dependent queued work stops the chain for evidence review."""
        return backend.workflow_run(workspace_id, run_id, steps)

    @server.tool(name="workflow.status", annotations=read, structured_output=True)
    def workflow_status(workspace_id: str, run_id: str) -> dict[str, Any]:
        """Resume visibility after disconnect without retrying side effects."""
        return backend.workflow_status(workspace_id, run_id)

    return server


def main() -> None:
    create_server().run(transport="stdio")
