from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from axmcp_offline import ax_supervisor
from axmcp_offline.autonomous import AutonomousHost, initialize_autonomous_root
from axmcp_offline.ax_supervisor import AxWorkerSupervisor, write_launcher_config
from axmcp_offline.security import (
    ProtocolViolation,
    canonical_json_bytes,
    sha256_file,
    utc_now,
)


ENVIRONMENT_DIGEST = "sha256:" + ("1" * 64)


@contextmanager
def _windows_guard_platform():
    # Replace module bindings only: changing global os.name breaks POSIX Path.
    windows_os = SimpleNamespace(**(vars(os) | {"name": "nt"}))
    windows_subprocess = SimpleNamespace(**(vars(subprocess) | {
        "STARTUPINFO": lambda: SimpleNamespace(dwFlags=0, wShowWindow=0),
        "STARTF_USESHOWWINDOW": 1,
        "SW_HIDE": 0,
    }))
    with patch.object(ax_supervisor, "os", windows_os), patch.object(
        ax_supervisor, "subprocess", windows_subprocess
    ):
        yield


class _Process:
    pid = 4242

    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class _UnterminableProcess(_Process):
    def terminate(self) -> None:
        raise RuntimeError("simulated termination failure")


class _PopenRecorder:
    def __init__(self, process: _Process | None = None) -> None:
        self.calls: list[tuple[str | list[str], dict[str, object]]] = []
        self.process = process or _Process()

    def __call__(self, command: str | list[str], **kwargs: object) -> _Process:
        self.calls.append((command, kwargs))
        return self.process


def _host(root: Path) -> AutonomousHost:
    initialize_autonomous_root(root)
    config_path = root / "autonomous-config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["environment_digest"] = ENVIRONMENT_DIGEST
    config_path.write_bytes(canonical_json_bytes(config))
    return AutonomousHost(root)


def _identity_files(root: Path) -> tuple[Path, Path, Path]:
    identity = root / "identity"
    identity.mkdir()
    executable = identity / "DIADesigner-AX.exe"
    engine = identity / "ScriptEngine3.dll"
    worker = identity / "worker.py"
    helper = identity / "ladder_import.py"
    executable.write_bytes(b"EXACT AX 1.9 EXECUTABLE")
    engine.write_bytes(b"EXACT SCRIPT ENGINE 4.0")
    worker.write_bytes(b"# bounded worker\n")
    helper.write_bytes(b"# bounded ladder helper\n")
    return executable, engine, worker


def _configure(root: Path, executable: Path, engine: Path, worker: Path) -> None:
    write_launcher_config(
        root,
        ide_executable=executable,
        ide_executable_sha256=sha256_file(executable),
        script_engine=engine,
        script_engine_sha256=sha256_file(engine),
        ide_profile="DIADesigner-AX 1.9",
        worker_script=worker,
        worker_script_sha256=sha256_file(worker),
    )


