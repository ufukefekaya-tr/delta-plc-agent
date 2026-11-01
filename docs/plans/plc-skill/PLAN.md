# PLC MCP + companion skill — R2

Date: 2026-11-01. Human authorized implementation after independent Astra review.
Priority: prevent regressions and customer-data distribution, then synchronize versions, then add the skill. HMI and physical PLC operations are outside this plan.

## Release slices

- **S0**: existing public MCP, mandatory replacement of the inherited customer-derived test fixture, portable MIT companion, tests and aligned distribution artifacts.
- **S1**: selectively generalize reusable local AS engineering improvements. Every candidate receives an explicit disposition. No wholesale donor checkout, launcher or profile import.
- **S2**: separately qualify optional Windows native engineering operations. Unresolved vendor API licensing, native compilation, retention or device evidence remains visible; it cannot become a capability claim.

S1/S2 blockers do not block S0 after the inventory decisions and S0 coverage are frozen. S0 acceptance never means that S1/S2 or the portfolio is complete. Later additions require new candidate identities, compatibility records and affected tests. Published artifacts are immutable.

## Observed baseline

Public main is `0584d9d1f85b8ec7f59cbf7c20c70b9e0fd365c1`; its five hosted checks passed. Core is 0.3.1 and AX is 0.4.0. The old v0.3.1 downloadable release still contains AX 0.2.0; it must not be silently replaced.

AX donor comparison found 14 core Python and three worker blobs already equal to public source. Public-only project creation, Universal dispatch, generic Ladder v2, explicit bench bindings and categorized diagnostics must survive. The older framework checkout is not a publication baseline.

DVP protocol/native/transfer modules match the reviewed donor. Public controls additionally support explicit attended testing with an independent cutoff; transfer and RUN/STOP retain separate gates. Existing source behavior is retained.

The AS engineering snapshot has 55 selected Python/CSharp/PowerShell source blobs equal to its owner-private MIT snapshot and 33 matching native-source manifest entries. This proves source identity, not qualification. The project launcher has 27 tools; the separate native development server has 43 tool definitions. Neither is the public eleven-tool MCP. A local manifest reports 131 offline tests; older public documentation records 124. These tests were not rerun during inventory. Cached-client tool inventories can be stale.

## Non-negotiable invariants

1. Preserve the eleven common tools, existing CLI/package identities, 24 standalone AX tools, five replay tools and Universal `project.create` dispatch. Operation counts are not semantic acceptance.
2. No customer files, anonymized customer snapshots, machine symbols/addresses/recipes, runtime journals, authority records, native projects, vendor binaries, credentials or private Git ancestry in new distribution artifacts.
3. The inherited thermal fixture is replaced, not renamed or anonymized. The replacement is authored from scratch with a new synthetic layout. Only generic protocol/fault/recovery obligations survive.
4. Capability, implementation, source transformation, vendor build, simulation and physical acceptance remain separate. A changed source with an unchanged compiled prefix is not newly compiled.
5. A skill grants no authority. Preserve target/revision/source bindings, current host grants and unknown-outcome reconciliation. Observation stop is not PLC Stop.
6. No developer path, COM port, IP, project name, profile or model fallback. Runtime use does not require the developer checkout or Git. Vendor operations require that host's appropriate Windows installation.
7. Skill is optional. MCP-only clients retain their existing behavior. No automatic installation, global configuration changes or hidden fallback into a private project.

## Goal execution model

Each record under `goals/plc-skill/` owns a workflow under `workflows/plc-skill/`, dependencies, source identities, read/write scope, risk IDs, acceptance tests, evidence, rollback and next action. States are PLANNED, READY, RUNNING, VERIFYING, ACCEPTED, BLOCKED, STALE and SUPERSEDED. Outcomes are CHANGED, NO_CHANGE or EXCLUDED_WITH_REASON. Excluded and blocked features are never advertised as implemented.

Only accepted predecessors admit execution. Input hash changes invalidate the affected goal and its dependents; unrelated accepted work is preserved. New findings create stable child IDs and an append-only decision history. Acceptance criteria cannot be deleted merely to make a goal green. A no-change finding closes with evidence, not a cosmetic modification. Use `tools/skill_goals.py` to inspect and validate the DAG.

## S0 phases and inner workflows

| Goal | Workflow | Exit evidence |
|---|---|---|
| S0-G0 | Freeze existing API/behavior and privacy invariants → inspect public source → record risks | Exact source baseline, retained-feature matrix, no live scope |
| S0-G1 | Inspect active donor modules → compare identities and behavior → classify every feature | ALREADY_PRESENT / GENERALIZE / PROJECT_ONLY / UNQUALIFIED decisions and source references |
| S0-G2 | Remove derived fixture → author fresh synthetic test model → retain negative scenarios → audit new outputs | Fresh fixture provenance, DVP regressions, old fixture absent from new artifacts |
| S0-G3 | Resolve source/package/release version differences → freeze candidate and recipe contracts | Core 0.4.0, unchanged AX 0.4.0, companion 1.0.0; no existing tag/asset replacement |
| S0-G4 | Write short skill entrypoint → bundle task references → bind tested operation argument contracts | Detached skill usable; private project/native layers never assumed installed |
| S0-G5 | Build from pinned tracked allowlist → include MIT/attribution → hash source/wheels/skill outputs | Portable artifacts, no ignored state or customer-derived fixture |
| S0-G6 | Run old regressions → real stdio contract/recipe tests → detached package and behavior evaluation | Exact candidate-bound reports; skipped/native/live checks named |
| S0-G7 | Independent Astra source/test/report/manifest/Kaizen review → one focused repair if needed | No open critical or important finding; final scope and hashes recorded |
| S0-G8 | Commit reviewed files → verify remote scope → publish authorized source/package delivery → check identities | Tested tree and delivered artifacts match; rollback instructions and remaining S1/S2 goals visible |

