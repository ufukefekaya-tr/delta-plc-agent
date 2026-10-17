# AX workflow and transferable lessons

The public source keeps transferable AX engineering lessons. See [public provenance](../../../docs/public/PROVENANCE.md) for the distribution boundary. Historical grants are never authority for a new project.

1. Resolve exact installed IDE profile, executable, ScriptEngine, libraries and
   device description. Pin digests; a model label alone is insufficient.
2. Create an isolated project or a digest-locked baseline workspace. Retain the
   original. Never reuse machine communication routes or operational authority.
3. Use the actual installed ScriptEngine signatures. Version differences in
   create_pou/create_gvl and exports must be detected, not guessed.
4. Run native build with the correct runtime initialization. In the recorded AX
   1.9 setup a hidden full runtime initialized the OEM compiler correctly while
   `--noUI` produced a compiler null reference. This is version-specific evidence,
   not a rule for every future release.
5. Save, close and reopen; inspect the called POU and task mapping, then compile.
   A deliberate error in an uncalled POU does not prove the compiler pipeline.
6. Preserve all diagnostics. Optional device placeholders must not be filled
   indiscriminately: mutually exclusive modules may generate real errors. A
   historical warning exemption was bound to exact project bytes and is not a
   universal allowance.
7. Compare native exports semantically. GUID changes may be incidental, but
   topology, declaration, I/O, task and symbol changes require explicit review.
8. Treat UTF-8/BOM parsing as a tested boundary. ScriptEngine's older XML readers
   may behave differently; the host and Python-2 worker must agree on semantics.
9. For observation, resolve the exact namespace, node identifiers and signal
   publication of the user project. A remembered namespace or GVL is not a
   binding. Session/run state must be read rather than inferred from connection.
10. Before deployment bind artifact, target fingerprint, rollback digest, expiry
    and single-use authority. An unknown outcome requires read-only reconciliation
    rather than another download. A lost connection does not prove rollback.
11. Stop owns the end of every bounded movement operation. A queued motion must
    not survive a newer Stop generation; cancellation must retain the stop latch
    until acknowledged. Transport recovery does not prove physical standstill.
12. Keep learning records scoped: observation, root cause, change, regression,
    evidence, rollback and applicability. A single AX308 bench proof does not
    certify all AX models, firmware, APIs or other machines.

## Current qualification boundary

The inherited target adapter can inspect AX308/SP18 identity and runtime markers,
but has no independently qualified physical-output-isolation observer. Its
approval set remains empty and the deployment launcher rejects unapproved code.
This is a real open dependency, not a completed online capability. Other AX
models require versioned identity profiles and independent acceptance.

The generic bench transport retains an optional application-level command
contract. It does not expose arbitrary node writes and is not enabled by naming
a model. Bindings, state values and fresh fingerprint observation are mandatory.
