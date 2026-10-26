"""Offline public-host regressions for the final deployment deadline review."""

from contextlib import nullcontext
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from axmcp_offline.protocol_v2 import load_v2
from axmcp_offline.security import (
    ProtocolViolation,
    canonical_json_bytes,
    sha256_file,
    strict_json_loads_v2,
)
from tests.ax.offline_foundation.test_autonomous_host import (
    ENVIRONMENT_DIGEST,
    TARGET_FINGERPRINT,
    _activate_root,
    _profile,
    _result,
)


class _OfflineBuildSupervisor:
    supports_workflow_deadline_v1 = True

    def launcher_fence(self):
        return nullcontext()

    def ensure_running(self, workspace_id: str, job_id: str) -> dict[str, str]:
        return {"workspace_id": workspace_id, "job_id": job_id}

    def wait_for_idle_shutdown(self, workspace_id: str) -> None:
        pass

    def request_shutdown(self, workspace_id: str) -> None:
        pass

    def _latest_workspace_launch(self, workspace_id: str):
        return None

    def _live_lease(self, workspace_id: str):
        return None


class FinalRepairTests(unittest.TestCase):
    def _prepared(self, root: Path, build_profile: str):
        host = _activate_root(root)
        host.supervisor = _OfflineBuildSupervisor()
        host.deployment_supervisor.supports_workflow_deadline_v1 = True
        baseline = host.v2 / "baselines" / "offline.project"
        baseline.write_bytes(b"OFFLINE-BUILD-FIXTURE\n")
        created = host.workspace_create(
            "baselines/offline.project", sha256_file(baseline), ENVIRONMENT_DIGEST
        )
        workspace_id = created["workspace_id"]
        deadline = (
            datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=500)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        kwargs = (
            {"transport_profile": "diacloud", "workflow_deadline_at": deadline}
            if build_profile == "diacloud"
            else {}
        )
        host.project_build(workspace_id, 0, **kwargs)
        request, request_raw = host.queue.claim_next()
        input_descriptor = next(
            item for item in request["artifacts"] if item["path"].startswith("inputs/")
        )
        build_input = strict_json_loads_v2((host.v2 / input_descriptor["path"]).read_bytes())
        project_digest = sha256_file(host.v2 / "workspaces" / workspace_id / "project.project")
        build_result = {
            "schema_version": "1",
            "record_type": "BuildResult",
            "build_id": request["job_id"],
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "workspace_id": workspace_id,
            "revision": 0,
            "kind": "rebuild",
            "status": "passed",
            "error_count": 0,
            "warning_count": 0,
            "diagnostics": [],
            "project_digest": project_digest,
            "artifact_digest": None,
            "runtime_release_digest": build_input["runtime_release_digest"],
            "environment_digest": ENVIRONMENT_DIGEST,
        }
        output = host.v2 / "outputs" / f"{request['job_id']}.build-result.json"
        output.write_bytes(canonical_json_bytes(build_result))
        host.ingest_worker_result(
            _result(
                request,
                request_raw,
                before_digest=project_digest,
                after_digest=project_digest,
                artifacts=[
                    {"role": "output", "path": f"outputs/{output.name}", "sha256": sha256_file(output)}
                ],
            )
        )
        artifact = host.artifact_freeze(workspace_id, 0)
        plan = host.deployment_prepare(
            artifact["artifact_digest"], TARGET_FINGERPRINT,
            artifact["artifact_digest"], _profile(),
        )
        execute_args = (
            plan["plan_id"], plan["artifact_digest"], plan["target_fingerprint"],
            plan["rollback_digest"], plan["expires_at"],
            plan["challenge_nonce"], "yükle",
        )
        return host, plan, execute_args, deadline

    def test_diacloud_execution_reuses_exact_qualifying_build_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host, _plan, execute_args, deadline = self._prepared(Path(tmp), "diacloud")
            queued = host.deployment_execute(
                *execute_args, transport_profile="diacloud", workflow_deadline_at=deadline
            )
            self.assertEqual(queued["workflow_deadline_at"], deadline)
            request, _ = load_v2(next((host.v2 / "inbox").glob("*.request.json")))
            self.assertEqual(request["deadline_at"], deadline)

    def test_build_transport_profile_cannot_be_changed_at_execution(self) -> None:
        for build_profile, execute_profile in (
            ("diacloud", "lan"),
            ("diacloud", "default"),
            ("lan", "diacloud"),
        ):
            with self.subTest(build=build_profile, execute=execute_profile), tempfile.TemporaryDirectory() as tmp:
                host, plan, execute_args, _ = self._prepared(Path(tmp), build_profile)
                if execute_profile == "default":
                    kwargs = {}
                else:
                    kwargs = {
                        "transport_profile": execute_profile,
                        "workflow_deadline_at": (
                            datetime.now(UTC) + timedelta(seconds=300)
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                with self.assertRaises(ProtocolViolation):
                    host.deployment_execute(*execute_args, **kwargs)
                self.assertFalse((host.v2 / "plans" / f"{plan['plan_id']}.consumed.json").exists())
                self.assertFalse(any((host.v2 / "inbox").glob("*.request.json")))
                self.assertEqual(host.deployment_supervisor.calls, [])

    def test_legacy_lan_build_and_execute_remain_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host, _plan, execute_args, _ = self._prepared(Path(tmp), "lan")
            queued = host.deployment_execute(*execute_args)
            self.assertEqual(queued["operation"], "deployment.execute")
            request, _ = load_v2(next((host.v2 / "inbox").glob("*.request.json")))
            self.assertNotIn("transport_profile", request)

    def test_recovery_input_cannot_masquerade_as_another_profile_or_deadline(self) -> None:
        for profile in ("lan", "diacloud"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                host, _plan, execute_args, build_deadline = self._prepared(Path(tmp), profile)
                deadline = (
                    build_deadline if profile == "diacloud"
                    else (datetime.now(UTC) + timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ")
                )
                host.deployment_execute(
                    *execute_args, transport_profile=profile, workflow_deadline_at=deadline
                )
                request, _ = load_v2(next((host.v2 / "inbox").glob("*.request.json")))
                descriptor = next(
                    item for item in request["artifacts"]
                    if item["role"] == "input" and item["path"].startswith("inputs/")
                )
                path = host.v2 / descriptor["path"]
                original = strict_json_loads_v2(path.read_bytes())
                forged_inputs = [
                    {**original, "transport_profile": "lan" if profile == "diacloud" else "diacloud"},
                    {**original, "workflow_deadline_at": (
                        datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                        - timedelta(seconds=1)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")},
                ]
                if profile == "diacloud":
                    forged_inputs.append({
                        key: value for key, value in original.items()
                        if key not in {"transport_profile", "workflow_deadline_at"}
                    })
                for forged in forged_inputs:
                    forged["schema_version"] = (
                        "1" if "transport_profile" not in forged else "2"
                    )
                    path.write_bytes(canonical_json_bytes(forged))
                    with self.assertRaises(ProtocolViolation):
                        host.queue._deployment_execution_input(request)

    def test_diacloud_execution_rejects_changed_extended_or_expired_build_budget(self) -> None:
        for variant in ("changed", "extended", "expired"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                host, plan, execute_args, deadline = self._prepared(Path(tmp), "diacloud")
                build_deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                candidate = (
                    build_deadline + timedelta(seconds=30)
                    if variant == "extended"
                    else build_deadline - timedelta(seconds=30)
                    if variant == "changed"
                    else build_deadline
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                if variant == "expired":
                    with patch("axmcp_offline.autonomous.datetime") as clock:
                        clock.now.return_value = build_deadline + timedelta(seconds=1)
                        with self.assertRaises(ProtocolViolation):
                            host.deployment_execute(
                                *execute_args, transport_profile="diacloud",
                                workflow_deadline_at=candidate,
                            )
                else:
                    with self.assertRaises(ProtocolViolation):
                        host.deployment_execute(
                            *execute_args, transport_profile="diacloud",
                            workflow_deadline_at=candidate,
                        )
                self.assertFalse((host.v2 / "plans" / f"{plan['plan_id']}.consumed.json").exists())
                self.assertFalse(any((host.v2 / "inbox").glob("*.request.json")))
                self.assertEqual(host.deployment_supervisor.calls, [])

    def test_bounded_lan_and_diacloud_recovery_is_unknown_without_replay(self) -> None:
        for profile in ("lan", "diacloud"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                host, plan, execute_args, build_deadline = self._prepared(Path(tmp), profile)
                deadline = (
                    build_deadline if profile == "diacloud"
                    else (datetime.now(UTC) + timedelta(seconds=300)).strftime("%Y-%m-%dT%H:%M:%SZ")
                )
                queued = host.deployment_execute(
                    *execute_args, transport_profile=profile, workflow_deadline_at=deadline
                )
                request, _ = host.queue.claim_next()
                host.queue.mark_side_effect_started(
                    request["job_id"], request["job_id"], ENVIRONMENT_DIGEST
                )
                recovered = host.queue.recover()
                self.assertEqual(recovered["unknown_outcome"], 1)
                self.assertEqual(recovered["requeued"], 0)
                self.assertEqual(host.job_status(queued["job_id"])["status"], "unknown_outcome")
                self.assertIsNone(host.queue.claim_next())
                self.assertEqual(
                    len([c for c in host.deployment_supervisor.calls if c[1] == "deployment.execute"]),
                    1,
                )
                self.assertEqual(
                    len([
                        p for p in (host.v2 / "archive").glob("*.request.json")
                        if load_v2(p)[0]["operation"] == "deployment.execute"
                    ]),
                    1,
                )
                reconciled = host.deployment_reconcile(
                    queued["job_id"], TARGET_FINGERPRINT,
                    plan["artifact_digest"], plan["rollback_digest"],
                )
                self.assertEqual(reconciled["operation"], "deployment.reconcile")
                next_request, _ = host.queue.claim_next()
                self.assertEqual(next_request["operation"], "deployment.reconcile")


if __name__ == "__main__":
    unittest.main()