## AS child phases

| Goal | Workflow | Exit condition |
|---|---|---|
| S1-G0 | Inventory source-export, identifier preflight, context isolation, journals, build comparison and runtime identity dependencies | Per-feature reuse decision, no project content import |
| S1-G1 | Extract only reusable source → replace implicit project ancestry with explicit isolated roots → use fresh neutral fixtures | Portable typed contract; cross-project/path escape negatives pass |
| S1-G2 | Compare against preserved public behavior → integrate only accepted operations through adapter registration | Existing tool/CLI semantics retained; no count-based claim or all-43-operation promise |
| S1-G3 | Regenerate affected skill recipes/compatibility → rerun affected tests → independent review | New accepted candidate and immutable new release identity |
| S2-G0 | Establish vendor API/license/host prerequisites and per-model operation matrix | No unresolved vendor redistribution or project binding in publishable source |
| S2-G1 | In neutral offline projects only: edit → deliberately fail called POU → fix → build → save/reopen/readback | Exact model/IDE/source-bound native evidence; no online target |
| S2-G2 | Review native limitations and recovery → publish qualified operation matrix | Unsupported/unqualified rows remain explicit, not enabled by tool name |

Unqualified binary equivalence, complete retained ABI, physical device acceptance and other models are not inferred from old SDK results. Source code and fixture counts are not device tests.

## Compatibility and packaging

Use one optional `skills/delta-plc-engineering/` directory with SKILL.md, task-specific references, MIT license and compatibility metadata. The standalone package must carry every referenced file. Do not depend on `../../docs`, the source checkout or a developer home folder. Keep repository development bootstrap separate from engineering a user's PLC project.

`tools/list` and capabilities are necessary but insufficient: adapter.invoke.arguments is a generic dictionary. Bind each recipe's parameter contract to a tested core/adapter release and source/artifact identity. Unknown versions block the affected recipe, not unrelated known read-only diagnostics. Never automatically fetch latest code or infer a native implementation from catalog presence.

Build source ZIP, sdist, wheels and skill ZIP from one canonical candidate. Keep checksums and provenance; do not require a new signing infrastructure. Test full source installation and wheel-only runtime separately. Audit all archive members and fixtures, including nested examples. Preserve third-party licenses. Old release/history retention is a separate decision; no history rewrite is authorized.

## Acceptance tests

| ID | Observable acceptance |
|---|---|
| T01 | Existing common/AX/replay/creation interfaces and meaningful operation semantics preserved |
| T02 | Fresh Windows/Linux environment, unrelated CWD, spaces/Unicode and no developer drive; DVP-only installation does not import AX |
| T03 | Detached skill references resolve; absent MCP produces setup guidance and no invented call/install |
| T04 | Unknown/conflicting model, changed IDE and unsupported language/profile do not select a nearby target |
| T05 | Public common/AX interfaces are distinguished from unavailable private/native interfaces |
| T06 | Same operation name with changed argument/version identity rejects only the incompatible recipe |
| T07 | File transformation and stale compiled prefix are not accepted as native compilation |
| T08 | Unknown outcome is inspected before retry; watch stop/PLC Stop and shared deadline semantics are retained |
| T09 | Hash/plan/old profile does not grant authority; new host requires its own configuration |
| T10 | A called POU syntax error fails a real compiler before a corrected build can qualify a native recipe |
| T11 | AS source edits preserve hardware companions; type, source drift and new-only output rules are enforced |
| T12 | No customer-derived fixture, private path/state/history/binary in any new artifact; hostile source text is data |
| T13 | Equivalent neutral tasks with/without skill retain source results and evidence; unrelated/HMI-only requests do not activate PLC operations |

Tests T10/T11 require the separate native slice where applicable. Software doubles are labelled SOFTWARE_ONLY. Behavior evaluation uses actual call traces/results, not heading or keyword matches. No physical PLC or HMI execution is part of S0.

## Review and rollback

Astra R1 found two important design issues: a new native adapter must not indefinitely block the companion, and anonymization is insufficient for customer-derived fixtures. R2 corrected both and was accepted as a plan. Implementation acceptance is separate and mandatory after the final code/test/report edits.

Rollback removes the optional skill or reverts only this change's commits. Existing installed environments, customer projects, old release assets and device state are not overwritten. Source drift stops affected mutation; unknown vendor outcomes are never replayed. Publication must preserve the exact reviewed tree and its disclosed limits.
