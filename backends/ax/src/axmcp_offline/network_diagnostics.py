"""Offline route and application evidence checks for an explicitly selected target.

Supplied snapshots are claims to inspect, not fresh target observations. The optional
Windows collector reads local OS metadata only; it never opens a target socket.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
from typing import Any, Callable


_IDENTITY = re.compile(r"^[A-Za-z0-9_.:{}-]{1,100}$")
_VERSION = re.compile(r"^[0-9A-Za-z_.+-]{1,60}$")
_MAX_SNAPSHOT_BYTES = 2_000_000


def _ipv4(value: object) -> ipaddress.IPv4Address:
    if not isinstance(value, str):
        raise ValueError("IPv4 address is required")
    try:
        return ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError("invalid IPv4 address") from exc


def _network(value: object) -> ipaddress.IPv4Network:
    if not isinstance(value, str):
        raise ValueError("IPv4 network is required")
    try:
        return ipaddress.IPv4Network(value, strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
        raise ValueError("invalid IPv4 network") from exc


def _identity(value: object) -> str | None:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        return None
    if any(secret in value.casefold() for secret in ("password", "secret", "token", "account")):
        return None
    return value


def _private_identity(value: object) -> str | None:
    """Validate an ID used only for comparison; never reflect its value."""
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


def _version(value: object) -> str | None:
    return value if isinstance(value, str) and _VERSION.fullmatch(value) else None


def _absolute_windows_path(value: object) -> str | None:
    if not isinstance(value, str) or "\x00" in value or not PureWindowsPath(value).is_absolute():
        return None
    return ntpath.normcase(ntpath.normpath(value))


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _check(name: str, status: str, code: str) -> dict[str, str]:
    return {"check": name, "status": status, "code": code}


def _adapter_addresses(adapter: dict[str, Any]) -> list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Network]]:
    result = []
    addresses = adapter.get("addresses")
    for entry in addresses if isinstance(addresses, list) else []:
        address = _mapping(entry)
        try:
            ip = _ipv4(address.get("ip"))
            prefix = address.get("prefix_length")
            if not isinstance(prefix, int) or isinstance(prefix, bool):
                continue
            if address.get("state", "Preferred") != "Preferred":
                continue
            result.append((ip, ipaddress.IPv4Network((ip, prefix), strict=False)))
        except ValueError:
            continue
    return result


def _selected_route(
    snapshot: dict[str, Any], target: ipaddress.IPv4Address
) -> tuple[dict[str, Any] | None, str]:
    """Cross-check the OS-selected path against the longest-prefix route table."""
    path = _mapping(snapshot.get("selected_path"))
    adapter_id = _identity(path.get("adapter_id"))
    try:
        source = _ipv4(path.get("source_ip"))
    except ValueError:
        source = None
    if adapter_id is None or source is None:
        return None, "selected_path_missing"
    adapters = {_identity(a.get("id")): a for a in _items(snapshot.get("adapters"))}
    adapter = adapters.get(adapter_id)
    if not adapter or adapter.get("up") is not True:
        return None, "selected_adapter_unavailable"
    if not any(ip == source for ip, _ in _adapter_addresses(adapter)):
        return None, "source_not_on_selected_adapter"

    matches: list[tuple[int, int, str, str]] = []
    for route in _items(snapshot.get("routes")):
        try:
            network = _network(route.get("destination"))
            metric = route.get("metric")
            route_adapter_id = _identity(route.get("adapter_id"))
            if not isinstance(metric, int) or isinstance(metric, bool) or metric < 0:
                continue
            if route_adapter_id not in adapters or adapters[route_adapter_id].get("up") is not True:
                continue
            if target in network:
                interface_metric = adapters[route_adapter_id].get("interface_metric", 0)
                if not isinstance(interface_metric, int) or interface_metric < 0:
                    continue
                matches.append((network.prefixlen, metric + interface_metric, route_adapter_id, str(network)))
        except ValueError:
            continue
    if not matches:
        return None, "route_missing"
    matches.sort(key=lambda item: (-item[0], item[1]))
    best = matches[0]
    tied = [item for item in matches if item[:2] == best[:2]]
    if len({item[2] for item in tied}) > 1:
        return None, "route_tie"
    if best[2] != adapter_id:
        return None, "selected_route_conflicts_with_table"
    return {
        "adapter_id": adapter_id,
        "source_ip": str(source),
        "destination_prefix": best[3],
        "effective_metric": best[1],
    }, "selected_route_confirmed"


def evaluate_network_snapshot(
    *,
    transport: str,
    target_ip: str,
    profile: dict[str, Any],
    snapshot: dict[str, Any],
    now: datetime | None = None,
    snapshot_origin: str = "supplied",
) -> dict[str, Any]:
    """Assess one supplied snapshot without network or OS reads.

    ``consistent`` means the supplied fields agree, never live target approval.
    Missing identity, age or layer evidence remains ``inconclusive``.
    """
    if transport not in {"lan", "diacloud"}:
        raise ValueError("transport must be lan or diacloud")
    target = _ipv4(target_ip)
    if not isinstance(profile, dict) or not isinstance(snapshot, dict):
        raise ValueError("profile and snapshot must be JSON objects")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    checks: list[dict[str, str]] = []
    observed = _time(snapshot.get("observed_at"))
    max_age = profile.get("max_snapshot_age_seconds", 300)
    if not isinstance(max_age, int) or isinstance(max_age, bool) or not 1 <= max_age <= 3600:
        raise ValueError("max_snapshot_age_seconds must be 1..3600")
    age = (now - observed).total_seconds() if observed else None
    if age is None or age < -30 or age > max_age:
        checks.append(_check("snapshot_freshness", "inconclusive", "missing_future_or_stale_timestamp"))
    else:
        checks.append(_check("snapshot_freshness", "consistent", "within_profile_age_limit"))

    route, route_code = _selected_route(snapshot, target)
    route_status = "consistent" if route else "blocked" if route_code in {
        "source_not_on_selected_adapter", "selected_route_conflicts_with_table"
    } else "inconclusive"
    checks.append(_check("selected_route", route_status, route_code))
    selected_adapter = None
    if route:
        selected_adapters = {_identity(a.get("id")): a for a in _items(snapshot.get("adapters"))}
        selected_adapter = selected_adapters.get(route["adapter_id"])
    if selected_adapter is None:
        checks.append(_check("transport_path", "inconclusive", "selected_adapter_kind_unknown"))
    elif transport == "diacloud":
        checks.append(_check(
            "transport_path", "consistent" if selected_adapter.get("kind") == "tunnel" else "blocked",
            "selected_tunnel_adapter" if selected_adapter.get("kind") == "tunnel" else "selected_non_tunnel_adapter",
        ))
    else:
        source = _ipv4(route["source_ip"])
        on_link = any(ip == source and target in subnet for ip, subnet in _adapter_addresses(selected_adapter))
        direct = selected_adapter.get("kind") == "physical" and on_link
        checks.append(_check(
            "transport_path", "consistent" if direct else "blocked",
            "selected_direct_lan_adapter" if direct else "selected_path_not_direct_lan",
        ))
    expected_adapter = _identity(profile.get("expected_adapter_id"))
    if not expected_adapter or not route:
        checks.append(_check("expected_adapter_identity", "inconclusive", "identity_or_route_missing"))
    elif route["adapter_id"] != expected_adapter:
        checks.append(_check("expected_adapter_identity", "blocked", "wrong_selected_adapter"))
    else:
        checks.append(_check("expected_adapter_identity", "consistent", "selected_adapter_matches"))

    epoch = _identity(snapshot.get("route_epoch"))
    previous_epoch = _identity(snapshot.get("previous_route_epoch"))
    if not epoch:
        checks.append(_check("route_epoch", "inconclusive", "route_epoch_missing"))
    elif previous_epoch and previous_epoch != epoch:
        checks.append(_check("route_epoch", "inconclusive", "route_changed_since_previous_sample"))
    else:
        checks.append(_check("route_epoch", "consistent", "route_epoch_recorded"))

    adapters = {_identity(a.get("id")): a for a in _items(snapshot.get("adapters"))}
    if transport == "diacloud":
        expected_tunnel = _identity(profile.get("expected_tunnel_id"))
        observed_tunnel = _identity(_mapping(snapshot.get("tunnel")).get("id"))
        if not expected_tunnel or not observed_tunnel:
            checks.append(_check("tunnel_identity", "inconclusive", "tunnel_identity_missing"))
        elif expected_tunnel != observed_tunnel:
            checks.append(_check("tunnel_identity", "blocked", "wrong_tunnel"))
        else:
            checks.append(_check("tunnel_identity", "consistent", "tunnel_matches"))
        expected_account = _private_identity(profile.get("expected_cloud_account_id"))
        observed_account = _private_identity(snapshot.get("cloud_account_id"))
        tunnel_account = _private_identity(_mapping(snapshot.get("tunnel")).get("account_id"))
        if not expected_account or not observed_account or not tunnel_account:
            checks.append(_check("cloud_account_binding", "inconclusive", "account_or_tunnel_owner_missing"))
        elif expected_account != observed_account or observed_account != tunnel_account:
            checks.append(_check("cloud_account_binding", "blocked", "account_tunnel_owner_mismatch"))
        else:
            checks.append(_check("cloud_account_binding", "consistent", "account_tunnel_owner_matches"))

        try:
            machine_network = _network(profile.get("machine_subnet"))
        except ValueError:
            machine_network = None
        if machine_network is None or target not in machine_network:
            checks.append(_check("upstream_subnet_overlap", "inconclusive", "machine_subnet_missing_or_target_outside"))
        else:
            overlap = any(
                adapter.get("up") is True
                and adapter.get("kind") != "tunnel"
                and any(subnet.overlaps(machine_network) for _, subnet in _adapter_addresses(adapter))
                for adapter in adapters.values()
            )
            checks.append(_check(
                "upstream_subnet_overlap", "blocked" if overlap else "consistent",
                "upstream_overlaps_machine_subnet" if overlap else "no_observed_overlap",
            ))
        physical_id = _identity(profile.get("machine_lan_adapter_id"))
        physical = adapters.get(physical_id)
        if physical is None:
            checks.append(_check("remote_cable_isolation", "inconclusive", "physical_lan_identity_or_state_missing"))
        elif physical.get("up") is True:
            checks.append(_check("remote_cable_isolation", "blocked", "machine_lan_adapter_connected"))
        elif physical.get("up") is False:
            checks.append(_check("remote_cable_isolation", "consistent", "machine_lan_adapter_disconnected"))
        else:
            checks.append(_check("remote_cable_isolation", "inconclusive", "physical_lan_state_missing"))

    candidates = _items(snapshot.get("gateway_candidates"))
    gateway = _mapping(snapshot.get("gateway")) if len(candidates) <= 1 else {}
    expected_exe = _absolute_windows_path(profile.get("expected_gateway_executable"))
    expected_version = _version(profile.get("expected_gateway_version"))
    gateway_exe = _absolute_windows_path(gateway.get("executable"))
    gateway_version = _version(gateway.get("version"))
    gateway_pid = gateway.get("pid")
    if len(candidates) > 1:
        checks.append(_check("gateway_identity", "inconclusive", "multiple_gateway_processes"))
    elif not expected_exe or not expected_version:
        checks.append(_check("gateway_identity", "inconclusive", "expected_gateway_absolute_identity_missing"))
    elif not gateway_exe or not gateway_version or not isinstance(gateway_pid, int) or isinstance(gateway_pid, bool) or gateway_pid <= 0:
        checks.append(_check("gateway_identity", "inconclusive", "gateway_process_metadata_missing"))
    elif gateway_exe != expected_exe or gateway_version != expected_version:
        checks.append(_check("gateway_identity", "blocked", "gateway_executable_path_or_version_mismatch"))
    else:
        checks.append(_check("gateway_identity", "consistent", "gateway_process_matches"))

    ide = _mapping(snapshot.get("ide"))
    ide_pid = ide.get("pid")
    if isinstance(ide_pid, int) and not isinstance(ide_pid, bool) and ide_pid > 0 and isinstance(ide.get("executable"), str):
        checks.append(_check("ide_process", "consistent", "ide_process_observed"))
    else:
        checks.append(_check("ide_process", "inconclusive", "ide_process_not_observed"))
    login = snapshot.get("application_login")
    checks.append(_check(
        "application_login", "consistent" if login == "logged_in" else "blocked" if login == "logged_out" else "inconclusive",
        "application_login_observed" if login == "logged_in" else "application_logged_out" if login == "logged_out" else "application_login_unknown",
    ))
    watch = snapshot.get("ua_watch")
    checks.append(_check(
        "ua_watch", "consistent" if watch == "active" else "blocked" if watch == "inactive" else "inconclusive",
        "watch_observed" if watch == "active" else "watch_inactive" if watch == "inactive" else "watch_unknown",
    ))
    cloud = snapshot.get("cloud_status")
    endpoint = snapshot.get("endpoint_status")
    if transport == "diacloud":
        if cloud == "online" and endpoint == "unreachable":
            cloud_status, cloud_code = "blocked", "cloud_online_endpoint_unreachable"
        elif cloud == "offline" and endpoint == "reachable":
            cloud_status, cloud_code = "inconclusive", "cloud_offline_endpoint_reachable_conflict"
        elif cloud == "online" and endpoint == "reachable":
            cloud_status, cloud_code = "consistent", "cloud_and_endpoint_reported_reachable"
        else:
            cloud_status, cloud_code = "inconclusive", "cloud_or_endpoint_evidence_missing"
        checks.append(_check("cloud_vs_endpoint", cloud_status, cloud_code))
    elif endpoint != "reachable":
        checks.append(_check("endpoint", "inconclusive", "endpoint_evidence_missing_or_unreachable"))
    else:
        checks.append(_check("endpoint", "consistent", "endpoint_reported_reachable"))

    status = "blocked" if any(c["status"] == "blocked" for c in checks) else (
        "inconclusive" if any(c["status"] == "inconclusive" for c in checks) else "consistent"
    )
    normalized = {
        "transport": transport, "target_ip": str(target), "observed_at": observed.isoformat() if observed else None,
        "route_epoch": epoch, "selected_route": route,
        "checks": checks,
    }
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "assessment": "offline_snapshot_only",
        "status": status,
        "network_ready": False,
        "target_contacted": False,
        "snapshot_origin": snapshot_origin,
        "observed_at": normalized["observed_at"],
        "age_seconds": round(age, 3) if age is not None else None,
        "route_epoch": epoch,
        "normalized_digest": f"sha256:{digest}",
        "transport": transport,
        "target_ip": str(target),
        "selected_route": route,
        "gateway_process": {
            "pid": gateway_pid if isinstance(gateway_pid, int) and not isinstance(gateway_pid, bool) and gateway_pid > 0 else None,
            "executable_name": _identity(PureWindowsPath(gateway_exe).name) if gateway_exe else None,
            "version": gateway_version,
            "candidate_count": len(candidates) if "gateway_candidates" in snapshot else None,
        },
        "checks": checks,
        "not_collected": ["target_port_probe", "application_protocol_proof", "PLC_identity", "HMI_identity"],
    }


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("JSON input exceeds size limit")
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("cannot read UTF-8 JSON input") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


_WINDOWS_INVENTORY = r"""
$ErrorActionPreference = 'Stop'
$target = '__TARGET__'
$interfaces = @(Get-NetIPInterface -AddressFamily IPv4 | Select-Object InterfaceIndex,InterfaceMetric)
$adapters = @(Get-NetAdapter | ForEach-Object {
  $adapter = $_
  $metric = @($interfaces | Where-Object InterfaceIndex -eq $adapter.ifIndex | Select-Object -First 1)
  $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $adapter.ifIndex -ErrorAction SilentlyContinue | ForEach-Object {
    @{ ip = [string]$_.IPAddress; prefix_length = [int]$_.PrefixLength; state = [string]$_.AddressState }
  })
  @{ id = [string]$adapter.InterfaceGuid; if_index = [int]$adapter.ifIndex;
     up = ($adapter.Status -eq 'Up');
     kind = $(if ($adapter.InterfaceDescription -match 'DIACom|VPN|Tunnel') { 'tunnel' } else { 'physical' });
     interface_metric = $(if ($metric.Count) { [int]$metric[0].InterfaceMetric } else { 0 });
     addresses = $addresses }
})
$routes = @(Get-NetRoute -AddressFamily IPv4 | ForEach-Object {
  $route = $_
  $adapter = @($adapters | Where-Object if_index -eq $route.InterfaceIndex | Select-Object -First 1)
  if ($adapter.Count) {
    @{ destination = [string]$route.DestinationPrefix; adapter_id = [string]$adapter[0].id;
       metric = [int]$route.RouteMetric }
  }
})
$selection = @()
try { $selection = @(Find-NetRoute -RemoteIPAddress $target) } catch {}
$selectedAddress = @($selection | Where-Object { $_.PSObject.Properties.Name -contains 'IPAddress' } | Select-Object -First 1)
$selectedRoute = @($selection | Where-Object { $_.PSObject.Properties.Name -contains 'DestinationPrefix' } | Select-Object -First 1)
$selectedPath = $null
if ($selectedAddress.Count) {
  $index = [int]$selectedAddress[0].InterfaceIndex
  if (!$index -and $selectedRoute.Count) { $index = [int]$selectedRoute[0].InterfaceIndex }
  $adapter = @($adapters | Where-Object if_index -eq $index | Select-Object -First 1)
  if ($adapter.Count) {
    $selectedPath = @{ adapter_id = [string]$adapter[0].id; source_ip = [string]$selectedAddress[0].IPAddress }
  }
}
$names = @('GatewayService.exe','CODESYSGatewayService.exe','CODESYSGateway.exe','DIADesigner-AX.exe','DIACom.exe')
$processes = @()
try { $processes = @(Get-CimInstance Win32_Process | Where-Object { $names -contains $_.Name } | ForEach-Object {
  $version = $null
  if ($_.ExecutablePath) {
    try { $version = [string](Get-Item -LiteralPath $_.ExecutablePath).VersionInfo.FileVersion } catch {}
  }
  @{ name = [string]$_.Name; executable = [string]$_.ExecutablePath;
     pid = [int]$_.ProcessId; version = $version }
}) } catch {}
@{ adapters = $adapters; routes = $routes; selected_path = $selectedPath;
   processes = $processes } | ConvertTo-Json -Depth 8 -Compress
