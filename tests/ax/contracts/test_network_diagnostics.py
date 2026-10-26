from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from axmcp_offline.network_diagnostics import (
    collect_local_windows_snapshot,
    evaluate_network_snapshot,
)

from tests.ax.offline_foundation import PACKAGE_ROOT, SRC_ROOT


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
TARGET = "10.50.1.5"


def _profile() -> dict:
    return {
        "expected_adapter_id": "dia-tunnel-1",
        "expected_tunnel_id": "T-42",
        "expected_cloud_account_id": "account-7",
        "machine_lan_adapter_id": "machine-lan-1",
        "machine_subnet": "10.50.1.0/24",
        "expected_gateway_executable": "C:\\Tools\\GatewayService.exe",
        "expected_gateway_version": "3.5.18.0",
    }


def _snapshot() -> dict:
    return {
        "observed_at": "2026-10-11T11:59:30Z",
        "route_epoch": "epoch-2",
        "adapters": [
            {"id": "dia-tunnel-1", "kind": "tunnel", "up": True, "interface_metric": 5,
             "addresses": [{"ip": "10.50.1.2", "prefix_length": 24},
                           {"ip": "10.50.1.3", "prefix_length": 24}]},
            {"id": "machine-lan-1", "kind": "physical", "up": False,
             "addresses": [{"ip": "10.50.1.100", "prefix_length": 24}]},
            {"id": "wifi-1", "kind": "physical", "up": True, "interface_metric": 15,
             "addresses": [{"ip": "10.50.8.9", "prefix_length": 24}]},
        ],
        "routes": [
            {"destination": "10.50.1.0/24", "adapter_id": "dia-tunnel-1", "metric": 1},
            {"destination": "0.0.0.0/0", "adapter_id": "wifi-1", "metric": 10},
        ],
        "selected_path": {"adapter_id": "dia-tunnel-1", "source_ip": "10.50.1.3"},
        "tunnel": {"id": "T-42", "account_id": "account-7"},
        "cloud_account_id": "account-7",
        "gateway": {"pid": 104, "executable": "C:\\Tools\\GatewayService.exe", "version": "3.5.18.0"},
        "ide": {"pid": 205, "executable": "C:\\Tools\\DIADesigner-AX.exe"},
        "application_login": "logged_in",
        "ua_watch": "active",
        "cloud_status": "online",
        "endpoint_status": "reachable",
    }


def _assessment(snapshot: dict, profile: dict | None = None, transport: str = "diacloud") -> dict:
    return evaluate_network_snapshot(
        transport=transport, target_ip=TARGET, profile=profile or _profile(),
        snapshot=snapshot, now=NOW,
    )


def _check(result: dict, name: str) -> dict:
    return next(item for item in result["checks"] if item["check"] == name)


