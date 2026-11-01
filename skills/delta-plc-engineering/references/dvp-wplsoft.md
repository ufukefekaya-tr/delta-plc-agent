# DVP and WPLSoft

Resolve the exact controller and requested file/IDE/serial operation. A .dvp
project route is not the serial transport route. The current serial model is
DVP14SS211R with toolchain=dvpserial; do not attach a project_format=.dvp to that
serial target. Keep the source hash separately in the job/workspace context.

WPLSoft editing, creation, vendor compilation, save/reopen and requested download
use the accessible vendor IDE workflow. The delta.wplsoft catalog entry does not
implement a native MCP compiler. Do not manufacture adapter.invoke calls for
unimplemented IDE actions. A file converter is not a vendor compiler.

For the tested offline IL/MPU recipe, select toolchain=ispsoft, create an isolated
workspace, import the selected UTF-8 IL with its SHA-256, then invoke
dvp.il_to_mpu with input_path/output_path relative to workspace files. Read its
evidence before dvp.mpu_to_il with a new output_path. Compare the resulting IL
and retain FILE_TRANSFORM classification. Follow the parameter contract in the
compatibility record; unsupported instructions or lossy layouts must be reported.

Serial port inventory, identity/status/read, profile-bound M/D requests and
parameters, observation and reconciliation remain available where qualified.
Command names, addresses, units, guards and signals come from the current user's
external profile. No test fixture is a deployment profile. Explicit attended
testing and isolation are distinct accepted enrollment paths for bounded profile
M/D operations; neither grants transfer or RUN/STOP authority.

When moving from IDE to serial observation, the IDE releases the port. Use the
existing verified target/revision context; a fresh workspace does not create
authority. Experimental SS2 native transfer remains separately gated. Readback
and observation are evidence of their measured values, not FAT/SAT or motion.
