from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Protocol

from .models import PLCError


class Adapter(Protocol):
    def invoke(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def capabilities(self) -> list[dict[str, Any]]: ...


class AdapterManager:
    def __init__(self, ax_config: str | None = None):
        self.ax_config = ax_config
        self.instances: dict[str, Adapter] = {}

    def register(self, adapter_id: str, instance: Adapter) -> None:
        if adapter_id in self.instances:
            raise PLCError(f"Adapter already registered: {adapter_id}")
        self.instances[adapter_id] = instance

    def get(self, adapter_id: str) -> Adapter:
        if adapter_id in self.instances:
            return self.instances[adapter_id]
        if adapter_id == "delta.ax":
            try:
                from axmcp_offline.universal_adapter import AxAdapter
            except ImportError as exc:
                raise PLCError("AX_BACKEND_NOT_INSTALLED: install the bundled backends/ax package") from exc
            adapter = AxAdapter(self.ax_config)
        elif adapter_id == "delta.dvpserial":
            from .dvp_serial import DvpSerialAdapter
            adapter = DvpSerialAdapter()
        elif adapter_id in {"delta.ispsoft", "delta.wplsoft", "delta.diadesigner"}:
            from .delta_files import DeltaFileAdapter
            adapter = DeltaFileAdapter(adapter_id.split(".", 1)[1])
        else:
            matches = [ep for ep in entry_points(group="universal_plc.adapters") if ep.name == adapter_id]
            if len(matches) != 1:
                raise PLCError(f"ADAPTER_NOT_INSTALLED: {adapter_id}")
            adapter = matches[0].load()()
        self.instances[adapter_id] = adapter
        return adapter
