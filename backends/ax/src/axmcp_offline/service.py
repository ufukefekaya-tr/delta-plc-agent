from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from .autonomous import AutonomousHost
from .ax_deployment_supervisor import AxDeploymentSupervisor
from .ax_supervisor import AxWorkerSupervisor
from .bridge import FileBridgeBackend
from .constants import AUTONOMOUS_MODE, FILE_BRIDGE_MODE, REPLAY_MODE
from .records import validate_operation
from .replay import ReplayBackend
from .security import ProtocolViolation


class Backend(Protocol):
    def execute(self, operation: str) -> dict[str, Any]: ...


class OfflineService:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    @classmethod
    def from_environment(cls) -> "OfflineService":
        mode = os.environ.get("AXMCP_MODE", REPLAY_MODE).strip().lower()
        if mode == REPLAY_MODE:
            fixture = os.environ.get("AXMCP_REPLAY_BUNDLE")
            backend = ReplayBackend.from_path(Path(fixture)) if fixture else ReplayBackend.from_default()
            return cls(backend)
        if mode == FILE_BRIDGE_MODE:
            root = os.environ.get("AXMCP_SCRATCH_ROOT")
            if not root:
                raise ProtocolViolation("AXMCP_SCRATCH_ROOT is required in file_bridge mode")
            timeout_text = os.environ.get("AXMCP_TIMEOUT_SECONDS", "3")
            try:
                timeout = int(timeout_text)
            except ValueError as exc:
                raise ProtocolViolation("AXMCP_TIMEOUT_SECONDS must be an integer") from exc
            return cls(FileBridgeBackend(Path(root), timeout_seconds=timeout))
        if mode == AUTONOMOUS_MODE:
            root = os.environ.get("AXMCP_SCRATCH_ROOT")
            if not root:
                raise ProtocolViolation("AXMCP_SCRATCH_ROOT is required in autonomous mode")
            timeout_text = os.environ.get("AXMCP_TIMEOUT_SECONDS", "3")
            try:
                timeout = int(timeout_text)
            except ValueError as exc:
                raise ProtocolViolation("AXMCP_TIMEOUT_SECONDS must be an integer") from exc
            autostart = os.environ.get("AXMCP_AUTOSTART_WORKER", "0").strip()
            if autostart not in {"0", "1"}:
                raise ProtocolViolation("AXMCP_AUTOSTART_WORKER must be 0 or 1")
            deployment_autostart = os.environ.get(
                "AXMCP_AUTOSTART_DEPLOYMENT_WORKER", "0"
            ).strip()
            if deployment_autostart not in {"0", "1"}:
                raise ProtocolViolation(
                    "AXMCP_AUTOSTART_DEPLOYMENT_WORKER must be 0 or 1"
                )
            supervisor = AxWorkerSupervisor(Path(root)) if autostart == "1" else None
            deployment_supervisor = (
                AxDeploymentSupervisor(Path(root))
                if deployment_autostart == "1"
                else None
            )
            return cls(
                AutonomousHost(
                    Path(root),
                    legacy_timeout_seconds=timeout,
                    supervisor=supervisor,
                    deployment_supervisor=deployment_supervisor,
                )
            )
        raise ProtocolViolation(f"unsupported AXMCP_MODE: {mode!r}")

    def execute(self, operation: str) -> dict[str, Any]:
        operation = validate_operation(operation)
        if isinstance(self._backend, AutonomousHost):
            return self._backend.execute_legacy(operation)
        return self._backend.execute(operation)

    @property
    def supports_autonomous(self) -> bool:
        return isinstance(self._backend, AutonomousHost)

    @property
    def autonomous(self) -> AutonomousHost:
        if not isinstance(self._backend, AutonomousHost):
            raise ProtocolViolation("autonomous tools require AXMCP_MODE=autonomous_file_bridge")
        return self._backend
