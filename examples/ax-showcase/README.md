# Neutral AX showcase inputs

Start with the [actual run page](../../docs/showcase/AX_DEMO.md). Result: **PARTIAL_BLOCKED**, not a completed plan-to-frozen-artifact cycle.

[Exact preparation and typed sequence](evidence/REPRODUCE.md) · [Input contract](evidence/contract.json) · [Final declaration](evidence/declaration-fixed.st) · [Final implementation](evidence/implementation-fixed.st) · [Compiler receipts](evidence/compiler-receipts.json).

The files were authored from scratch for this offline IDE demonstration. They contain only internal variables, no mapped physical I/O, customer logic or machine addresses. The separately planned invalid source and Ladder recipe are labelled NOT_RUN in the summary. Original neutral input and evidence bytes are preserved by the evidence-directory attributes; the manifest hashes can be checked after cloning or extracting a Git archive.

Use your own configured host; the public evidence includes hashes and returned identifiers, not reusable runtime authority. Do not replay recorded job/workspace IDs. Carry the fresh IDs, revisions and digests returned by your own run. Existing acceptance gates remain in effect.
