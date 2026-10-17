# DVP / WPLSoft agent delivery

This is the execution guide for the existing Universal MCP, not a new PLC compiler or raw writer. Follow [canonical authority](../SYSTEM_PROMPT.md#2-yetki-çevrimdışı-ide-ve-canlı-hedef). The [job template](../examples/dvp-job.json) describes a request; it does not grant live authority. The [workflow recipe](../workflows/dvp-final.json) orchestrates existing W1-W7 plus separately recorded GUI stages.

## 1. Discover and provision

Read the current approved source handoff, exact CPU model, route and tag contract. Hash the source and compare with the approved manifest before each production, compilation and deployment stage. Highest filename/version/time alone is not approval. Keep machine recipes and addresses outside Universal core. Old V10 examples are historical; never silently repin them to a new program.

**Required agent rule:** inspect before installing. If a required software component or driver is absent, obtain the exact package from its official publisher, verify its signature or approved SHA, use documented installation arguments, then inventory it again. Do not delegate ordinary supported installation to the operator. Preserve suitable installations. Report a specific blocker when administrator privileges, reboot, publisher download terms or an unqualified installer prevent the supported path. Hardware cannot be installed by downloading: report missing PLC, adapter, cable or gateway separately from a missing driver.

The [prerequisite catalog](../packaging/dvp-prerequisites.json) records official origins and qualification. Python has a documented silent bootstrap recipe. WPLSoft and vendor driver **silent installer contracts remain unqualified**; do not invent `/S` switches or execute arbitrary downloaded installers. Research the exact official installer when needed, record the verification and installation receipt. Windows GUI installation follows the active Computer Use skill's confirmation requirements. No security bypass or automatic reboot.

On Windows, from the extracted release root:

```powershell
./tools/bootstrap_dvp.ps1 -CheckOnly
./tools/bootstrap_dvp.ps1 -PythonExe 'C:/path/to/python.exe'
./.agent-state/dvp-venv/Scripts/python.exe tools/dvp_prerequisites.py --route directlink_cdc --port COM6 --require-vendor
```

Paths/COM6 above are examples, not defaults. Bootstrap reuses a suitable interpreter or downloads the pinned official x64 Python with verified publisher signature, creates a private venv and installs only Universal DVP dependencies. It does not install AX or start a vendor IDE. Use `python tools/install.py --profile dvp` with an existing managed venv. Default `--profile all` preserves the original AX-inclusive install. Environment/installer failures are terminal, not permission to loop or reinstall blindly.

Configure the MCP client with the installed venv Python and `-I -m universal_plc --root <absolute-runtime-root> --allow-read <approved-input-root> serve`. Set `UNIVERSAL_PLC_DVPSERIAL_CONFIG` to the absolute host enrollment. Never distribute this host's credentials, lease tokens, live permissions or absolute project paths in the package. The observation helper uses the installed package and explicit `target_model`, not a developer checkout on PYTHONPATH.

## 2. Produce, compile and freeze — WPLSoft

Use existing qualified file tools where applicable; WPLSoft creation/editing, compile/save/reopen use Computer Use. No new `project.build` implementation is implied by catalog names. The active Computer Use skill is the sole Windows UI control path. Verify executable digest and select exactly one returned WPLSoft window. Work on a task copy/new neutral project, never the approved original. Keep UI evidence, compile diagnostics and saved candidate digest. Reopen the saved candidate and verify compile again. A checksum conversion or software fixture is not vendor compilation.

The desktop must be unlocked and expose a controllable vendor window. An unreachable window is `BLOCKED_VENDOR_UI`; do not substitute guessed keystrokes, DLL calls or process memory. Stop after the skill's bounded recovery. Complete independent packaging/tests while vendor or hardware stages are blocked.

## 3. Optional download — WPLSoft

Download only when the current human job requests it and exact target/revision/physical conditions are current. The default is no download; final state STOP unless separately authorized otherwise. Follow the existing target fingerprint and exclusive lease contract.

End MCP observation, invoke `target.disconnect`, confirm closure, then hand ownership to WPLSoft. Obtain and preserve a target program backup through the vendor tool, download the frozen candidate, and perform the vendor program compare or independent upload/readback comparison. Record what was actually compared; binary `.dvp` container metadata can differ. The completion dialog is not an independent match. If comparison is unavailable, mark `program_comparison: NOT_RUN`, not transfer acceptance. Release WPLSoft's connection before MCP resumes. Never kill another agent's program, delete its lock or select an alternative COM port automatically.

Existing experimental SS2 native MCP transfer remains opt-in and separately qualified. This delivery neither extends it nor uses it as a fallback to a blocked WPLSoft UI. A lost GUI download result is `UNKNOWN_OUTCOME`: preserve candidate/backup and reconcile the actual target through the vendor tool before another transfer. Do not feed GUI events into a fabricated native MCP journal.

## 4. Watch and existing controls — MCP only

After GUI releases the handle, acquire the current shared lease, connect, verify target identity and read status twice. USB gateway identity alone is not a CPU model. GUI does no watch/polling. Default observation is 90 seconds at a target 1 second interval; actual serial processing time can increase the interval. Values are timestamped, non-atomic and only interpreted through the exact live revision contract. Unknown live binding means raw/unverified observations, never a write.

```powershell
./.agent-state/dvp-venv/Scripts/python.exe tools/dvp_controls_observe.py --config C:/job/profile.json --runtime-dir C:/job/runtime --evidence-path C:/job/profile-check.json
./.agent-state/dvp-venv/Scripts/python.exe tools/dvp_controls_observe.py --config C:/job/profile.json --runtime-dir C:/job/runtime --workspace-id ws_CURRENT_ENROLLED_ID --evidence-path C:/job/watch-001.json --observe
```

Each evidence path must be new. `machine.profile` is offline by default. Actual observe requires current enrollment and ownership, plus the exact existing workspace and runtime root bound by its live revision evidence. `workspace.status` validates the stored context/baseline; the helper checks target and revision before requesting observations. It does not create or repin live authority. Each sample must return `live_revision_verified=true`; otherwise the raw sample is retained and the run ends incomplete. On failure, stale values are never fresh samples; evidence remains incomplete and the owned connection is closed. Read-only retries require a fresh authorized run after identity/lease reconciliation. Never replay a write or automatically issue RUN as a connection fix.

Existing `machine.command` and `machine.parameter.write` use named profile keys, limits, guards and independent readback. For M/D only, `user_present=true` with either `heaters_isolated=true` **or both** `attended_machine_test_authorized=true` and `independent_power_cutoff_available=true` is required. All are actual JSON booleans, under the existing expiring, source-bound host enrollment. Current conditions cannot be inferred from an old receipt. Separate permissions, late checks, uncertainty fences and read-only reconciliation remain. This alternative does not authorize program transfer, RUN/STOP, forced Y or direct module CR writes.

HMI address syntax, CPU memory existence and allowed operation are separate checks. M/D are decimal; Y is octal in the existing MCP. HMI D0..11999/M0..4095 syntax does not widen this adapter's ranges. Source-pinned HMI contracts inform external profiles only; HMI runtime/DIAScreen dependencies are not required for PLC MCP.

## 5. Deliver and close

Produce native candidate and hashes, vendor UI evidence, optional backup/compare results, MCP observations, exact package version, test/review results and remaining limitations in one index. Use `PASS/FAIL/NOT_RUN/NOT_REQUESTED/BLOCKED/UNKNOWN_OUTCOME` per stage. A software-ready package is not a fully accepted physical job. No arbitrary machine source/profile changes are part of package acceptance.

Run the DVP/runtime/delivery tests and clean-wheel SDK test. Independent final review follows all code, evidence, feature and Kaizen updates. Close with a scoped local commit; push/tag publication requires separate authorization. Maintain [goals](../goals/dvp-final.json) and [current delivery](public/VALIDATION.md). Do not perpetuate old V5/V7/V10 names as the current live program.
