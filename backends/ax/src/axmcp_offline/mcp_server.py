from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .constants import PACKAGE_VERSION
from .operator_evidence import profile, check_evidence
from .security import ProtocolViolation, validate_digest
from .service import OfflineService


_SERVER_INSTRUCTIONS = (
    "Offline by default: recorded_replay is simulation; a file bridge is not PLC permission. "
    "PLC source changes require explicit permission for a named revision; it persists through "
    "that revision's edit/build/download/RUN/watch/fix loop until done or newly restricted. "
    "Each artifact/target still needs bounded authority. After transfer intent, timeout or "
    "uncertainty retains worker/session: no kill, no cleanup, no RUN, no blind retry. job.progress "
    "reads evidence only. "
    "Create a hash-locked workspace, build, and freeze only a zero-error/zero-warning result. "
    "deployment.execute requires deployment.prepare and the current exact 'yükle' authority. "
    "For DIACloud, reuse the build's absolute workflow_deadline_at for deployment; do not refresh "
    "the 600-second budget. Confirm download and initialization complete before RUN. A heartbeat "
    "is not transfer progress; bytes and percent remain unknown without vendor evidence. "
    "Never invent a target fingerprint or request force, arbitrary online writes, or safety bypass."
)

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

_SCRATCH_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)

_DEPLOYMENT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)


