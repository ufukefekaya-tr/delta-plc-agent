# Neutral AX demo: actual run and reproducibility

Result: **PARTIAL_BLOCKED**, recorded on 2026-11-01. This is an actual vendor IDE
run through the public Universal MCP stdio interface, not replay or a test double.
The fixed public source is `9b209e1e559a726bdce12450c48af56b60ed6cc8`.

## What the recording establishes

The existing project factory created a new AX-308EA0MA1P project, added and called
Demo_PRG from a cyclic task, saved and reopened it, and verified the exact device,
task and ST. Source readback passed. Native revision batches produced closed,
reopened, digest-bound candidates. The real compiler rejected an agent input error
(`Limit` collides with builtin `LIMIT`); the agent corrected only the neutral ST
to `CountLimit`. The corrected compiler summary was 0 errors / 0 compiler warnings.

The unchanged build acceptance gate still rejected 21 environment warnings:
`Device description for '<Empty>' is missing`. It is not a fully accepted build,
frozen artifact or completed end-to-end qualification. No warning exemption,
private worker, runtime patch, customer baseline or device-slot edit was used.

The separately planned `Counter := ;` negative test and its restoration were
**NOT_RUN** after this gate. The observed earlier LIMIT error is not relabelled as
that planned test. Ladder has a schema-valid planned input, but its native import,
roundtrip and build are **NOT_RUN**. No physical PLC was contacted or executed.

## Host preparation, before the recorded automation

1. Use the fixed public source and its documented pinned Python dependencies.
   The relevant core/backend/worker files must match the Git blobs. The recording
   verified 54 runtime files; `summary.json` contains the exact source and tool hashes.
2. Discover your own installed DIADesigner-AX executable, ScriptEngine and profile.
   This host used DIADesigner-AX 1.9.0.9194, profile `DIADesigner-AX 1.9`,
   ScriptEngine 4.0.0.0. Do not substitute the historical worker or installation.
3. Verify the installed device catalogue: model AX-308EA0MA1P, type 4102,
   device ID `16f7 0319`, version `1.0.9.2`. The public catalogue record hash is in
   `summary.json`; the real project factory independently verifies saved identity.
4. Make a **new** isolated scratch root, initialize the existing autonomous
   backend, and use `configure-ax-worker` with the actual executable, ScriptEngine,
   worker and ladder-helper SHA-256 pins. Keep deployment autostart disabled and
   the target list empty. Configure project_factory with the exact device profile,
   an unused output directory and a 180-second deadline.
5. Point `UNIVERSAL_PLC_AX_CONFIG` and `UNIVERSAL_PLC_ROOT` to your own local
   configuration/workspace. Start the existing Universal MCP `serve` command.
   Host paths and generated native projects are private and are not supplied here.

## Recorded typed operation sequence

All operations below used real stdio `adapter.invoke`, bound to the context digest
returned by the common `workspace.create`. The common file workspace and the AX
native workspace are separate handles. Carry forward returned IDs, hashes and
revisions rather than inventing or replaying them.

1. Resolve the exact target, create a Universal context, then invoke `project.create`:
   project `NeutralPermitDemo`, controller `DemoController`, POU `Demo_PRG`, task
   `DemoTask`, interval 20 ms. Initial ST is in `declaration.st` and `implementation.st`.
   Read `job.status` for the returned creation job until its one terminal result.
2. Enroll the newly created project's verified digest as the native baseline.
   Invoke the AX `workspace.create` with that digest and the pinned environment.
3. Invoke `source.read` for Demo_PRG at revision 0 and compare both ST fields.
4. Invoke `revision.apply` for exact object path
   `DemoController / Plc Logic / Application / Demo_PRG`, expected revision 0,
   unique idempotency key, unchanged declaration and `implementation-corrected.st`.
   This first revision adds only a comment; it retains the initial LIMIT mistake.
   Await its job, then call `candidate.snapshot` at revision 1. It is saved and
   uncompiled, not a deployable artifact.
5. Invoke `project.build` in rebuild mode at revision 1. Record the current failed
   job, its two Demo_PRG compiler errors and 21 environment warnings.
6. In the same stable workspace, invoke a new `revision.apply` at revision 1 using
   `declaration-fixed.st` and `implementation-fixed.st`; use a new idempotency key.
   Verify save/reopen evidence and candidate digest at revision 2.
7. Invoke a fresh rebuild at revision 2. The current run reported zero compiler
   errors and 21 retained environment warnings, so stop here with the exact blocker.
   Do not call this accepted or freeze/deploy it, alter warning policy or fill device slots.

There were no human clicks during either recorded automatic attempt. Agent
inspection and input correction occurred between them and count toward total
elapsed time. `timeline.json` contains the real UTC events; it is not an IDE screen
recording. A short presentation may condense it if labelled as an edited event
visualization, with the partial outcome visible.

## Evidence and publication boundary

`public-source-pins.json` contains all 54 public source/blob/file identity pairs.
`actual-call-timeline.jsonl` is a complete sanitized call index: every typed call, its UTC timestamps,
operation, duration, result status and hash of the retained full private record.
Polling calls are listed explicitly; they are not extra builds or downloads.
It does not contain full request arguments or result bodies.
`compiler-receipts.json` contains a privacy-filtered projection of the two actual
build results, bound to their retained source JSON hashes, project/revision and
ST digests. It separates compiler diagnostics from aggregate environment warnings.

`summary.json`, `timeline.json`, these instructions, the neutral ST files and the
explicitly NOT_RUN Ladder recipe are approved public inputs. `manifest.json`
hash-binds this allowlisted package. There are no host paths, credentials or
customer sources in it.

Full request/result records, exact OS command observations, native projects,
precompile caches, user options, vendor logs and process observations remain in
the private task directory. The first typed shutdown receipt was unavailable;
later read-only process checks confirmed both owned workers had exited. That is
not misrepresented as a successful typed-shutdown or recovery demonstration.

This experiment does not prove cyclic PLC behavior, online operation, physical
motion, Ladder qualification, restoration/rollback or artifact.freeze acceptance.
