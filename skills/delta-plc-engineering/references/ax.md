# AX engineering

Route an exact AX model through the registered AX adapter and discover its
current capability set. The tested AX package is 0.4.0: 24 standalone autonomous
tools, five replay tools, and an additional Universal project.create operation.
These are different interfaces, not thirty common MCP tools.

Preserve project creation, ST/Ladder editing, generic LadderRecipe v2, explicit
output allowlists, compilation diagnostics by category, native revision batches,
source readback, immutable uncompiled snapshots, job progress, artifacts and
deployment/observation/Stop operations. Read actual operation schemas before
calling them. A baseline clone is not project.create. A saved uncompiled revision
is neither a build nor a frozen deployment candidate.

Bindings for bench/OPC UA controls belong to the user's explicit project profile.
Do not invent symbols or copy an example operator profile into a real machine.
Synthetic operator evidence does not establish physical movement or acceptance.
Native work requires the correct Windows IDE/ScriptEngine/device profile; a
Linux file-only installation does not magically provide that IDE.

Use matching typed standalone tool schemas for their operations. When using the
Universal dispatcher, verify the corresponding versioned adapter argument
contract; capability names cannot supply that contract. Keep existing commands
and qualified extensions available instead of reducing AX to file-only recipes.

After a transfer intent, timeout or lost response, inspect job/session ownership
and reconcile the real outcome. Do not automatically kill, close, logout, retry
download or issue RUN. LAN/DIACloud operations share the bounded job budget.
Heartbeat is not transfer progress; unknown percentage stays unknown. Stopping
observation is distinct from stopping the PLC. A request to stop a live PLC uses
its authorized typed Stop path, not the observation-stop operation.