class NetworkDiagnosticsTests(unittest.TestCase):
    def test_selected_source_is_os_chosen_source_not_first_adapter_address(self) -> None:
        result = _assessment(_snapshot())
        self.assertEqual(result["status"], "consistent")
        self.assertEqual(result["selected_route"]["source_ip"], "10.50.1.3")
        self.assertEqual(result["selected_route"]["adapter_id"], "dia-tunnel-1")
        self.assertFalse(result["network_ready"])
        self.assertFalse(result["target_contacted"])
        self.assertEqual(result["assessment"], "offline_snapshot_only")

    def test_wrong_route_is_blocked_even_when_expected_tunnel_adapter_exists(self) -> None:
        snapshot = _snapshot()
        snapshot["routes"][0]["adapter_id"] = "wifi-1"
        snapshot["selected_path"] = {"adapter_id": "wifi-1", "source_ip": "10.50.8.9"}
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "expected_adapter_identity")["status"], "blocked")
        self.assertEqual(_check(result, "transport_path")["code"], "selected_non_tunnel_adapter")
        self.assertEqual(result["status"], "blocked")

    def test_selected_source_not_on_adapter_is_never_passed(self) -> None:
        snapshot = _snapshot()
        snapshot["selected_path"]["source_ip"] = "10.50.1.99"
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "selected_route")["code"], "source_not_on_selected_adapter")
        self.assertEqual(result["status"], "blocked")

    def test_upstream_overlap_and_attached_machine_cable_are_separate_blocks(self) -> None:
        snapshot = _snapshot()
        snapshot["adapters"][1]["up"] = True
        snapshot["adapters"][2]["addresses"][0]["ip"] = "10.50.1.9"
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "upstream_subnet_overlap")["status"], "blocked")
        self.assertEqual(_check(result, "remote_cable_isolation")["status"], "blocked")

    def test_lan_path_requires_direct_physical_selected_source(self) -> None:
        snapshot = _snapshot()
        snapshot["adapters"][1]["up"] = True
        snapshot["routes"][0]["adapter_id"] = "machine-lan-1"
        snapshot["selected_path"] = {"adapter_id": "machine-lan-1", "source_ip": "10.50.1.100"}
        profile = _profile()
        profile["expected_adapter_id"] = "machine-lan-1"
        result = _assessment(snapshot, profile, "lan")
        self.assertEqual(_check(result, "transport_path")["status"], "consistent")
        self.assertEqual(result["status"], "consistent")
        remote = _assessment(snapshot)
        self.assertEqual(_check(remote, "transport_path")["status"], "blocked")

    def test_changing_route_epoch_is_inconclusive_even_with_current_path(self) -> None:
        snapshot = _snapshot()
        snapshot["previous_route_epoch"] = "epoch-1"
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "route_epoch")["code"], "route_changed_since_previous_sample")
        self.assertEqual(result["status"], "inconclusive")

    def test_cloud_online_can_coexist_with_endpoint_failure(self) -> None:
        snapshot = _snapshot()
        snapshot["endpoint_status"] = "unreachable"
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "cloud_vs_endpoint")["code"], "cloud_online_endpoint_unreachable")
        self.assertEqual(result["status"], "blocked")

    def test_cloud_offline_endpoint_success_is_conflict_not_universal_failure(self) -> None:
        snapshot = _snapshot()
        snapshot["cloud_status"] = "offline"
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "cloud_vs_endpoint")["code"], "cloud_offline_endpoint_reachable_conflict")
        self.assertEqual(result["status"], "inconclusive")

    def test_portal_only_and_missing_identity_never_pass(self) -> None:
        snapshot = _snapshot()
        snapshot.pop("endpoint_status")
        snapshot.pop("tunnel")
        snapshot.pop("application_login")
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "cloud_vs_endpoint")["status"], "inconclusive")
        self.assertEqual(_check(result, "tunnel_identity")["status"], "inconclusive")
        self.assertEqual(_check(result, "cloud_account_binding")["status"], "inconclusive")
        self.assertEqual(_check(result, "application_login")["status"], "inconclusive")
        self.assertEqual(result["status"], "inconclusive")

    def test_missing_expected_adapter_cannot_inherit_snapshot_identity(self) -> None:
        profile = _profile()
        profile.pop("expected_adapter_id")
        result = _assessment(_snapshot(), profile)
        self.assertEqual(_check(result, "expected_adapter_identity")["status"], "inconclusive")
        self.assertEqual(result["status"], "inconclusive")

    def test_missing_account_or_tunnel_ownership_never_proves_binding(self) -> None:
        snapshot = _snapshot()
        snapshot.pop("cloud_account_id")
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "tunnel_identity")["status"], "consistent")
        self.assertEqual(_check(result, "cloud_account_binding")["status"], "inconclusive")
        self.assertEqual(result["status"], "inconclusive")
        snapshot["cloud_account_id"] = "other-account"
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "cloud_account_binding")["status"], "blocked")

    def test_stale_gateway_and_application_evidence_remain_separate(self) -> None:
        snapshot = _snapshot()
        snapshot["observed_at"] = "2026-10-10T11:00:00Z"
        snapshot.pop("gateway")
        snapshot.pop("ide")
        snapshot.pop("application_login")
        snapshot.pop("ua_watch")
        result = _assessment(snapshot)
        for name in ("snapshot_freshness", "gateway_identity", "ide_process", "application_login", "ua_watch"):
            self.assertEqual(_check(result, name)["status"], "inconclusive")
        self.assertEqual(result["status"], "inconclusive")

    def test_gateway_version_mismatch_is_diagnostic_without_causal_claim(self) -> None:
        snapshot = _snapshot()
        snapshot["gateway"]["version"] = "4.0.0.0"
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "gateway_identity")["code"], "gateway_executable_path_or_version_mismatch")
        self.assertNotIn("cause", json.dumps(result).lower())

    def test_same_gateway_name_and_version_in_wrong_install_is_blocked(self) -> None:
        snapshot = _snapshot()
        snapshot["gateway"]["executable"] = "D:\\OtherInstall\\GatewayService.exe"
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "gateway_identity")["code"], "gateway_executable_path_or_version_mismatch")
        self.assertEqual(result["status"], "blocked")

    def test_relative_gateway_expectation_is_inconclusive(self) -> None:
        profile = _profile()
        profile["expected_gateway_executable"] = "GatewayService.exe"
        result = _assessment(_snapshot(), profile)
        self.assertEqual(_check(result, "gateway_identity")["code"], "expected_gateway_absolute_identity_missing")
        self.assertEqual(result["status"], "inconclusive")

    def test_multiple_gateway_candidates_are_not_arbitrarily_selected(self) -> None:
        snapshot = _snapshot()
        snapshot["gateway_candidates"] = [
            snapshot["gateway"],
            {"pid": 105, "executable": "D:\\OtherInstall\\GatewayService.exe", "version": "3.5.18.0"},
        ]
        result = _assessment(snapshot)
        self.assertEqual(_check(result, "gateway_identity")["code"], "multiple_gateway_processes")
        self.assertIsNone(result["gateway_process"]["pid"])
        self.assertEqual(result["gateway_process"]["candidate_count"], 2)
        self.assertEqual(result["status"], "inconclusive")

    def test_secret_fields_are_not_reflected_into_result(self) -> None:
        snapshot = _snapshot()
        snapshot.update({"account": "person@example.com", "password": "Pä$$w0rd!", "token": "token-ABC"})
        result = _assessment(snapshot)
        encoded = json.dumps(result, ensure_ascii=False)
        for secret in ("person@example.com", "Pä$$w0rd!", "token-ABC"):
            self.assertNotIn(secret, encoded)
        self.assertTrue(result["normalized_digest"].startswith("sha256:"))

    def test_windows_collector_is_bounded_and_fully_injectable(self) -> None:
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "adapters": _snapshot()["adapters"],
                "routes": _snapshot()["routes"],
                "selected_path": _snapshot()["selected_path"],
                "processes": [
                    {"name": "GatewayService.exe", "pid": 104,
                     "executable": "C:\\Tools\\GatewayService.exe", "version": "3.5.18.0"},
                    {"name": "DIADesigner-AX.exe", "pid": 205,
                     "executable": "C:\\Tools\\DIADesigner-AX.exe", "version": "1.9"},
                ],
            }), "")

        collected = collect_local_windows_snapshot(
            TARGET, runner=fake_runner, now=NOW, platform_name="nt"
        )
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command[:3], ["powershell", "-NoProfile", "-NonInteractive"])
        self.assertIn("Find-NetRoute", command[4])
        self.assertIn("Get-NetAdapter", command[4])
        self.assertIn("Get-CimInstance Win32_Process", command[4])
        self.assertNotIn("Test-NetConnection", command[4])
        self.assertNotIn("CommandLine", command[4])
        self.assertEqual(kwargs["timeout"], 15)
        self.assertEqual(collected["selected_path"]["source_ip"], "10.50.1.3")
        self.assertNotIn("application_login", collected)
        self.assertNotIn("endpoint_status", collected)
        self.assertEqual(collected["gateway"]["pid"], 104)
        self.assertEqual(len(collected["gateway_candidates"]), 1)

    def test_collector_preserves_multiple_gateway_processes(self) -> None:
        def fake_runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, json.dumps({
                "adapters": [], "routes": [], "selected_path": None,
                "processes": [
                    {"name": "GatewayService.exe", "pid": 104,
                     "executable": "C:\\Tools\\GatewayService.exe", "version": "3.5.18.0"},
                    {"name": "GatewayService.exe", "pid": 105,
                     "executable": "D:\\OtherInstall\\GatewayService.exe", "version": "3.5.18.0"},
                ],
            }), "")

        collected = collect_local_windows_snapshot(
            TARGET, runner=fake_runner, now=NOW, platform_name="nt"
        )
        self.assertIsNone(collected["gateway"])
        self.assertEqual(len(collected["gateway_candidates"]), 2)

    def test_collector_timeout_is_sanitized_and_bounded(self) -> None:
        def timed_out(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with self.assertRaisesRegex(ValueError, "^local Windows inventory timed out$"):
            collect_local_windows_snapshot(
                TARGET, runner=timed_out, now=NOW, platform_name="nt"
            )

    def test_cli_uses_supplied_snapshot_and_preserves_package_ready_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "profile.json"
            snapshot_path = Path(tmp) / "snapshot.json"
            profile_path.write_text(json.dumps(_profile()), encoding="utf-8")
            snapshot = _snapshot()
            snapshot["observed_at"] = datetime.now(timezone.utc).isoformat()
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SRC_ROOT)
            env["AXMCP_MODE"] = "recorded_replay"
            completed = subprocess.run(
                [sys.executable, "-m", "axmcp_offline", "doctor",
                 "--transport", "diacloud", "--target-ip", TARGET,
                 "--network-profile", str(profile_path), "--network-snapshot", str(snapshot_path)],
                cwd=PACKAGE_ROOT, env=env, capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertTrue(result["ready"])
            self.assertEqual(result["readiness_scope"], "package_backend_only")
            self.assertFalse(result["live_ax_verified"])
            self.assertEqual(result["network_diagnostics"]["snapshot_origin"], "supplied")
            self.assertFalse(result["network_diagnostics"]["network_ready"])

            snapshot["endpoint_status"] = "unreachable"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            blocked = subprocess.run(
                [sys.executable, "-m", "axmcp_offline", "doctor",
                 "--transport", "diacloud", "--target-ip", TARGET,
                 "--network-profile", str(profile_path), "--network-snapshot", str(snapshot_path)],
                cwd=PACKAGE_ROOT, env=env, capture_output=True, text=True, timeout=20, check=False,
            )
            self.assertEqual(blocked.returncode, 2, blocked.stderr)
            blocked_result = json.loads(blocked.stdout)
            self.assertTrue(blocked_result["ready"])
            self.assertEqual(blocked_result["network_diagnostics"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