"""


def collect_local_windows_snapshot(
    target_ip: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    now: datetime | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Read bounded local Windows inventory after explicit CLI selection.

    This runs no ping, TCP/UDP probe, login, IDE launch or network mutation.
    ``runner`` is injectable so contract tests cannot invoke host inventory.
    """
    target = str(_ipv4(target_ip))
    if (platform_name or os.name) != "nt":
        raise ValueError("local network inventory requires Windows")
    command = _WINDOWS_INVENTORY.replace("__TARGET__", target)
    try:
        completed = runner(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("local Windows inventory timed out") from exc
    except OSError as exc:
        raise ValueError("local Windows inventory failed") from exc
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("local Windows inventory failed")
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("local Windows inventory returned invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("local Windows inventory returned invalid shape")
    current = now or datetime.now(timezone.utc)
    processes = _items(raw.get("processes"))
    gateway_candidates = [p for p in processes if str(p.get("name", "")).casefold() in {
        "gatewayservice.exe", "codesysgatewayservice.exe", "codesysgateway.exe"
    }]
    gateway = gateway_candidates[0] if len(gateway_candidates) == 1 else None
    ide = next((p for p in processes if str(p.get("name", "")).casefold() == "diadesigner-ax.exe"), None)
    route_state = {key: raw.get(key) for key in ("adapters", "routes", "selected_path")}
    route_epoch = hashlib.sha256(json.dumps(route_state, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "observed_at": current.astimezone(timezone.utc).isoformat(),
        "route_epoch": route_epoch,
        "adapters": raw.get("adapters", []),
        "routes": raw.get("routes", []),
        "selected_path": raw.get("selected_path"),
        "gateway": gateway,
        "gateway_candidates": gateway_candidates,
        "ide": ide,
    }
