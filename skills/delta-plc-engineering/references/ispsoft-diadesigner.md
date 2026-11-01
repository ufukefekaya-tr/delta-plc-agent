# ISPSoft and normal DIADesigner

AS and DVP are controller families; ISPSoft and normal DIADesigner are separate
toolchains. Use exact model/toolchain resolution and project metadata together.
Normal DIADesigner is not DIADesigner-AX.

The public ISPSoft file adapter supports bounded container inspect/unpack/pack,
existing ST/local-variable patching, library packages, IL/MPU conversion and
explicit compatible DVP-SE template generation. Model variants and source-pointer
formats are bounded by the current codec. Preserve original bytes and hashes,
use new output names, then read back source and check declarations/encoding.

File operations return FILE_TRANSFORM with vendor_compile=false and
live_execution=false. Updating source while retaining a compiled prefix makes
that prefix stale. Opening, saving, repacking or a zero-error unused POU does not
prove that the changed called program compiled. A native build claim requires a
real called-POU syntax-negative followed by a corrected build on the same profile.

Separate AS engineering and native development layers may exist on a host.
Their 27/43 tool inventories are not part of the public eleven-tool core and do
not make every listed operation qualified. Do not depend on a private repository
or an existing machine source profile. If unavailable, report the exact missing
capability; never switch to a private launcher or project.

Reusable source-export/preflight/journal/build-comparison improvements are tracked
in separate integration goals. Native hardware companions, retained layout and
old/current binary equivalence require their own evidence. Preserve unsupported
operation and language restrictions for the exact IDE; do not invent FUNCTION
POU support in a profile that only supports PROGRAM and FB. Hardware configuration
follows the matching vendor IDE workflow, retaining its companion files.

An executed SDK experiment is not automatically main-IDE CompileAll, another
controller model, deployed byte identity, full retained ABI or physical acceptance.
