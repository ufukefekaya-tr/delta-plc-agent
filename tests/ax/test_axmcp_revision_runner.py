"""Public-client orchestration checks; no vendor or target process is launched."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import os
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from axmcp_offline import revision_runner as runner
from axmcp_offline import ax_supervisor, window_guard
from axmcp_offline.jobs import WorkerLease


class RevisionRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_holds_workspace_and_launcher_fences_until_guard_stops(self):
        launches = self.root / "v2/state/launches"
        launches.mkdir(parents=True)
        (launches / "owned.json").write_text('{"worker_id":"owned"}')
        events = []
        @contextmanager
        def fence(name):
            events.append(name + "+")
            try:
                yield
            finally:
                events.append(name + "-")
        def request(_):
            self.assertEqual(events, ["workspace+", "launcher+"])
            events.append("request")
            return {"status": "requested"}
        def wait(_):
            self.assertEqual(events, ["workspace+", "launcher+", "request"])
            events.append("guard_stopped")
            return {"status": "stopped"}
        supervisor = SimpleNamespace(launcher_fence=lambda: fence("launcher"),
                                     request_shutdown=request, wait_for_idle_shutdown=wait)
        host = SimpleNamespace(_workspace_fence=lambda _: fence("workspace"))
        with patch.object(runner, "AxWorkerSupervisor", return_value=supervisor), \
             patch.object(runner, "AutonomousHost", return_value=host):
            result = await runner.close_workspace(self.root, "owned")
        self.assertEqual(events, ["workspace+", "launcher+", "request", "guard_stopped",
                                  "launcher-", "workspace-"])
        self.assertEqual(result["guard"]["status"], "stopped")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.environment = "sha256:" + "a" * 64
        self.baseline = "sha256:" + "b" * 64
        (self.root / "autonomous-config.json").write_text(json.dumps({
            "target_allowlist": [], "environment_digest": self.environment,
        }))

    def request(self):
        return {"environment_digest": self.environment,
                "baseline_sha256": self.baseline, "workspace_id": "fixture",
                "expected_revision": 0, "idempotency_key": "test-request",
                "st_changes": []}

    def candidate(self):
        native = self.root / "v2/native_candidates/fixture.project"
        native.parent.mkdir(parents=True)
        native.write_bytes(b"saved native fixture")
        return native, {"path": "native_candidates/fixture.project",
                        "project_digest": runner.digest(native),
                        "status": "NATIVE_SAVED_UNCOMPILED", "deployable": False}

    async def test_offline_allowlist_rejects_before_client(self):
        client = AsyncMock()
        with self.assertRaises(ValueError):
            await runner.call(client, "deployment.start", {})
        client.call_tool.assert_not_called()
        params = runner.client_parameters(self.root)
        self.assertEqual(params.env["AXMCP_AUTOSTART_DEPLOYMENT_WORKER"], "0")
        (self.root / "autonomous-config.json").write_text(
            '{"target_allowlist":[{"fingerprint":"anything"}]}')
        with self.assertRaises(ValueError):
            runner.offline_root(self.root)

    async def test_bad_timestamp_or_environment_never_submits(self):
        for extra in ({"request_received_unix": True},
                      {"request_received_unix": float("nan")},
                      {"request_received_unix": float("inf")},
                      {"request_received_unix": float("-inf")},
                      {"environment_digest": "wrong"}):
            with self.subTest(extra=extra), patch.object(runner, "call", AsyncMock()) as call:
                with self.assertRaises(ValueError):
                    await runner.revision(object(), self.root, {**self.request(), **extra})
                call.assert_not_called()

    async def test_verified_candidate_is_returned_without_build(self):
        native, candidate = self.candidate()
        calls = []

        async def call(_client, operation, arguments):
            calls.append(operation)
            return {"workspace.status": {"baseline_project_digest": self.baseline, "revision": 0},
                    "revision.apply": {"job_id": "one"},
                    "candidate.snapshot": candidate}[operation]

        with patch.object(runner, "call", call), \
             patch.object(runner, "terminal", AsyncMock(return_value={"status": "succeeded"})), \
             patch.object(runner, "emit"):
            result = await runner.revision(object(), self.root, self.request())
        self.assertEqual(Path(result["native_path"]), native)
        self.assertEqual(result["request_timing_status"], "NOT_MEASURED")
        self.assertIsNone(result["request_to_native_seconds"])
        self.assertEqual(calls, ["workspace.status", "revision.apply", "candidate.snapshot"])

    async def test_observed_request_time_includes_prepared_work_without_fabricating_it(self):
        _native, candidate = self.candidate()
        async def call(_client, operation, arguments):
            return {"workspace.status": {"baseline_project_digest": self.baseline, "revision": 0},
                    "revision.apply": {"job_id": "one"},
                    "candidate.snapshot": candidate}[operation]
        with patch.object(runner, "call", call), \
             patch.object(runner, "terminal", AsyncMock(return_value={"status": "succeeded"})), \
             patch.object(runner, "emit"), \
             patch.object(runner.time, "time", return_value=1000):
            result = await runner.revision(
                object(), self.root, {**self.request(), "request_received_unix": 900})
        self.assertEqual(result["request_timing_status"], "OBSERVED")
        self.assertEqual(result["request_to_native_seconds"], 100)
        self.assertLess(result["tool_seconds"], result["request_to_native_seconds"])

    async def test_candidate_escape_or_bad_status_is_not_delivered(self):
        _native, candidate = self.candidate()
        for extra in ({"path": "../outside.project"}, {"project_digest": self.baseline},
                      {"status": "COMPILED"}, {"deployable": True}):
            async def call(_client, operation, arguments):
                return {"workspace.status": {"baseline_project_digest": self.baseline, "revision": 0},
                        "revision.apply": {"job_id": "one"},
                        "candidate.snapshot": {**candidate, **extra}}[operation]
            with self.subTest(extra=extra), patch.object(runner, "call", call), \
                 patch.object(runner, "terminal", AsyncMock(return_value={"status": "succeeded"})):
                with self.assertRaises(RuntimeError):
                    await runner.revision(object(), self.root, self.request())

    async def test_replay_uses_original_revision_and_emits_binding_before_enqueue(self):
        _native, candidate = self.candidate()
        events = []
        async def call(_client, operation, arguments):
            events.append((operation, arguments))
            return {"workspace.status": {"baseline_project_digest": self.baseline, "revision": 1},
                    "revision.apply": {"job_id": "original"},
                    "candidate.snapshot": candidate}[operation]
        with patch.object(runner, "call", call), \
             patch.object(runner, "terminal", AsyncMock(return_value={"status": "succeeded"})), \
             patch.object(runner, "emit", lambda event, **values: events.append((event, values))):
            await runner.revision(object(), self.root, self.request())
        bound = next(v for event, v in events if event == "revision_bound")
        submitted = next(v for event, v in events if event == "revision.apply")
        self.assertEqual(bound["expected_revision"], 0)
        self.assertEqual(submitted["expected_revision"], 0)
        self.assertEqual(bound["workspace_id"], submitted["workspace_id"])
        self.assertEqual(bound["idempotency_key"], submitted["idempotency_key"])
        self.assertLess([e for e, _ in events].index("revision_bound"),
                        [e for e, _ in events].index("revision.apply"))

    async def test_failed_unknown_and_timeout_never_resubmit(self):
        for state, expected in (("failed", RuntimeError), ("unknown_outcome", RuntimeError),
                                ("running", TimeoutError)):
            call = AsyncMock(return_value={"status": state})
            with self.subTest(state=state), patch.object(runner, "call", call), \
                 patch.object(runner, "emit"):
                with self.assertRaises(expected):
                    await runner.terminal(object(), {"job_id": "same-job"}, -1)
            self.assertEqual(call.await_count, 1)
            self.assertEqual(call.await_args.args[1:], ("job.status", {"job_id": "same-job"}))

    async def test_build_failure_preserves_native_and_closes_inside_client(self):
        native, candidate = self.candidate()
        request_path = self.root / "request.json"
        request_path.write_text(json.dumps(self.request()))
        args = SimpleNamespace(root=self.root, request=request_path, timeout=5, build="rebuild")
        client_context = AsyncMock()
        events = []
        async def close(*_args):
            events.append("close")
            return {"guard": {"status": "stopped"}}
        async def exit_client(*_args):
            events.append("client_exit")
        client_context.__aexit__.side_effect = exit_client
        saved = {"candidate": candidate, "native_path": str(native),
                 "workspace_id": "fixture", "revision": 1}
        with patch.object(runner, "Client", return_value=client_context), \
             patch.object(runner, "revision", AsyncMock(return_value=saved)), \
             patch.object(runner, "call", AsyncMock(side_effect=RuntimeError("build failed"))), \
             patch.object(runner, "close_workspace", close):
            result = await runner.run(args)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["candidate"], candidate)
        self.assertEqual(result["failure"], "build failed")
        self.assertEqual(events, ["close", "client_exit"])

    @unittest.skipUnless(os.name == "nt", "Windows process liveness contract")
    async def test_windows_probes_do_not_terminate_owned_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with patch.object(os, "kill", side_effect=AssertionError("not a Windows probe")):
                for _ in range(3):
                    self.assertTrue(ax_supervisor._pid_alive(process.pid))
                    self.assertTrue(WorkerLease._default_pid_alive(process.pid))
                    self.assertIsNone(process.poll())
            process.terminate()
            process.wait(timeout=5)
            self.assertFalse(ax_supervisor._pid_alive(process.pid))
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows unknown liveness contract")
    async def test_windows_unknown_liveness_is_not_dead(self):
        with patch.object(ax_supervisor, "_read_only_pid_alive",
                          side_effect=PermissionError("access denied")):
            with self.assertRaises(PermissionError):
                ax_supervisor._pid_alive(1234)
        with patch.object(window_guard, "_pid_alive", side_effect=OSError("WAIT_FAILED")):
            with self.assertRaises(OSError):
                WorkerLease._default_pid_alive(1234)


if __name__ == "__main__":
    unittest.main()
