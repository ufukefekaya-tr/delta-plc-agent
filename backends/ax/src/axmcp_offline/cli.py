from __future__ import annotations

import argparse
from importlib.resources import files
import json
import os
import platform
import sys
from pathlib import Path
from typing import Sequence

from .autonomous import (
    AutonomousHost,
    configure_autonomous_environment,
    enroll_baseline,
    initialize_autonomous_root,
)
from .ax_deployment_supervisor import write_deployment_launcher_config
from .ax_supervisor import write_launcher_config
from .bridge import FileBridgeBackend, initialize_scratch
from .constants import (
    ALLOWED_OPERATIONS,
    AUTONOMOUS_MODE,
    FILE_BRIDGE_MODE,
    PACKAGE_NAME,
    PACKAGE_VERSION,
    PUBLIC_OPERATIONS,
    REPLAY_MODE,
)
from .mcp_server import run_stdio
from .network_diagnostics import (
    collect_local_windows_snapshot,
    evaluate_network_snapshot,
    load_json_object,
)
from .replay import ReplayBackend
from .security import (
    ProtocolViolation,
    atomic_write_exclusive,
    atomic_write_replace,
    sha256_file,
    strict_json_loads_v2,
    validate_digest,
)
from .service import OfflineService


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _doctor(args: argparse.Namespace) -> int:
    mode = os.environ.get("AXMCP_MODE", REPLAY_MODE).strip().lower()
    checks: list[dict[str, object]] = []
    checks.append(
        {
            "check": "python",
            "status": "pass" if sys.version_info >= (3, 11) else "fail",
            "value": platform.python_version(),
        }
    )
    checks.append({"check": "package_version", "status": "pass", "value": PACKAGE_VERSION})
    try:
        if mode == REPLAY_MODE:
            backend = ReplayBackend.from_default()
            checks.append(
                {
                    "check": "replay_bundle",
                    "status": "pass",
                    "environment_digest": backend.environment_digest,
                }
            )
        elif mode == FILE_BRIDGE_MODE:
            root_text = os.environ.get("AXMCP_SCRATCH_ROOT")
            if not root_text:
                raise ProtocolViolation("AXMCP_SCRATCH_ROOT is required")
            FileBridgeBackend(Path(root_text), timeout_seconds=1)._load_config()
            checks.append({"check": "file_bridge", "status": "pass", "root": root_text})
        elif mode == AUTONOMOUS_MODE:
            root_text = os.environ.get("AXMCP_SCRATCH_ROOT")
            if not root_text:
                raise ProtocolViolation("AXMCP_SCRATCH_ROOT is required")
            service = OfflineService.from_environment()
            host = service.autonomous
            legacy_config = host.legacy._load_config()
            if legacy_config["environment_digest"] != host.environment_digest:
                raise ProtocolViolation("V1/V2 environment digests do not match")
            target_count = len(host.config["target_allowlist"])
            deployment_launcher_ready = host.deployment_supervisor is not None
            checks.append(
                {
                    "check": "autonomous_file_bridge",
                    "status": "pass",
                    "root": root_text,
                    "environment_digest": host.environment_digest,
                    "target_count": target_count,
                    "ax_worker_autostart": host.supervisor is not None,
                    "deployment_worker_autostart": deployment_launcher_ready,
                }
            )
            checks.append(
                {
                    "check": "ax_worker_launcher",
                    "status": "pass" if host.supervisor is not None else "fail",
                    "diagnostic": None
                    if host.supervisor is not None
                    else "AXMCP_AUTOSTART_WORKER is disabled",
                }
            )
            checks.append(
                {
                    "check": "deployment_worker_launcher",
                    "status": "pass"
                    if target_count == 0 or deployment_launcher_ready
                    else "fail",
                    "diagnostic": None
                    if target_count == 0 or deployment_launcher_ready
                    else "target allowlist is non-empty but no deployment worker launcher is configured",
                    "required": target_count > 0,
                }
            )
        else:
            raise ProtocolViolation(f"unsupported AXMCP_MODE: {mode}")
    except (OSError, ProtocolViolation) as exc:
        checks.append({"check": "backend", "status": "fail", "diagnostic": str(exc)})
    passed = all(item["status"] == "pass" for item in checks)
    payload: dict[str, object] = {
        "package": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "mode": mode,
        "ready": passed,
        "readiness_scope": "package_backend_only",
        "live_ax_verified": False,
        "checks": checks,
    }
    network_requested = any((
        args.transport, args.target_ip, args.expected_adapter_id,
        args.expected_tunnel_id, args.network_profile, args.network_snapshot,
        args.network_collect,
    ))
    if network_requested:
        if not args.transport or not args.target_ip:
            raise ProtocolViolation("network diagnosis requires --transport and --target-ip")
        if not args.network_snapshot and not args.network_collect:
            raise ProtocolViolation("network diagnosis requires --network-snapshot or --network-collect")
        try:
            profile = load_json_object(args.network_profile) if args.network_profile else {}
            if args.expected_adapter_id:
                profile["expected_adapter_id"] = args.expected_adapter_id
            if args.expected_tunnel_id:
                profile["expected_tunnel_id"] = args.expected_tunnel_id
            snapshot = (
                collect_local_windows_snapshot(args.target_ip)
                if args.network_collect else load_json_object(args.network_snapshot)
            )
            payload["network_diagnostics"] = evaluate_network_snapshot(
                transport=args.transport,
                target_ip=args.target_ip,
                profile=profile,
                snapshot=snapshot,
                snapshot_origin="local_windows_inventory" if args.network_collect else "supplied",
            )
        except ValueError as exc:
            raise ProtocolViolation(str(exc)) from exc
    _json_print(payload)
    network = payload.get("network_diagnostics")
    network_consistent = network is None or network["status"] == "consistent"
    return 0 if passed and network_consistent else 2


