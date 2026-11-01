from __future__ import annotations

PACKAGE_NAME = "universal-plc-axmcp"
PACKAGE_VERSION = "0.4.0"
PROTOCOL_V1 = "AXQ-FILE/1"
PROTOCOL_V2 = "AXQ-FILE/2"
# Compatibility alias used by the qualified read-only bridge.
PROTOCOL = PROTOCOL_V1
REPLAY_MODE = "recorded_replay"
FILE_BRIDGE_MODE = "file_bridge"
AUTONOMOUS_MODE = "autonomous_file_bridge"

ALLOWED_OPERATIONS = (
    "environment.probe",
    "project.tree.read",
    "project.inventory.read",
    "source.read",
    "project.export.native",
)

AUTONOMOUS_OPERATIONS = (
    "workspace.create",
    "workspace.status",
    "source.upsert.st",
    "ladder.networks.apply",
    "revision.apply",
    "candidate.snapshot",
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
    "job.progress",
    "operator.profile",
    "operator.evidence.check",
)

PUBLIC_OPERATIONS = ALLOWED_OPERATIONS + AUTONOMOUS_OPERATIONS
AXQ_OPERATIONS = tuple(operation for operation in PUBLIC_OPERATIONS if operation not in {"job.progress", "operator.profile", "operator.evidence.check"})

WORKER_OPERATIONS = (
    "source.upsert.st",
    "ladder.networks.apply",
    "revision.apply",
    "project.build",
    "deployment.execute",
    "deployment.reconcile",
    "target.observe.start",
    "target.observe.stop",
)

JOB_STATES = (
    "queued",
    "running",
    "succeeded",
    "blocked",
    "failed",
    "cancelled",
    "unknown_outcome",
)
DENIED_OPERATION_TOKENS = (
    "eval",
    "_eval",
    "shell",
    "filesystem",
    "import",
    "write",
    "login",
    "download",
    "force",
    "run",
    "stop",
    "reset",
    "plc",
    "online",
    "dev.flag",
    "mcptoolkit_dev",
)
