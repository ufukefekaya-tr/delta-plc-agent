# KZ-20261026-001 — Preserve public behavior when importing AX improvements

- Baseline/hash: `87ed7b565cabddff0394e2cee5546579e7998e08`
- 10. Adam karşı görüşü: A newer private package may erase public generalization; compare behavior, not version alone.
- Yanlış genelleme sınırı: The reference evidence profile is synthetic; no live target qualification is inherited.

## Gözlem

The public backend and the internal engineering line had diverged. Copying the
newer package wholesale would remove generic LadderRecipe v2, explicit bench
bindings and category-aware diagnostics, and expose private machine defaults.

## Kök neden hipotezi

Source version alone is not a safe integration criterion.

## En küçük değişiklik

A three-way comparison
of shared code preserved public extensions while importing transfer protection,
progress, native revisions and pure evidence checks. Project creation and the
DVP/ISPSoft adapters remain independent. The legacy bound bench implementation
already contained Stop priority and pulse-release fixes and was kept unchanged.

## Doğrulama

Regression checks exercise both public-specific behavior and imported fixes.
The common adapter must dispatch every advertised operation, not only list it.
Manifest hashes bind the published tree; private reports are not public live
evidence. Release tests and remaining acceptance are in the
[0.4 release record](../backends/ax/docs/RELEASE_0_4.md).

## Rollback

Rollback is a scoped Git revert of the public update, without rewriting history
or changing a PLC. No live system is involved in this publication.

## Sonuç

The reusable AX changes and public-specific regression sentinels are delivered
together. Software acceptance and live acceptance remain separate.