def _smoke() -> int:
    service = OfflineService.from_environment()
    results = [service.execute(operation) for operation in ALLOWED_OPERATIONS]
    expected_mode = os.environ.get("AXMCP_MODE", REPLAY_MODE).strip().lower()
    if [item["operation"] for item in results] != list(ALLOWED_OPERATIONS):
        raise ProtocolViolation("smoke operation order mismatch")
    if expected_mode == REPLAY_MODE:
        if not all(
            item["status"] == "unverified"
            and item["evidence_class"] == "SIMULATED"
            and item["live_ax_verified"] is False
            for item in results
        ):
            raise ProtocolViolation("replay smoke attempted to elevate simulated evidence")
    elif expected_mode == FILE_BRIDGE_MODE and any(
        item["status"] == "blocked" for item in results
    ):
        raise ProtocolViolation("file bridge smoke did not receive all five validated results")
    _json_print(
        {
            "status": "pass",
            "mode": expected_mode,
            "operations": [item["operation"] for item in results],
            "live_ax_verified": all(item.get("live_ax_verified") is True for item in results),
        }
    )
    return 0


def _print_config() -> int:
    _json_print(
        {
            "mcpServers": {
                "universal-plc-axmcp-offline": {
                    "command": sys.executable,
                    "args": ["-m", "axmcp_offline", "serve"],
                    "env": {"AXMCP_MODE": REPLAY_MODE},
                }
            }
        }
    )
    return 0


def _autonomous_smoke() -> int:
    service = OfflineService.from_environment()
    if not service.supports_autonomous:
        raise ProtocolViolation("autonomous-smoke requires AXMCP_MODE=autonomous_file_bridge")
    host = service.autonomous
    recovery = host.queue.recover()
    legacy_config = host.legacy._load_config()
    if legacy_config["environment_digest"] != host.environment_digest:
        raise ProtocolViolation("V1/V2 environment digests do not match")
    target_count = len(host.config["target_allowlist"])
    deployment_launcher_ready = host.deployment_supervisor is not None
    ready = host.supervisor is not None and (
        target_count == 0 or deployment_launcher_ready
    )
    _json_print(
        {
            "status": "pass" if ready else "blocked",
            "ready": ready,
            "mode": AUTONOMOUS_MODE,
            "protocol": "AXQ-FILE/2",
            "public_tools": list(PUBLIC_OPERATIONS),
            "environment_digest": host.environment_digest,
            "target_count": target_count,
            "queue_recovery": recovery,
            "ax_worker_autostart": host.supervisor is not None,
            "deployment_worker_autostart": deployment_launcher_ready,
            "ide_or_target_contacted": False,
        }
    )
    return 0 if ready else 2


