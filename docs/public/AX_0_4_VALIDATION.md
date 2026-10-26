# AX 0.4 public integration validation

Date: 2026-10-26. Public baseline: `87ed7b565cabddff0394e2cee5546579e7998e08`.
Scope: offline software publication; no IDE, PLC or HMI operation.

| Check | Result |
|---|---|
| Unified repository suite, Windows/Python 3.14 | 477 tests, 476 passed, 1 skipped, 0 failures/errors; 109.400 s |
| Separate DVP serial suite | 153 passed; 72.095 s |
| Focused revision/worker/public-routing checks | 98 passed; 8.765 s |
| Fresh wheel installation without source checkout on PYTHONPATH | PASS, Universal and AX wheels |
| Installed AX stdio list/call | PASS: 24 autonomous tools, 5 replay tools |
| Universal stdio file roundtrip and AX replay | PASS, 11 common tools |
| Demo, repository, bootstrap and diff checks | PASS |
| Public indexed-file digest/privacy manifest | PASS |
| Real vendor compile, native LD replacement, PLC download/RUN/watch/Stop | NOT_RUN in this publication |

AX wheel: `universal_plc_axmcp-0.4.0-py3-none-any.whl`.
SHA-256: `5572f6e943d5563be9dee0ec800a94eeb90adcd17216af8bae28162bc7d793e8`.
Universal wheel remains 0.3.1; its DVP/ISPSoft runtime was not changed.

Installed stdio tests create a synthetic scratch workspace, query its state,
read the synthetic operator profile, and reject unknown progress, missing evidence,
empty revision batches and unsaved candidates. No target is enrolled or contacted.
The full suite additionally exercises protected transfers, deadlines, native
transaction faults, public Ladder v2, project creation and category diagnostics.

The first merge run exposed integration mistakes (exception declarations,
synthetic test constants/imports and operation registry order). They were repaired;
a following run found only two text newline violations, now corrected. The final
suite above is the passing rerun, not the earlier failed result.

Source provenance and exclusions are described in [PROVENANCE](PROVENANCE.md).
Runtime behavior and acceptance limits are in the [AX release record](../../backends/ax/docs/RELEASE_0_4.md).
CI results should always be checked against the exact installed commit.

The first GitHub run passed Linux and portable packaging but exposed a test-only
Windows path comparison: TEMP used a short path while the verified result used
its canonical long path. The assertion now checks canonical path and file identity;
runtime behavior and the AX wheel are unchanged. This correction is rechecked by CI.

Independent read-only review: **ACCEPT for offline publication**, with no open
critical or major finding. Public-specific behavior, transfer protection, new
dispatch paths, privacy boundaries and negative tests were reviewed. Installation
wording was clarified during review. This is not native or live acceptance.