def _expected_tool_call(call: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    """Expose fail-closed protocol rejections to the MCP caller without a crash mask."""

    try:
        return call(*args)
    except ProtocolViolation as exc:
        raise ToolError(str(exc)) from exc


def create_server(service: OfflineService | None = None) -> MCPServer:
    backend = service or OfflineService.from_environment()
    server = MCPServer(
        name="Universal PLC AX MCP",
        title="Universal PLC bounded AX automation host",
        description=(
            "Default-deny AX MCP facade. Replay stays simulated; file bridges require "
            "validated AXQ-FILE evidence and autonomous deployment remains fingerprint gated."
        ),
        instructions=_SERVER_INSTRUCTIONS,
        version=PACKAGE_VERSION,
        log_level="WARNING",
    )

    @server.tool(
        name="environment.probe",
        description="Return the bounded environment identity result without contacting a PLC.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def environment_probe() -> dict[str, Any]:
        return _expected_tool_call(backend.execute, "environment.probe")

    @server.tool(
        name="project.tree.read",
        description="Read the recorded or AXQ-validated scratch project tree.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def project_tree_read() -> dict[str, Any]:
        return _expected_tool_call(backend.execute, "project.tree.read")

    @server.tool(
        name="project.inventory.read",
        description="Read device, library and task inventory from the bounded backend.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def project_inventory_read() -> dict[str, Any]:
        return _expected_tool_call(backend.execute, "project.inventory.read")

    @server.tool(
        name="source.read",
        description="Read the source target selected in the scratch probe configuration.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def source_read(
        workspace_id: str | None = None,
        expected_revision: int | None = None,
        target: str | None = None,
    ) -> dict[str, Any]:
        if backend.supports_autonomous:
            if workspace_id is None or expected_revision is None or target is None:
                raise ToolError(
                    "autonomous source.read requires workspace_id, expected_revision and target"
                )
            return _expected_tool_call(
                backend.autonomous.source_read,
                workspace_id,
                expected_revision,
                target,
            )
        if workspace_id is not None or expected_revision is not None or target is not None:
            raise ToolError("recorded/file-bridge source.read accepts no arguments")
        return _expected_tool_call(backend.execute, "source.read")

    @server.tool(
        name="project.export.native",
        description="Return the bounded native-export result for the configured scratch path.",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def project_export_native() -> dict[str, Any]:
        return _expected_tool_call(backend.execute, "project.export.native")

    if backend.supports_autonomous:
        host = backend.autonomous

        @server.tool(
            name="workspace.create",
            description=(
                "Create a scratch workspace from a lowercase sha256 digest. Omit "
                "baseline_path: it is deterministically derived as "
                "baselines/<64 lowercase hex>.project. If supplied for compatibility, "
                "baseline_path must exactly equal that digest-derived path."
            ),
            annotations=_SCRATCH_WRITE,
            structured_output=True,
        )
        def workspace_create(
            baseline_sha256: str,
            environment_digest: str,
            baseline_path: str | None = None,
        ) -> dict[str, Any]:
            validate_digest(baseline_sha256)
            derived_path = f"baselines/{baseline_sha256.removeprefix('sha256:')}.project"
            if baseline_path is None:
                baseline_path = derived_path
            elif baseline_path != derived_path:
                raise ToolError(
                    "baseline_path must equal the deterministic digest-derived path"
                )
            return _expected_tool_call(
                host.workspace_create, baseline_path, baseline_sha256, environment_digest
            )

        @server.tool(
            name="workspace.status",
            description="Return revision, digest, pending job and dirty state for one workspace.",
            annotations=_READ_ONLY,
            structured_output=True,
        )
        def workspace_status(workspace_id: str) -> dict[str, Any]:
            return _expected_tool_call(host.workspace_status, workspace_id)

        @server.tool(
            name="source.upsert.st",
            description=(
                "Queue a bounded ST POU create/update against an exact workspace revision. "
                "pou_kind is lowercase and must be program, function_block, function, or gvl."
            ),
            annotations=_SCRATCH_WRITE,
            structured_output=True,
        )
        def source_upsert_st(
            workspace_id: str,
            expected_revision: int,
            pou_name: str,
            pou_kind: Literal["program", "function_block", "function", "gvl"],
            declaration: str,
            implementation: str,
            return_type: str | None = None,
        ) -> dict[str, Any]:
            return _expected_tool_call(
                host.source_upsert_st,
                workspace_id,
                expected_revision,
                pou_name,
                pou_kind,
                declaration,
                implementation,
                return_type,
            )

        @server.tool(
            name="ladder.networks.apply",
            description="Queue only a strict project-bound LadderRecipe v2.",
            annotations=_SCRATCH_WRITE,
            structured_output=True,
        )
        def ladder_networks_apply(
            workspace_id: str,
            expected_revision: int,
            recipe: dict[str, Any],
        ) -> dict[str, Any]:
            return _expected_tool_call(
                host.ladder_networks_apply, workspace_id, expected_revision, recipe
            )

        @server.tool(
            name="revision.apply",
            description="Apply bounded ST and the qualified same-interface single-contact MachineSupervisor LD subset in one AX save/reopen transaction. No general LD editing; build is separate.",
            annotations=_SCRATCH_WRITE,
            structured_output=True,
        )
        def revision_apply(
            workspace_id: str,
            expected_revision: int,
            idempotency_key: str,
            st_changes: list[dict[str, Any]],
            ladder_changes: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return _expected_tool_call(
                host.revision_apply, workspace_id, expected_revision, idempotency_key,
                st_changes, ladder_changes,
            )

        @server.tool(
            name="candidate.snapshot",
            description="Publish an immutable native save/reopen candidate without build or deployment claims.",
            annotations=_SCRATCH_WRITE,
            structured_output=True,
        )
        def candidate_snapshot(workspace_id: str, expected_revision: int) -> dict[str, Any]:
            return _expected_tool_call(host.candidate_snapshot, workspace_id, expected_revision)

        @server.tool(
            name="project.build",
            description="Queue build/rebuild with at most ten bounded compiler attempts.",
            annotations=_SCRATCH_WRITE,
            structured_output=True,
        )
        def project_build(
            workspace_id: str,
            expected_revision: int,
            mode: Literal["build", "rebuild"] = "rebuild",
            max_attempts: int = 10,
            transport_profile: Literal["lan", "diacloud"] = "lan",
            workflow_deadline_at: str | None = None,
        ) -> dict[str, Any]:
            return _expected_tool_call(
                host.project_build, workspace_id, expected_revision, mode, max_attempts,
                transport_profile, workflow_deadline_at,
            )

        @server.tool(
            name="artifact.freeze",
            description="Freeze the exact bytes proven by the current qualifying build.",
            annotations=_SCRATCH_WRITE,
            structured_output=True,
        )
        def artifact_freeze(workspace_id: str, expected_revision: int) -> dict[str, Any]:
            return _expected_tool_call(host.artifact_freeze, workspace_id, expected_revision)

        @server.tool(
            name="deployment.prepare",
            description="Prepare a time-bounded artifact/target/rollback plan without contacting a target.",
            annotations=_SCRATCH_WRITE,
            structured_output=True,
        )
        def deployment_prepare(
            artifact_digest: str,
            target_fingerprint: str,
            rollback_digest: str,
            observation_profile: dict[str, Any],
        ) -> dict[str, Any]:
            return _expected_tool_call(
                host.deployment_prepare,
                artifact_digest,
                target_fingerprint,
                rollback_digest,
                observation_profile,
            )

        @server.tool(
            name="deployment.execute",
            description=(
                "Consume one unexpired deployment plan only with exact current 'yükle' authority; "
                "UNKNOWN_OUTCOME is never blindly retried."
            ),
            annotations=_DEPLOYMENT,
            structured_output=True,
        )
        def deployment_execute(
            plan_id: str,
            artifact_digest: str,
            target_fingerprint: str,
            rollback_digest: str,
            expires_at: str,
            challenge_nonce: str,
            authorization_phrase: str,
            transport_profile: Literal["lan", "diacloud"] = "lan",
            workflow_deadline_at: str | None = None,
        ) -> dict[str, Any]:
            return _expected_tool_call(
                host.deployment_execute,
                plan_id,
                artifact_digest,
                target_fingerprint,
                rollback_digest,
                expires_at,
                challenge_nonce,
                authorization_phrase,
                transport_profile,
                workflow_deadline_at,
            )

        @server.tool(
            name="deployment.reconcile",
            description=(
                "Read the exact target state after UNKNOWN_OUTCOME; never retries download, "
                "start, stop or rollback."
            ),
            annotations=_READ_ONLY,
            structured_output=True,
        )
        def deployment_reconcile(
            original_job_id: str,
            target_fingerprint: str,
            artifact_digest: str,
            rollback_digest: str,
        ) -> dict[str, Any]:
            return _expected_tool_call(
                host.deployment_reconcile,
                original_job_id,
                target_fingerprint,
                artifact_digest,
                rollback_digest,
            )

        @server.tool(
            name="target.observe.start",
            description="Queue bounded read-only observation for exact allowlisted expressions.",
            annotations=_DEPLOYMENT,
            structured_output=True,
        )
        def target_observe_start(
            target_fingerprint: str,
            observation_profile: dict[str, Any],
        ) -> dict[str, Any]:
            return _expected_tool_call(
                host.target_observe_start, target_fingerprint, observation_profile
            )

        @server.tool(
            name="target.observe.read",
            description="Read up to the latest 100 immutable observation samples and job state.",
            annotations=_READ_ONLY,
            structured_output=True,
        )
        def target_observe_read(observation_id: str) -> dict[str, Any]:
            return _expected_tool_call(host.target_observe_read, observation_id)

        @server.tool(
            name="target.observe.stop",
            description="Queue a bounded stop for an existing observation session.",
            annotations=_DEPLOYMENT,
            structured_output=True,
        )
        def target_observe_stop(observation_id: str) -> dict[str, Any]:
            return _expected_tool_call(host.target_observe_stop, observation_id)

        @server.tool(
            name="job.status",
            description="Read the durable queued/running/terminal state of one job.",
            annotations=_READ_ONLY,
            structured_output=True,
        )
        def job_status(job_id: str) -> dict[str, Any]:
            return _expected_tool_call(host.job_status, job_id)

        @server.tool(
            name="job.cancel",
            description="Cancel a queued job or place an immutable cancellation marker for a running job.",
            annotations=_DEPLOYMENT,
            structured_output=True,
        )
        def job_cancel(job_id: str) -> dict[str, Any]:
            return _expected_tool_call(host.job_cancel, job_id)

        @server.tool(
            name="job.progress",
            description="Read durable stage and deadline evidence without launching a worker.",
            annotations=_READ_ONLY,
            structured_output=True,
        )
        def job_progress(job_id: str) -> dict[str, Any]:
            return _expected_tool_call(host.job_progress, job_id)

        @server.tool(name="operator.profile", description="Read the synthetic six-drive field contract; no live dispatch or target connection.", annotations=_READ_ONLY, structured_output=True)
        def operator_profile(profile_id: str = "example-six-drive-v1") -> dict[str, Any]:
            return _expected_tool_call(profile, profile_id)

        @server.tool(name="operator.evidence.check", description="Check supplied watch, coefficient ACK or ramp evidence. Never proves a live session or writes a PLC.", annotations=_READ_ONLY, structured_output=True)
        def operator_evidence_check(profile_id: str, kind: Literal["watch", "coefficient", "ramps"], evidence: dict[str, Any]) -> dict[str, Any]:
            return _expected_tool_call(check_evidence, profile_id, kind, evidence)

    return server


def run_stdio() -> None:
    create_server().run(transport="stdio")
