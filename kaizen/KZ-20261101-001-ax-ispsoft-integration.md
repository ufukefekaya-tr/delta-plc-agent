# KZ-20261101-001 — Preserve independent AX and ISPSoft changes during merge

- Durum: `STATIC_CHECKED`
- Tarih: 2026-11-01
- Baseline/hash: AX `7fcf209f16b1aad1d5bcf9ee66f3b3b50347dd3c`; ISPSoft main `f4f041ab4212aca9afa4c66d745f551bfe431a10`.
- 10. Adam karşı görüşü: Taking either complete manifest or index could silently omit the other branch's delivered features or evidence.
- Yanlış genelleme sınırı: Repository integration and software tests provide no new IDE or physical PLC acceptance.

## Gözlem

AX 0.4 PR #1 passed its checks but was deliberately left open before merge
authorization. Independent ISPSoft changes then advanced main. The shared source
manifest and Kaizen index conflicted; each branch also allocated KZ-20261026-001.

## Kök neden hipotezi

Independent writers updated shared publication metadata from the same baseline.
The conflict is in records, while AX and ISPSoft runtime edits are disjoint.

## En küçük değişiklik

Merge current main into the AX branch without rewriting either history. Preserve
AX 0.4 and all ISPSoft runtime/docs/test bytes. Regenerate the public manifest
from the integrated index, retain both provenance fields, and keep both index
entries. The AX lesson becomes KZ-20261026-002 with an explicit history reference;
ISPSoft keeps its existing main identity.

## Doğrulama

Compare the complete AX runtime and ISPSoft/DVP runtime against their owning
parents. Run combined offline suites, clean wheel/stdio smoke, repository and
manifest/privacy checks, then independent review and GitHub CI before merging.
Current integration results are recorded in the
[public AX validation record](../docs/public/AX_0_4_VALIDATION.md).
Native IDE, live PLC and skill-package execution are NOT_RUN in this task.

## Rollback

Preserve both parents and the merge commit. If reversal is required, use an
explicit scoped revert preserving subsequent changes; never force-push or erase
history. No PLC state is modified by this integration.

## Sonuç

The merge joins existing feature sets. Validation, current merged SHA and any
remaining acceptance are reported separately from historical release evidence.
