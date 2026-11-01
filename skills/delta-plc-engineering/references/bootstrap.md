# Bootstrap and scope

Establish the user's requested result and current authorization. Record exact
vendor, family, model, firmware if known, toolchain/version, project format,
source digest, task and revision. Do not assume AX, infer an exact model from a
family name or substitute a nearby supported model. A project/model conflict
must be resolved before mutation.

If MCP is absent, explain which configured server is needed and provide setup
guidance. Do not invent a successful call, automatically install packages or
silently fall back to a machine-specific launcher. The runtime command is
`python -m universal_plc --root <chosen writable data root> serve` using the
interpreter on the chosen host. An import root is explicitly selected with
`--allow-read <source directory>` before `serve`. Paths are host configuration,
not values to copy from someone else's workstation. Choose an absolute data root
when the client has no fixed working directory. Do not edit global configuration
without a task that includes configuration changes.

The eleven common tools are environment.probe, target.resolve,
capabilities.list, workspace.create/status/import, adapter.invoke, evidence.read
and workflow.start/run/status. Client namespaces can prefix these names; inspect
the actual connected catalog instead of constructing a client-specific name.

Verify that the MCP initialization version, installed distribution version and
loaded module identity agree. A source override or editable checkout must not
hide older installed metadata. Refresh a stale client connection before treating
its cached tool count as the current inventory; do not restart unrelated apps.
Inspect capability implementation, qualification and operation-specific scope.
Creating a file workspace is not creating a native PLC project. Workspace IDs,
hashes and plans identify work; they are not permission to operate a device.
Import only selected files from authorized roots with their expected hashes.
Never supply a customer program, tag catalog, IP, COM port, credential, machine
recipe or historical authority as a built-in default for another user.

W1 selects scope and route; W2 creates a project; W3 edits an existing project;
W4 handles a supported format transition; W5 handles separately authorized
deployment/observation; W6 reconciles interruptions; W7 records results and
general lessons. The workflow tool does not implement every vendor IDE action.

For software-only work, keep IDE and device access out of scope. Native IDE work
needs matching host tools and an explicit offline project scope. Actual device
operations need the current target/revision authority accepted by the backend;
a previous project's permission is never inherited. Repeated steps within a
still-valid authorized revision do not require repetitive user confirmations.
