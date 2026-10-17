# Universal AX backend

Standalone Python backend migrated from the user's AX implementation. Package
identity is `universal-plc-axmcp`; Python imports remain `axmcp_offline`. The
`axmcp`, `axmcp-offline`, and `axmcp-stdio` commands and all 19 existing operation
names are preserved. This code is MIT licensed; see [LICENSE](LICENSE).

## Install and run

From this directory, use `python -m pip install .`. The root repository installer
installs this backend together with Universal PLC. Its wheel includes every
worker, schema, protocol document, synthetic replay and configuration template.
It does not access the historical source repository or depend on its location.

`axmcp doctor` checks the selected mode. `axmcp-stdio` starts the compatible AX MCP.
Universal PLC calls `AxAdapter(config_path).invoke(operation, arguments)` through
the shared MCP. Its `capabilities()` method requires no vendor IDE and performs
no target connection. Configuration paths resolve relative to their config file.

By default the legacy CLI uses **simulated replay**, which returns `unverified`
and `live_ax_verified=false`. It never means an installed IDE or connected PLC
has been detected. Use the existing CLI initialization commands (`axmcp --help`)
to create an external scratch workspace and pinned worker configuration.

Universal calls additionally carry `universal_target`. The adapter requires an
explicit matching `target` in its config; it never substitutes AX308. The target
contains vendor, family, model, toolchain, ide_version and optional firmware and
project_format. Vendor deployment enrollment/fingerprint guards remain separate.

```json
{
  "mode": "recorded_replay",
  "target": {
    "vendor": "Delta", "family": "AX", "model": "AX-308EA0MA1P",
    "toolchain": "DIADesigner-AX", "ide_version": "1.9.0.9194",
    "project_format": ".project"
  }
}
```

For native operation use `autonomous_file_bridge`, scratch_root, timeout_seconds,
autostart_worker and autostart_deployment_worker as explicitly configured fields.
Machine addresses, credentials, authority and signal bindings belong to the
consumer workspace. They are never shipped as operational defaults.

## Deliberate compatibility changes

- LadderRecipe v2 requires an IEC project identifier and `output_allowlist`.
  This replaces fixed machine POU/output lists. It still checks declared BOOL
  outputs, a single writer, supported FB signatures, bounded network count and
  save/reopen semantic topology. Version 1 is rejected rather than broadened.
- OPC UA `BenchConfig` requires namespace/application prefix, a complete named
  signal binding, drive count and idle/automatic state values. Bindings are copied
  and frozen. The existing bounded command contract, exact fingerprint and Stop
  generation/ownership behavior remain. This is an opt-in application contract,
  not a universal assumption that every PLC program has those signals.
- Historical machine-specific warning exemptions no longer match real project
  digests. New native projects must retain and review their actual diagnostics.
- AX308/SP18 deployment identity is a version-specific profile, never a fallback
  for other models. The live adapter approval set is still empty. No live
  deployment readiness is claimed by this migration.

## Native project creation

`adapters/ax_worker/project_factory.py` provides the actual offline ScriptEngine
factory. Its API is `create_project(projects, PouType, ImplementationLanguages,
KindOfTask, config)`. The exact config fields are output_path, project_id,
device_profile, controller_name, pou_name, task_name, interval_ms, declaration and
implementation. The device profile fields are profile_id, model, device_type,
device_id and device_version, sourced from the installed vendor catalogue.

The factory creates a new `.project`, adds the exact controller, uses the native
application skeleton, creates a called ST program and cyclic task, then saves,
reopens and verifies source/device/task. Existing files are rejected; existing
native task names and calls are preserved. Task-call tree nodes cannot masquerade
as textual POUs. No PLC is contacted. Native compilation, negative called-POU
testing and public MCP launcher qualification are recorded independently.

The public `project.create` operation requires a `project_factory` object in the
adapter configuration with these exact fields: ide_executable,
ide_executable_sha256, script_engine, script_engine_sha256, ide_profile,
output_root, timeout_seconds (10–600) and device_profile. Tool file paths are
configured/discovered on the destination host; hashes are verified before queueing
and again before launch. output_root resolves relative to the adapter config.

Public arguments are project_id, controller_name, pou_name, task_name, interval_ms,
declaration and implementation, plus the universal_target supplied by the router.
No output path or executable is accepted from tool arguments. The adapter creates
a unique child directory and returns `create_<uuid>` immediately. `job.status`
reads durable request/result evidence and checks created project bytes.
`job.cancel` writes a durable cancellation marker for the dedicated supervisor.
That supervisor owns only a new offline scratch IDE, preserves failed artifacts
and records an unknown outcome on timeout. It never supervises a PLC transfer.

The optional bound OPC UA transport requires `pip install .[opcua]`; installing
that dependency does not enable any target or supply application bindings.

## Evidence and tests

Run from the repository root:

```text
python -m unittest discover -s tests/ax -t tests/ax
```

Use an environment with this package and requirements-dev.txt installed. The
tests use synthetic projects and test doubles, not real controllers. The source
manifest records what was copied, generalized or excluded; historical source
paths are lineage only. The neutral fixtures have new hashes and must never be
misrepresented as original live evidence.

- [Source migration manifest](../../docs/public/PROVENANCE.md)
- [AX runbook and transferable lessons](docs/AX_RUNBOOK.md)
- [Every source kaizen candidate](docs/AX_LESSONS.md)
- [Compatibility protocol](protocol/AXQ-FILE-2.md)
