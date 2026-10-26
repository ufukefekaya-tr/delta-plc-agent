# ISPSoft: file codec and bounded Windows engineering layer

Updated 2026-10-26. This public core now supports the explicit AS228T/P/R v3010 source pointer in addition to the existing 0xAA/0xAC containers. It validates header version, pointer/prefix relationship, payload length, CRC, source model and protection state. ST edits retain the compiled prefix byte-for-byte; the compiled prefix becomes stale after source changes and is **not** a newly compiled program. The seven synthetic AS codec tests supplement the original file-adapter regression.

## Current source inventory

The host-specific ISPSoft development layer is maintained separately in the owner-private [Delta ISPSoft MCP repository](https://github.com/ufukefekaya-tr/delta-ispsoft-mcp). Access requires repository permission. Its source manifest contains the current Python/C# workers, MCP wrapper, source-export profile, build inspection, partial retained comparison, durable operation journals implementation, and offline tests. It contains no vendor binaries, runtime journals or PLC application sources. It is a source snapshot with an explicit existing-project profile, not a generic install-and-run adapter. The main public MCP remains eleven tools; the host layer exposes twenty-six under its separate launcher.

The local consolidation exercised actual digest-bound source export and positive/negative vendor SDK calls for AS228T-A / ISPSoft 3.19. Those local SDK artifacts are not redistributed. Both SIM and NoSIM returned zero errors/warnings. SIM execution matched its baseline; NoSIM execution changed from 95378 to 95542 bytes despite identical decoded native source and companion files. Cause and behavior equivalence remain unresolved. No native IDE CompileAll, deployed bytehash, full retained ABI, power-cycle or physical machine qualification is claimed. The partial retained comparison observed 119 of 120 scalar leaves with matching addresses and 227 retained words; missing/unobserved layout is not proven.

## Scope and workflow

Resolve the exact model, IDE/version and source profile. Select an explicit source manifest and native baseline hashes. Plan, apply a unique operation once, inspect its durable result, and verify hardware companions and source inventory. Same-project worktrees require a validated registration. Source export and an SDK code artifact are separate evidence levels. Unknown compiler outcomes are never automatically retried; stage, unsigned exit code and bounded log hashes support diagnosis.

ISPSoft and normal DIADesigner supported series remain the engineering scope, with model-specific qualification still required; AX is excluded from this extension. Hardware module/slot/channel/range/address editing is performed in the vendor IDE with Computer Use. Source/export/build/watch use validated APIs where supported. This documentation does not grant a physical-device operation. The source snapshot preserves an existing project binding; those symbols, addresses, aliases or transport identity must never become public-core defaults or a new project's profile.

## Validation and rollback

Run `python -m unittest discover -s tests/delta_files -v`. These are software fixtures and never contact an IDE or PLC. Reverting the codec commit restores the former unsupported-pointer behavior. A future native implementation needs independently qualified exact vendor inputs and its own evidence; capability metadata cannot stand in for that acceptance.

The public codec also fixes a final-review finding: valid compiled-prefix bytes that resemble a fixed legacy stream marker cannot override an explicit v3010 pointer. The private snapshot retains the original pinned host codec V1 for source provenance; its new source-export path is separate. This public core change is not silently installed into that host environment.
