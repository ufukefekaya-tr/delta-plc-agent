# KZ-20261026-001 — ISPSoft source pointer and evidence boundaries

- Durum: `STATIC_CHECKED`
- Tarih: 2026-10-26
- Baseline/hash: public core 87ed7b565cabddff0394e2cee5546579e7998e08
- Requirement/ADR: user-authorized ISPSoft MCP consolidation and GitHub source update.

## Gözlem

The public codec rejected AS v3010 projects carrying a compiled prefix. A project-local pointer-aware implementation and six neutral tests already existed; the public update adds an opaque-prefix marker-collision regression. Private Windows SDK workflows had advanced beyond the public file adapter.

## Kök neden hipotezi

The former decoder recognized only two fixed offsets. The explicit header source pointer provides a bounded alternative. Counterview: accepting any decompressible stream would conceal malformed or protected input; this implementation checks the exact version/model/prefix relationship instead.

## En küçük değişiklik

Port the two existing codec/target-match source files and neutral fixtures. Document the separate private host-layer source snapshot, rather than copying machine bindings into the universal core. No SDK executable, application program, transport setting or device authority is imported.

## Doğrulama

Seven new software fixtures cover round-trip source integrity, compiled-prefix preservation, CRC/length/pointer corruption, source protection, target mismatch and Unicode ST editing. Existing file-adapter tests must also pass. Native IDE/bench/FAT remain NOT_RUN here. Local SDK success is narrower than native IDE or behavioral equivalence; the NoSIM binary regression stays unresolved. Independent final review is required after tests and docs.

## Rollback

Revert this scoped commit; retain source manifests and original references. The expected former behavior is explicit unsupported-container rejection.

## Sonuç

Bounded file compatibility is delivered without upgrading its evidence class. Source-profile migration is recorded separately from full native/toolchain qualification.

## Genellenebilir ders

Use exact container metadata and negative fixtures rather than scanning. Identical source text or zero compiler diagnostics do not prove identical execution bytes, full retained layout or physical behavior. The observed AS228 pointer layout is not evidence for every series or protected format.