def _packaged_ax_worker() -> tuple[Path, Path]:
    package = files("axmcp_offline")
    worker = Path(str(package.joinpath("ax_worker/worker.py")))
    helper = Path(str(package.joinpath("ax_worker/ladder_import.py")))
    if not worker.is_file() or not helper.is_file():
        development_root = Path(__file__).resolve().parents[2]
        worker = development_root / "adapters" / "ax_worker" / "worker.py"
        helper = development_root / "adapters" / "ax_worker" / "ladder_import.py"
    if not worker.is_file() or not helper.is_file():
        raise ProtocolViolation("packaged AX worker bundle is unavailable")
    return worker.resolve(), helper.resolve()


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_codex_toml(python: Path, scratch_root: Path) -> bytes:
    lines = [
        "[mcp_servers.universal-plc-axmcp]",
        f"command = {_toml_string(str(python.resolve()))}",
        'args = ["-m", "axmcp_offline", "serve"]',
        "startup_timeout_sec = 20",
        "tool_timeout_sec = 330",
        "enabled = true",
        "required = true",
        'default_tools_approval_mode = "writes"',
        "",
        "[mcp_servers.universal-plc-axmcp.env]",
        'AXMCP_MODE = "autonomous_file_bridge"',
        f"AXMCP_SCRATCH_ROOT = {_toml_string(str(scratch_root.resolve()))}",
        'AXMCP_TIMEOUT_SECONDS = "3"',
        'AXMCP_AUTOSTART_WORKER = "1"',
        'AXMCP_AUTOSTART_DEPLOYMENT_WORKER = "0"',
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _packaged_ax_deployment_worker() -> Path:
    package = files("axmcp_offline")
    worker = Path(str(package.joinpath("ax_deployment_worker/worker.py")))
    if not worker.is_file():
        development_root = Path(__file__).resolve().parents[2]
        worker = development_root / "adapters" / "ax_deployment_worker" / "worker.py"
    if not worker.is_file() or worker.is_symlink():
        raise ProtocolViolation("packaged AX deployment worker is unavailable")
    return worker.resolve()


def _setup_regular_file(path: Path, field: str) -> Path:
    if path.is_symlink():
        raise ProtocolViolation(f"{field} cannot be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ProtocolViolation(f"{field} is unavailable") from exc
    if not resolved.is_file():
        raise ProtocolViolation(f"{field} must be a regular file")
    return resolved


def _setup_autonomous(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    validate_digest(args.environment_digest)
    validate_digest(args.baseline_sha256)
    placeholder = "sha256:" + ("0" * 64)
    if args.environment_digest == placeholder or args.baseline_sha256 == placeholder:
        raise ProtocolViolation("setup digests cannot remain placeholders")
    if args.profile != "DIADesigner-AX 1.9":
        raise ProtocolViolation("only the frozen DIADesigner-AX 1.9 profile is supported")
    baseline_source = _setup_regular_file(args.baseline, "baseline")
    ide_executable = _setup_regular_file(args.ide_executable, "ide_executable")
    script_engine = _setup_regular_file(args.script_engine, "script_engine")
    if sha256_file(baseline_source) != args.baseline_sha256:
        raise ProtocolViolation("baseline source digest mismatch")
    worker, helper = _packaged_ax_worker()
    worker_digest = sha256_file(worker)
    helper_digest = sha256_file(helper)
    ide_digest = sha256_file(ide_executable)
    engine_digest = sha256_file(script_engine)
    for reserved in (root / "ax-launcher-config.json", root / "codex-mcp.toml"):
        if reserved.exists():
            raise ProtocolViolation(f"setup output already exists: {reserved.name}")
    enrolled = root / "v2" / "baselines" / f"{args.baseline_sha256[7:]}.project"
    if enrolled.exists() and (enrolled.is_symlink() or sha256_file(enrolled) != args.baseline_sha256):
        raise ProtocolViolation("baseline destination already exists with different bytes")
    initialize_autonomous_root(root)
    synchronized = configure_autonomous_environment(root, args.environment_digest)
    baseline = enroll_baseline(root, baseline_source, args.baseline_sha256)
    launcher = write_launcher_config(
        root,
        ide_executable=ide_executable,
        ide_executable_sha256=ide_digest,
        script_engine=script_engine,
        script_engine_sha256=engine_digest,
        ide_profile=args.profile,
        worker_script=worker,
        worker_script_sha256=worker_digest,
        ladder_helper=helper,
        ladder_helper_sha256=helper_digest,
        transport_profile=args.transport_profile,
    )
    codex_path = root / "codex-mcp.toml"
    atomic_write_exclusive(codex_path, _render_codex_toml(Path(sys.executable), root))
    _json_print(
        {
            "status": "configured",
            "root": str(root),
            "environment": synchronized,
            "baseline": baseline,
            "launcher_config": str(launcher),
            "ide_executable_sha256": ide_digest,
            "script_engine_sha256": engine_digest,
            "worker_script": str(worker),
            "worker_script_sha256": worker_digest,
            "ladder_helper": str(helper),
            "ladder_helper_sha256": helper_digest,
            "codex_toml": str(codex_path),
            "ide_or_target_contacted": False,
        }
    )
    return 0


def _configure_deployment_worker(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    project_launcher_path = root / "ax-launcher-config.json"
    try:
        project_launcher = strict_json_loads_v2(project_launcher_path.read_bytes())
    except (OSError, ProtocolViolation) as exc:
        raise ProtocolViolation(
            "configure-ax-deployment-worker requires a valid project worker setup"
        ) from exc
    required = {
        "ide_executable",
        "ide_executable_sha256",
        "script_engine",
        "script_engine_sha256",
        "ide_profile",
    }
    if not required.issubset(project_launcher):
        raise ProtocolViolation("project launcher identity fields are incomplete")
    adapter = _setup_regular_file(args.target_adapter, "target_adapter")
    enrollment = _setup_regular_file(args.target_enrollment, "target_enrollment")
    validate_digest(args.target_adapter_sha256)
    validate_digest(args.target_enrollment_sha256)
    if sha256_file(adapter) != args.target_adapter_sha256:
        raise ProtocolViolation("target adapter source digest mismatch")
    if sha256_file(enrollment) != args.target_enrollment_sha256:
        raise ProtocolViolation("target enrollment digest mismatch")
    worker = _packaged_ax_deployment_worker()
    launcher = write_deployment_launcher_config(
        root,
        ide_executable=Path(project_launcher["ide_executable"]),
        ide_executable_sha256=project_launcher["ide_executable_sha256"],
        script_engine=Path(project_launcher["script_engine"]),
        script_engine_sha256=project_launcher["script_engine_sha256"],
        ide_profile=project_launcher["ide_profile"],
        worker_script=worker,
        worker_script_sha256=sha256_file(worker),
        target_adapter=adapter,
        target_adapter_sha256=args.target_adapter_sha256,
        target_enrollment=enrollment,
        target_enrollment_sha256=args.target_enrollment_sha256,
        replace=args.replace,
        transport_profile=args.transport_profile,
    )
    codex_path = root / "codex-mcp.toml"
    if codex_path.is_file():
        original = codex_path.read_text(encoding="utf-8")
        disabled = 'AXMCP_AUTOSTART_DEPLOYMENT_WORKER = "0"'
        enabled = 'AXMCP_AUTOSTART_DEPLOYMENT_WORKER = "1"'
        if disabled in original:
            atomic_write_replace(
                codex_path, original.replace(disabled, enabled, 1).encode("utf-8")
            )
        elif enabled not in original:
            raise ProtocolViolation("Codex MCP config lacks the deployment autostart gate")
    _json_print(
        {
            "status": "configured",
            "deployment_launcher_config": str(launcher),
            "worker_script": str(worker),
            "worker_script_sha256": sha256_file(worker),
            "target_adapter_sha256": args.target_adapter_sha256,
            "target_enrollment_sha256": args.target_enrollment_sha256,
            "codex_toml": str(codex_path) if codex_path.is_file() else None,
            "deployment_autostart": True if codex_path.is_file() else False,
            "ide_or_target_contacted": False,
        }
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="axmcp-offline")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="Run the MCP server over stdio")
    doctor = sub.add_parser("doctor", help="Validate the package; optionally assess an explicit network snapshot")
    doctor.add_argument("--transport", choices=("lan", "diacloud"))
    doctor.add_argument("--target-ip", help="Explicit IPv4 target for selected-route analysis")
    doctor.add_argument("--expected-adapter-id", help="Expected source adapter identity")
    doctor.add_argument("--expected-tunnel-id", help="Expected DIACloud tunnel identity")
    doctor.add_argument("--network-profile", type=Path, help="Read-only JSON expected identities and subnet profile")
    network_input = doctor.add_mutually_exclusive_group()
    network_input.add_argument("--network-snapshot", type=Path, help="Evaluate a supplied UTF-8 JSON observation")
    network_input.add_argument("--network-collect", action="store_true", help="Explicitly read bounded local Windows route/process metadata; no target probes")
    sub.add_parser("smoke", help="Run all five allowlisted operations headlessly")
    sub.add_parser("print-config", help="Print a ready-to-paste stdio host configuration")
    sub.add_parser("autonomous-smoke", help="Validate AXQ-FILE/2 host state without IDE/target contact")
    init = sub.add_parser("init-scratch", help="Initialize an AXQ-FILE scratch root")
    init.add_argument("root", type=Path)
    init_v2 = sub.add_parser("init-autonomous", help="Initialize a hybrid AXQ-FILE/1 + /2 root")
    init_v2.add_argument("root", type=Path)
    configure = sub.add_parser(
        "configure-ax-worker",
        help="Pin the exact DIADesigner/ScriptEngine/worker identities without launching the IDE",
    )
    configure.add_argument("root", type=Path)
    configure.add_argument("--ide-executable", type=Path, required=True)
    configure.add_argument("--ide-sha256", required=True)
    configure.add_argument("--script-engine", type=Path, required=True)
    configure.add_argument("--script-engine-sha256", required=True)
    configure.add_argument("--worker-script", type=Path, required=True)
    configure.add_argument("--worker-script-sha256", required=True)
    configure.add_argument("--profile", default="DIADesigner-AX 1.9")
    configure.add_argument("--transport-profile", choices=("lan", "diacloud"), default="lan")
    setup = sub.add_parser(
        "setup-autonomous",
        help="Initialize, identity-pin and baseline-enroll one private AX MCP root",
    )
    setup.add_argument("root", type=Path)
    setup.add_argument("--environment-digest", required=True)
    setup.add_argument("--baseline", type=Path, required=True)
    setup.add_argument("--baseline-sha256", required=True)
    setup.add_argument("--ide-executable", type=Path, required=True)
    setup.add_argument("--script-engine", type=Path, required=True)
    setup.add_argument("--profile", default="DIADesigner-AX 1.9")
    setup.add_argument("--transport-profile", choices=("lan", "diacloud"), default="lan")
    deployment = sub.add_parser(
        "configure-ax-deployment-worker",
        help="Pin exact target enrollment and vendor adapter without launching the IDE",
    )
    deployment.add_argument("root", type=Path)
    deployment.add_argument("--target-adapter", type=Path, required=True)
    deployment.add_argument("--target-adapter-sha256", required=True)
    deployment.add_argument("--target-enrollment", type=Path, required=True)
    deployment.add_argument("--target-enrollment-sha256", required=True)
    deployment.add_argument("--replace", action="store_true")
    deployment.add_argument("--transport-profile", choices=("lan", "diacloud"), default="lan")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            run_stdio()
            return 0
        if args.command == "doctor":
            return _doctor(args)
        if args.command == "smoke":
            return _smoke()
        if args.command == "print-config":
            return _print_config()
        if args.command == "autonomous-smoke":
            return _autonomous_smoke()
        if args.command == "init-scratch":
            config = initialize_scratch(args.root)
            _json_print({"status": "initialized", "config": str(config)})
            return 0
        if args.command == "init-autonomous":
            config = initialize_autonomous_root(args.root)
            _json_print({"status": "initialized", "config": str(config), "protocol": "AXQ-FILE/2"})
            return 0
        if args.command == "configure-ax-worker":
            config = write_launcher_config(
                args.root,
                ide_executable=args.ide_executable,
                ide_executable_sha256=args.ide_sha256,
                script_engine=args.script_engine,
                script_engine_sha256=args.script_engine_sha256,
                ide_profile=args.profile,
                worker_script=args.worker_script,
                worker_script_sha256=args.worker_script_sha256,
                transport_profile=args.transport_profile,
            )
            _json_print({"status": "configured", "config": str(config), "ide_launched": False})
            return 0
        if args.command == "setup-autonomous":
            return _setup_autonomous(args)
        if args.command == "configure-ax-deployment-worker":
            return _configure_deployment_worker(args)
    except (OSError, ProtocolViolation) as exc:
        print(f"axmcp-offline: {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


def serve_main() -> None:
    run_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
