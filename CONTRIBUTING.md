# Contribute to PLCWeave

Bring a reproducible improvement: an adapter, exact-model qualification, protocol regression, example or translation.

1. Read [AGENTS.md](AGENTS.md), [INDEX.md](INDEX.md) and [the feature map](docs/FEATURES.md).
2. Open an issue with the exact model, tool version, intended behavior and a neutral reproduction.
3. Work in an isolated branch. Keep addresses, credentials, customer programs and live enrollment in external project profiles.
4. Install with `python tools/install.py`, then install test dependencies with `python -m pip install -c requirements-lock.txt -r backends/ax/requirements-dev.txt`. Run `python -m unittest discover -s tests -t . -v`, `python -m unittest discover -s tests/dvpserial -v` and `python tools/demo.py`.
5. Explain the change and tests; distinguish software, native IDE and hardware evidence.

For a new vendor, implement `capabilities()` and `invoke(operation, arguments)`, register the `universal_plc.adapters` entry point, and add exact target rules and rejection tests. Start offline; qualify native/online operations separately.

Preserve upstream license notices. Contributions are submitted under MIT; share only material you have the right to contribute. See [SECURITY](SECURITY.md) for sensitive findings.
