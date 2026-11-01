# KZ-20261101-001 — Companion identity and fixture provenance

- Baseline/hash: public source `0584d9d1f85b8ec7f59cbf7c20c70b9e0fd365c1`.

## Gözlem

An agent can load current source while its installed distribution
still reports an older version. An inherited test fixture can also retain a
customer-derived layout after names and hashes are anonymized.

Expected/actual: a portable public skill must use the actual installed engine
and carry no customer files or derived snapshots. Source-only execution and
renaming a fixture were insufficient acceptance criteria.

## Kök neden hipotezi

Source identity, package identity, test data provenance
and native qualification were treated as interchangeable summaries.

- 10. Adam karşı görüşü: replacing the MCP or importing a whole newer local checkout would
create additional regression and privacy risk. The execution engines already
matched the reviewed public source.

## En küçük değişiklik

Keep the engines; add an optional self-contained skill, bind
recipes to versions/argument contracts/module identities, reject installed/source
skew, and author a new input-free eight-lane synthetic bench. Record local AS
reuse separately rather than assuming its private profile is universal.

## Doğrulama

153 DVP tests pass, 14 targeted package/goal/stdio checks pass,
and the full 498-test run passes with one Windows symlink skip. Counts overlap.
Independent Astra forward testing observed the old-version block and a fresh
installed-wheel IL/MPU roundtrip. See the
[validation record](../docs/public/SKILL_0_4_VALIDATION.json) and
[behavior review](../docs/plans/plc-skill/BEHAVIOR_REVIEW.json).

Final review also found that a hash-list file alone does not bind its underlying
inputs, and a Git identifier alone does not bind an archive built from working
files. Goals now pin actual inputs/outputs and dependencies; archives read exact
Git blobs. Negative drift and newline/working-copy tests cover both corrections.

## Rollback

Remove the optional skill or revert only these source commits; do not
overwrite customer environments, historical assets or device state.

## Sonuç

- Yanlış genelleme sınırı: source hashes, catalogs and tool counts are not native
or physical acceptance. Read-only annotations do not guarantee host-only AX
operations: observation/reconciliation may enqueue a target worker. Unknown
transfers need scoped outcome reconciliation before target actions.
