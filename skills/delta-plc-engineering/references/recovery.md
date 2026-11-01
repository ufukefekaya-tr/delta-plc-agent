# Recovery and evidence

After interruption, verify source/task/revision, target identity, tool versions,
workspace digest and the last durable operation intent/result. Read job or
workflow status and relevant evidence before deciding what to do next.

An unfinished intent is unknown_outcome. Do not issue the operation again merely
because the client timed out. Use its typed read-only reconciliation path. A new
command ID, workspace, process or agent does not erase the pending outcome or
grant new device authority. Never convert a read-only request into mutation.

Read-only annotations alone do not prove a host-only operation: AX observation
read/stop or deployment reconciliation can enqueue a target worker. Respect the
pending-transfer guard and network scope. With an unknown target, begin with
known host-local job/workspace/evidence records; do not sweep or reconnect to
targets, cancel a transfer or call observe.stop as an automatic shortcut.

Preserve baseline bytes and publish a corrected candidate under a new output
identity. Stop the affected operation on target/source/profile drift. Keep valid
unrelated diagnostics available when their own contracts are known.

Record operation, package/tool/profile identity, input/output hashes and actual
result. Distinguish SOFTWARE_ONLY, FILE_TRANSFORM, vendor build, simulation and
device/physical evidence. Missing/native/unrun checks remain visible. Generic
lessons may be retained; customer artifacts, tags, recipes and authority records
do not enter this shared skill.
