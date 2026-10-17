# Feature map

Delta PLC Agent separates the reusable engineering interface from each model's tools and evidence. Extend coverage without changing the core workflow.

## Eleven common MCP tools

| Tool | Purpose |
|---|---|
| `environment.probe` | Inventory host tools without contacting a PLC |
| `target.resolve` | Match vendor, exact model, toolchain and project format |
| `capabilities.list` | Inspect implementation and qualification independently |
| `workspace.create` | Create a target/task/revision-bound workspace |
| `workspace.status` | Verify context and source baseline |
| `workspace.import` | Import a hash-checked file from configured read roots |
| `adapter.invoke` | Invoke a registered typed operation |
| `evidence.read` | Read durable operation and result records |
| `workflow.start` | Start a persistent W1–W7 workflow |
| `workflow.run` | Execute explicit dependent steps |
| `workflow.status` | Inspect progress and recover context |

## Engineering paths

| Path | Included implementation | Qualification |
|---|---|---|
| AX | Project factory, ST/Ladder workers, build/readback, jobs, artifact and deployment infrastructure; 19 established operations plus `project.create` | Model, IDE/device profile and operation are qualified individually. Public-chain and live deployment acceptance are separate. |
| ISPSoft files | Container roundtrip, existing ST/local-variable patch, POU/library import/export | Software-tested transforms; AS native IDE creation/build acceptance is a separate step. |
| DVP file tools | Supported CSV IL↔MPU; explicit DVP-SE template-based JSON generation | Command subset and exact target checks. File generation is not vendor compilation or general DVP→AS conversion. |
| DVP serial | ASCII/DirectLink CDC, inventory, identity/status/reads, profile commands and 16-bit D writes with readback, watch and reconciliation | Current serial model: `DVP14SS211R`. Live operations require current source/target enrollment. Hardware acceptance is separate from software tests. |
| WPLSoft | Agent Computer Use recipe for editing, compile, save/reopen and requested download; MCP observation | Documented agent workflow, not a WPLSoft MCP compiler. Native acceptance requires accessible IDE and current target. |
| SS2 transfer | Opt-in backup/preparation/execution/reconciliation software | Experimental; live transfer qualification remains open. |
| DIADesigner | Model/toolchain catalog route | Native implementation is a roadmap workstream. |
| Extensions | `universal_plc.adapters` entry points and extensible registry | Synthetic second-vendor tests verify the interface; each real integration establishes support. |

[Exact model catalog](../catalog/README.md) · [AX](../backends/ax/README.md) · [File operations](adapters/delta-files.md) · [DVP profiles](adapters/dvp-controls.md) · [Validation](public/VALIDATION.md)

## Agent collaboration

Six roles pair engineering with independent verification and Kaizen. Source hashes, task scopes, checkpoints, behavior contracts and local commits give the next agent a concrete continuation point. [Seven workflows](../workflows/README.md) cover bootstrap, creation, edits, transitions, observation, recovery and learning.

General [AX lessons](../backends/ax/docs/AX_LESSONS.md) retain ScriptEngine version handling, Unicode/BOM, save/reopen, symbol publication, worker lifecycle, timeouts and Stop races.
