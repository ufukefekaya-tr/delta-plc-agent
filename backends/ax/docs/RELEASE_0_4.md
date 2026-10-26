# AX backend 0.4.0

This release brings the reviewed native revision and transfer-lifecycle work into
the public backend. It preserves generic LadderRecipe v2, explicit OPC UA signal
bindings, category-aware compiler diagnostics, and offline project creation.

## Public operations

The AX server exposes 24 operations in autonomous mode and the unchanged five
replay operations. The Universal adapter additionally retains `project.create`.
New operations are `revision.apply`, `candidate.snapshot`, `job.progress`,
`operator.profile`, and `operator.evidence.check`. They are routed explicitly by
the Universal adapter as well as the standalone AX stdio server.

`axmcp-revision --help` describes the packaged native revision client. A batch is
bound to one workspace, expected revision and idempotency key. Save/reopen evidence
and an immutable `NATIVE_SAVED_UNCOMPILED` candidate do not imply a successful
build or deployment. Build and artifact freeze remain separate gates.

Existing LD replacement is deliberately narrow: a single MachineSupervisor
contact and normal coil, preserving native identity and metadata. General
LadderRecipe v2 creation remains available. Replacement is not a general graphical
editor; this release provides offline regression evidence, not new vendor acceptance.

## Download, RUN, watch and Stop

The deployment chain and bound OPC UA bench methods are retained. No public
target approval or machine binding is added by installing this release.
`target.observe.stop` ends observation; it is not a PLC Stop command. Operator
Stop, application Stop and watch shutdown have distinct contracts.

After transfer intent, a deadline or exception preserves the owned transfer
session. It does not kill the IDE, close the project, retry download or send RUN.
Unknown outcomes require reconciliation. Late completion remains evidence.
LAN behavior is retained; DIACloud uses a shared absolute build/transfer deadline
with a 600-second ceiling, rather than restarting a budget at every step.
Heartbeat is not transfer progress; unavailable bytes/percent remain null.

`axmcp doctor --help` documents read-only connection-profile diagnostics. A supplied
network snapshot is a claim to evaluate, not fresh PLC discovery. Local route,
source adapter, gateway, application login and UA watch are separate observations.

## Evidence helpers and portability

`operator.profile` returns `example-six-drive-v1`, a **synthetic reference** with
six drive slots, two coefficient channels and explicit ramp masks 15/47.
Its Example symbols are not live bindings. `operator.evidence.check` checks only
caller-supplied observations: active faults even with code zero, null/Bad/stale
values, distinct channel ACK/readback, frozen ramp participants and pulse release.
It never connects or writes and never proves physical motion or scan atomicity.
Other machine contracts require their own reviewed mapping and acceptance.

Private project symbols, native projects, local paths, warning exemptions,
historical session authority and benchmark scripts are excluded. A machine-specific
source-tag ban stays in its owning project; it does not become a generic runtime
ban. Public process liveness uses read-only Windows queries, never `os.kill(pid,0)`.

## Verification

Run the repository [validation commands](../../../docs/public/VALIDATION.md).
The release includes negative transfer, deadline, revision, evidence and routing
tests. A software PASS is separate from native compiler and live PLC acceptance.
See [public source provenance](../../../docs/public/PROVENANCE.md) and the
[publication lesson](../../../kaizen/KZ-20261026-001-ax-public-sync.md).
