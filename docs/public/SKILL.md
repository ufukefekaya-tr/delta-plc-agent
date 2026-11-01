# Optional Delta PLC engineering skill

The MIT [skill folder](../../skills/delta-plc-engineering/SKILL.md) adds reusable
engineering workflow to the existing MCP. Install that complete directory in
your agent's supported skill location, or extract the standalone skill ZIP.
Keep its LICENSE and references together. Configure the MCP independently using
the interpreter and project/data roots on your host. The skill does not install
vendor tooling or change global agent configuration.

The entrypoint loads only the guide relevant to AX, ISPSoft/normal DIADesigner
or DVP/WPLSoft. Its compatibility record distinguishes installed package
identity, connected tool schemas and operation-specific argument contracts.
Installed metadata must agree with loaded source; a source-path override is not
a substitute for the matching installed release.

Current package identities: **core 0.4.0, AX 0.4.0, skill 1.0.0**. Existing MCP
commands and operation paths remain available without this skill. A missing or
incompatible recipe does not remove unrelated known features or diagnostics.
The public core does not silently acquire private AS engineering tools.

Validate the standalone directory with `python tools/skill_package.py` in the
source repository. The builder uses an explicit member allowlist, reproducible
ZIP entries, an exact source-tree identity and per-file hashes. It never copies
the developer environment, user projects, enrolled profiles or credentials.

Use `python tools/skill_goals.py` to inspect phase dependencies and current
evidence. `--reconcile` explicitly marks input-drift-affected goals STALE; it does
not execute PLC operations or automatically rewrite an engineering plan.
See the [reviewed implementation plan](../plans/plc-skill/PLAN.md).

S0 is the existing MCP plus privacy correction and companion. S1 separately
tracks reusable AS integration; S2 tracks native qualification. Completing S0
never implies that those additional slices are implemented or qualified.
