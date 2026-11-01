# Synthetic digital bench provenance

`digital-bench.json` is generated only by `tests/dvpserial/synthetic_bench.py`.
The generator takes no customer or native-project input. Its eight digital lanes,
four banks, register layout, command names and measurements are test inventions.
Only reusable protocol, pulse, fault, revocation and recovery obligations from
the existing software tests are retained. No customer catalog fields or native
bytes are copied. This fixture never qualifies a PLC or a deployment profile.

The former inherited thermal fixture is removed from the current tree and new
artifacts. Historical release assets and Git history are not rewritten.
