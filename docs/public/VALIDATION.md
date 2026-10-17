# Validation and reproducibility

Use CI results for the **exact commit** you install. The workflow runs offline suites and demo on Windows/Linux with Python 3.11/3.13. A Windows packaging job builds both wheels, installs a fresh environment and exercises real stdio MCP, file roundtrip and AX replay.

```text
python tools/install.py
python -m pip install -c requirements-lock.txt -r backends/ax/requirements-dev.txt
python -m unittest discover -s tests -t . -v
python -m unittest discover -s tests/dvpserial -v
python tools/demo.py
python tools/portable_smoke.py --profile all
```

## Evidence classes

- **Software:** protocol doubles, routing, guards, file transforms, replay and installation.
- **Native IDE:** exact IDE/model/profile, called-POU compilation and save/reopen.
- **Live:** current identity, source/revision, authority, physical conditions and readback.

These classes stay separate. Private session reports are not republished as new hardware acceptance.

## Engineering qualifications

AX native implementation and neutral historical records are in [AX qualification](../../backends/ax/qualification). Public build/freeze and live acceptance remain profile/operation-specific. ISPSoft files have software acceptance; AS native acceptance remains open. WPLSoft has the Computer Use recipe; source-session UI acceptance was blocked. Current-release DVP physical watch/M-D acceptance remains open; SS2 transfer is experimental.

[Feature map](../FEATURES.md) · [Provenance](PROVENANCE.md) · [Exact CI runs](https://github.com/ufukefekaya-tr/delta-plc-agent/actions). CI is software evidence, not commissioning or FAT/SAT.