class AxWorkerSupervisorTests(unittest.TestCase):
    def test_artifact_handoff_waits_for_guard_stopped_record(self) -> None:
        with _windows_guard_platform(), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()
            status_paths: list[Path] = []

            def start_guard(process: object, _seconds: int, status: Path) -> int:
                status_paths.append(status)
                status.write_bytes(
                    canonical_json_bytes(
                        {
                            "schema_version": "1",
                            "record_type": "AxWindowGuard",
                            "pid": process.pid,
                            "status": "active",
                            "started_at": utc_now(),
                            "checked_at": utc_now(),
                            "windows_hidden": 1,
                            "error": None,
                        }
                    )
                )
                return 9001

            alive_calls = 0

            def stop_on_check(_pid: int) -> bool:
                nonlocal alive_calls
                alive_calls += 1
                if status_paths:
                    record = json.loads(status_paths[0].read_bytes())
                    record["status"] = "stopped"
                    status_paths[0].write_bytes(canonical_json_bytes(record))
                return False

            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=stop_on_check,
                window_guard=start_guard,
            )
            launch = supervisor.ensure_running(
                workspace["workspace_id"], str(uuid.uuid4())
            )
            self.assertEqual(launch["status"], "launched")
            stopped = supervisor.wait_for_idle_shutdown(workspace["workspace_id"])
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(alive_calls, 1)

    def test_window_guard_start_failure_terminates_untracked_ax_process(self) -> None:
        with _windows_guard_platform(), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()

            def fail_guard(_process: object, _seconds: int, _status: Path) -> None:
                raise RuntimeError("simulated guard initialization failure")

            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: True,
                window_guard=fail_guard,
            )
            host_with_supervisor = AutonomousHost(root, supervisor=supervisor)
            with self.assertRaisesRegex(RuntimeError, "guard initialization"):
                host_with_supervisor.source_upsert_st(
                    workspace["workspace_id"],
                    0,
                    "SupervisorProbe",
                    "program",
                    "PROGRAM SupervisorProbe",
                    "SupervisorProbe := SupervisorProbe;",
                )
            self.assertTrue(popen.process.terminated)
            launch_paths = list(
                (root / "v2" / "state" / "launches").glob("*.json")
            )
            self.assertEqual(len(launch_paths), 1)
            launch = json.loads(launch_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(
                launch["handoff_status"],
                "window_guard_start_failed_terminated",
            )
            self.assertEqual(launch["pid"], 4242)
            self.assertIn("guard initialization", launch["failure"])

    def test_guard_and_termination_failure_leave_unknown_durable_handoff(self) -> None:
        with _windows_guard_platform(), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder(_UnterminableProcess())

            def fail_guard(_process: object, _seconds: int, _status: Path) -> None:
                raise RuntimeError("simulated guard initialization failure")

            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda pid: pid == 4242,
                window_guard=fail_guard,
            )
            job_id = str(uuid.uuid4())
            with self.assertRaisesRegex(
                ProtocolViolation, "termination=simulated termination failure"
            ):
                supervisor.ensure_running(workspace["workspace_id"], job_id)
            launch_path = next(
                (root / "v2" / "state" / "launches").glob("*.json")
            )
            launch = json.loads(launch_path.read_text(encoding="utf-8"))
            self.assertEqual(
                launch["handoff_status"],
                "window_guard_start_failed_termination_unknown",
            )
            self.assertEqual(
                supervisor.recover_workspace_job(
                    workspace["workspace_id"], job_id, sha256_file(baseline)
                )["status"],
                "launching",
            )
            self.assertEqual(len(popen.calls), 1)

    def test_final_launch_record_failure_terminates_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()
            from axmcp_offline import ax_supervisor as supervisor_module

            original_replace = supervisor_module.atomic_write_replace
            failed_ready_publication = False

            def fail_ready_publication(path: Path, raw: bytes) -> None:
                nonlocal failed_ready_publication
                value = json.loads(raw)
                if (
                    value.get("record_type") == "AxWorkerLaunch"
                    and value.get("handoff_status") == "ready"
                    and not failed_ready_publication
                ):
                    failed_ready_publication = True
                    raise OSError("simulated ready publication failure")
                original_replace(path, raw)

            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: True,
                window_guard=lambda _process, _seconds, _status: 31337,
            )
            with patch.object(
                supervisor_module,
                "atomic_write_replace",
                side_effect=fail_ready_publication,
            ):
                with self.assertRaisesRegex(OSError, "ready publication failure"):
                    supervisor.ensure_running(
                        workspace["workspace_id"], str(uuid.uuid4())
                    )
            self.assertTrue(popen.process.terminated)
            launch_path = next(
                (root / "v2" / "state" / "launches").glob("*.json")
            )
            launch = json.loads(launch_path.read_text(encoding="utf-8"))
            self.assertEqual(
                launch["handoff_status"],
                "launch_record_handoff_failed_terminated",
            )

    def test_exact_identity_background_launch_and_live_lease_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()
            guarded: list[tuple[int, int, Path]] = []
            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: True,
                window_guard=lambda process, seconds, status_path: guarded.append(
                    (process.pid, seconds, status_path)
                ),
            )
            host_with_supervisor = AutonomousHost(root, supervisor=supervisor)
            queued = host_with_supervisor.source_upsert_st(
                workspace["workspace_id"],
                0,
                "SupervisorProbe",
                "program",
                "PROGRAM SupervisorProbe",
                "SupervisorProbe := SupervisorProbe;",
            )
            self.assertEqual(queued["worker"]["status"], "launched")
            self.assertEqual(len(popen.calls), 1)
            if os.name == "nt":
                self.assertEqual(len(guarded), 1)
                self.assertEqual(guarded[0][:2], (4242, 330))
                self.assertEqual(
                    guarded[0][2].name, f"{queued['job_id']}.window-guard.json"
                )
            command, kwargs = popen.calls[0]
            if os.name == "nt":
                self.assertIsInstance(command, str)
                self.assertTrue(command.startswith(f'"{executable.resolve()}" '))
                self.assertIn('--profile="DIADesigner-AX 1.9"', command)
                self.assertIn(f'--runscript="{worker.resolve()}"', command)
                self.assertTrue(command.endswith("--culture=en --textPrompts"))
                self.assertNotIn("--noUI", command)
                self.assertEqual(
                    kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE
                )
                self.assertEqual(kwargs["creationflags"], 0)
            else:
                self.assertEqual(command[0], str(executable.resolve()))
                self.assertIn('--profile="DIADesigner-AX 1.9"', command)
                self.assertIn(f'--runscript="{worker.resolve()}"', command)
                self.assertEqual(command[-3:], ["--culture=en", "--noUI", "--textPrompts"])
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(kwargs["env"]["AXMCP_WORKER_ROOT"], str((root / "v2").resolve()))
            worker_config = json.loads((root / "v2" / "worker-config.json").read_bytes())
            self.assertEqual(
                worker_config["workspace_project"],
                f"workspaces/{workspace['workspace_id']}/project.project",
            )
            self.assertEqual(worker_config["initial_project_sha256"], sha256_file(baseline))
            self.assertEqual(
                worker_config["ladder_helper_sha256"],
                sha256_file(worker.with_name("ladder_import.py")),
            )
            self.assertEqual(
                worker_config["allowed_operations"],
                [
                    "source.read",
                    "source.upsert.st",
                    "ladder.networks.apply",
                    "project.build",
                ],
            )
            self.assertNotIn("deployment.execute", worker_config["allowed_operations"])
            self.assertEqual(
                kwargs["env"]["AXMCP_LADDER_HELPER_SHA256"],
                worker_config["ladder_helper_sha256"],
            )

            lease = {
                "protocol": "AXQ-FILE/2",
                "worker_id": workspace["workspace_id"],
                "pid": 4242,
                "process_start_fingerprint": "sha256:" + ("2" * 64),
                "started_at": "2026-09-01T00:00:00Z",
                "heartbeat_at": "2026-09-01T00:00:01Z",
                "environment_digest": ENVIRONMENT_DIGEST,
                "sequence": 0,
            }
            lease_path = root / "v2" / "state" / "worker.lease.json"
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_bytes(canonical_json_bytes(lease))
            adopted_job_id = str(uuid.uuid4())
            adopted_correlation_id = str(uuid.uuid4())
            (root / "v2" / "running" / f"{adopted_correlation_id}.request.json").write_bytes(
                canonical_json_bytes(
                    {
                        "protocol": "AXQ-FILE/2",
                        "correlation_id": adopted_correlation_id,
                        "job_id": adopted_job_id,
                    }
                )
            )
            adopted = supervisor.ensure_running(workspace["workspace_id"], adopted_job_id)
            self.assertEqual(adopted["status"], "already_running")
            self.assertEqual(len(popen.calls), 1)

            with self.assertRaisesRegex(ProtocolViolation, "absent"):
                supervisor.ensure_running(workspace["workspace_id"], str(uuid.uuid4()))

    def test_live_worker_claims_published_inbox_job_during_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            lease = {
                "protocol": "AXQ-FILE/2",
                "worker_id": workspace["workspace_id"],
                "pid": 4242,
                "process_start_fingerprint": "sha256:" + ("2" * 64),
                "started_at": "2026-09-01T00:00:00Z",
                "heartbeat_at": "2026-09-01T00:00:01Z",
                "environment_digest": ENVIRONMENT_DIGEST,
                "sequence": 0,
            }
            lease_path = root / "v2" / "state" / "worker.lease.json"
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_bytes(canonical_json_bytes(lease))
            supervisor = AxWorkerSupervisor(
                root,
                popen=_PopenRecorder(),
                process_alive=lambda _pid: True,
                window_guard=lambda _process, _seconds, _status: 9001,
            )
            states = iter(("inbox", "running"))
            supervisor._job_queue_state = lambda _job_id: next(states)  # type: ignore[method-assign]

            adopted = supervisor.ensure_running(workspace["workspace_id"], str(uuid.uuid4()))

            self.assertEqual(adopted["status"], "already_running")

    def test_idle_exit_race_relaunches_for_still_inbox_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()
            first = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: False,
                window_guard=lambda _process, _seconds, _status: 9001,
            )
            first.ensure_running(workspace["workspace_id"], str(uuid.uuid4()))

            job_id = str(uuid.uuid4())
            correlation_id = str(uuid.uuid4())
            inbox = root / "v2" / "inbox" / f"{correlation_id}.request.json"
            inbox.write_bytes(
                canonical_json_bytes(
                    {
                        "protocol": "AXQ-FILE/2",
                        "correlation_id": correlation_id,
                        "job_id": job_id,
                    }
                )
            )
            lease = {
                "protocol": "AXQ-FILE/2",
                "worker_id": workspace["workspace_id"],
                "pid": 4242,
                "process_start_fingerprint": "sha256:" + ("2" * 64),
                "started_at": "2026-09-01T00:00:00Z",
                "heartbeat_at": "2026-09-01T00:00:01Z",
                "environment_digest": ENVIRONMENT_DIGEST,
                "sequence": 0,
            }
            lease_path = root / "v2" / "state" / "worker.lease.json"
            lease_path.write_bytes(canonical_json_bytes(lease))
            checks = iter((True, False))
            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: next(checks, False),
                window_guard=lambda _process, _seconds, _status: 9002,
            )

            relaunched = supervisor.ensure_running(workspace["workspace_id"], job_id)

            self.assertEqual(relaunched["status"], "launched")
            self.assertEqual(relaunched["job_id"], job_id)
            self.assertEqual(len(popen.calls), 2)

    def test_changed_identity_workspace_drift_and_foreign_lease_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            worker.write_bytes(b"tampered")
            with self.assertRaisesRegex(ProtocolViolation, "worker_script bytes changed"):
                AxWorkerSupervisor(root)

            worker.write_bytes(b"# bounded worker\n")
            supervisor = AxWorkerSupervisor(root, popen=_PopenRecorder())
            helper = worker.with_name("ladder_import.py")
            helper.write_bytes(b"tampered helper")
            with self.assertRaisesRegex(ProtocolViolation, "ladder_helper bytes changed"):
                AxWorkerSupervisor(root)

            helper.write_bytes(b"# bounded ladder helper\n")
            supervisor = AxWorkerSupervisor(root, popen=_PopenRecorder())
            project = root / "v2" / "workspaces" / workspace["workspace_id"] / "project.project"
            project.write_bytes(b"OUT-OF-PROTOCOL")
            with self.assertRaisesRegex(ProtocolViolation, "drifted"):
                supervisor.ensure_running(workspace["workspace_id"], str(uuid.uuid4()))

            project.write_bytes(b"MINIMAL")
            lease_path = root / "v2" / "state" / "worker.lease.json"
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease = {
                "protocol": "AXQ-FILE/2",
                "worker_id": str(uuid.uuid4()),
                "pid": 777,
                "process_start_fingerprint": "sha256:" + ("3" * 64),
                "started_at": "2026-09-01T00:00:00Z",
                "heartbeat_at": "2026-09-01T00:00:01Z",
                "environment_digest": ENVIRONMENT_DIGEST,
                "sequence": 0,
            }
            lease_path.write_bytes(canonical_json_bytes(lease))
            with self.assertRaisesRegex(ProtocolViolation, "another workspace"):
                supervisor.ensure_running(workspace["workspace_id"], str(uuid.uuid4()))

    def test_dead_worker_requeues_unchanged_running_workspace_job_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            queued = host.source_read(workspace["workspace_id"], 0, "PLC_PRG")
            self.assertIsNotNone(host.queue.claim_next())
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()
            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: False,
                window_guard=lambda _process, _seconds, _status: 9001,
            )

            recovered = supervisor.recover_workspace_job(
                workspace["workspace_id"], queued["job_id"], sha256_file(baseline)
            )

            self.assertEqual(recovered["status"], "launched")
            self.assertEqual(len(popen.calls), 1)
            self.assertIsNotNone(host.queue._matching_request(queued["job_id"], "inbox"))
            self.assertIsNone(host.queue._matching_request(queued["job_id"], "running"))

    @unittest.skipUnless(os.name == "nt", "hidden-window guard is Windows-only")
    def test_active_launch_guard_prevents_startup_gap_duplicate_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            queued = host.source_read(workspace["workspace_id"], 0, "PLC_PRG")
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()

            def active_guard(process: object, _seconds: int, status: Path) -> int:
                timestamp = utc_now()
                status.write_bytes(
                    canonical_json_bytes(
                        {
                            "schema_version": "1",
                            "record_type": "AxWindowGuard",
                            "pid": process.pid,
                            "status": "active",
                            "started_at": timestamp,
                            "checked_at": timestamp,
                            "windows_hidden": 1,
                            "error": None,
                        }
                    )
                )
                return 9001

            launching = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: False,
                window_guard=active_guard,
            )
            self.assertEqual(
                launching.ensure_running(workspace["workspace_id"], queued["job_id"])[
                    "status"
                ],
                "launched",
            )
            recovering = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: False,
                window_guard=active_guard,
            )

            recovered = recovering.recover_workspace_job(
                workspace["workspace_id"], queued["job_id"], sha256_file(baseline)
            )

            self.assertEqual(recovered["status"], "launching")
            self.assertEqual(len(popen.calls), 1)

    @unittest.skipUnless(os.name == "nt", "hidden-window guard is Windows-only")
    def test_stale_launch_guard_retries_once_then_terminalizes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            queued = host.source_read(workspace["workspace_id"], 0, "PLC_PRG")
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()

            def stale_guard(process: object, _seconds: int, status: Path) -> int:
                status.write_bytes(
                    canonical_json_bytes(
                        {
                            "schema_version": "1",
                            "record_type": "AxWindowGuard",
                            "pid": process.pid,
                            "status": "active",
                            "started_at": "2026-09-01T00:00:00Z",
                            "checked_at": "2026-09-01T00:00:00Z",
                            "windows_hidden": 1,
                            "error": None,
                        }
                    )
                )
                return 9001

            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: False,
                window_guard=stale_guard,
            )
            supervisor.ensure_running(workspace["workspace_id"], queued["job_id"])

            retried = supervisor.recover_workspace_job(
                workspace["workspace_id"], queued["job_id"], sha256_file(baseline)
            )
            exhausted = supervisor.recover_workspace_job(
                workspace["workspace_id"], queued["job_id"], sha256_file(baseline)
            )

            self.assertEqual(retried["status"], "launched")
            self.assertEqual(exhausted["status"], "blocked")
            self.assertEqual(len(popen.calls), 2)
            self.assertTrue(
                (
                    root
                    / "v2"
                    / "state"
                    / "launches"
                    / f"{queued['job_id']}.attempt-02.json"
                ).is_file()
            )
            self.assertIsNotNone(host.queue._matching_request(queued["job_id"], "running"))

    @unittest.skipUnless(os.name == "nt", "hidden-window guard is Windows-only")
    def test_stale_launch_guard_requires_confirmed_dead_ide_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            queued = host.source_read(workspace["workspace_id"], 0, "PLC_PRG")
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()

            def stale_guard(process: object, _seconds: int, status: Path) -> int:
                status.write_bytes(
                    canonical_json_bytes(
                        {
                            "schema_version": "1",
                            "record_type": "AxWindowGuard",
                            "pid": process.pid,
                            "status": "active",
                            "started_at": "2026-09-01T00:00:00Z",
                            "checked_at": "2026-09-01T00:00:00Z",
                            "windows_hidden": 1,
                            "error": None,
                        }
                    )
                )
                return 9001

            launching = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: True,
                window_guard=stale_guard,
            )
            launching.ensure_running(workspace["workspace_id"], queued["job_id"])

            recovered = launching.recover_workspace_job(
                workspace["workspace_id"], queued["job_id"], sha256_file(baseline)
            )
            self.assertEqual(recovered["status"], "launching")
            self.assertEqual(len(popen.calls), 1)

            def liveness_error(_pid: int) -> bool:
                raise OSError("process query denied")

            unknown = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=liveness_error,
                window_guard=stale_guard,
            )
            with self.assertRaisesRegex(
                ProtocolViolation, "process liveness is unknown"
            ):
                unknown.recover_workspace_job(
                    workspace["workspace_id"], queued["job_id"], sha256_file(baseline)
                )
            self.assertEqual(len(popen.calls), 1)

    def test_dead_worker_never_replays_changed_running_workspace_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            host = _host(root)
            baseline = host.v2 / "baselines" / "minimal.project"
            baseline.write_bytes(b"MINIMAL")
            workspace = host.workspace_create(
                "baselines/minimal.project", sha256_file(baseline), ENVIRONMENT_DIGEST
            )
            queued = host.source_upsert_st(
                workspace["workspace_id"],
                0,
                "CrashProbe",
                "program",
                "PROGRAM CrashProbe",
                "CrashProbe := CrashProbe;",
            )
            self.assertIsNotNone(host.queue.claim_next())
            project = (
                root
                / "v2"
                / "workspaces"
                / workspace["workspace_id"]
                / "project.project"
            )
            project.write_bytes(b"CHANGED-BEFORE-WORKER-DEATH")
            executable, engine, worker = _identity_files(root)
            _configure(root, executable, engine, worker)
            popen = _PopenRecorder()
            supervisor = AxWorkerSupervisor(
                root,
                popen=popen,
                process_alive=lambda _pid: False,
                window_guard=lambda _process, _seconds, _status: 9001,
            )

            recovered = supervisor.recover_workspace_job(
                workspace["workspace_id"], queued["job_id"], sha256_file(baseline)
            )

            self.assertEqual(recovered["status"], "unknown_outcome")
            self.assertEqual(recovered["actual_project_digest"], sha256_file(project))
            self.assertEqual(popen.calls, [])
            self.assertIsNotNone(host.queue._matching_request(queued["job_id"], "running"))

    def test_configuration_rejects_digest_and_profile_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _host(root)
            executable, engine, worker = _identity_files(root)
            with self.assertRaisesRegex(ProtocolViolation, "does not match"):
                write_launcher_config(
                    root,
                    ide_executable=executable,
                    ide_executable_sha256="sha256:" + ("0" * 64),
                    script_engine=engine,
                    script_engine_sha256=sha256_file(engine),
                    ide_profile="DIADesigner-AX 1.9",
                    worker_script=worker,
                    worker_script_sha256=sha256_file(worker),
                )
            with self.assertRaisesRegex(ProtocolViolation, "only the frozen"):
                write_launcher_config(
                    root,
                    ide_executable=executable,
                    ide_executable_sha256=sha256_file(executable),
                    script_engine=engine,
                    script_engine_sha256=sha256_file(engine),
                    ide_profile="DIADesigner-AX 2.0",
                    worker_script=worker,
                    worker_script_sha256=sha256_file(worker),
                )


if __name__ == "__main__":
    unittest.main()
