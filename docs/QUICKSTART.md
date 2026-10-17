# Your first Delta PLC Agent workflow

## Install

Use Python 3.11+ in an activated virtual environment. In PowerShell, activate with `.venv\Scripts\Activate.ps1`; on Linux/macOS use `source .venv/bin/activate`. From the repository root run `python tools/install.py` for Universal plus AX, or add `--profile dvp` for Universal/DVP/ISPSoft only. Installation downloads pinned Python dependencies. Vendor IDEs remain host-installed tools.

Run `python tools/demo.py` for an offline IL → MPU → IL roundtrip with target selection, workspace isolation and evidence. Run `universal-plc doctor` for a read-only inventory and `universal-plc catalog` for model/toolchain mappings.

## Connect an MCP client

Replace `python` with the absolute path to **your installed environment's interpreter**. Choose an absolute writable `--root` path when the client has no fixed working directory.

```json
{
  "mcpServers": {
    "delta-plc-agent": {
      "command": "python",
      "args": ["-m", "universal_plc", "--root", "./plc-agent-data", "serve"]
    }
  }
}
```

For project import, add `--allow-read` followed by the chosen source directory **before** `serve`. The server copies hash-checked files into its workspace. Existing `universal-plc`, `universal-plc-mcp`, and AX compatibility commands remain stable.

## A first agent prompt

> Read AGENTS.md and INDEX.md. Resolve Delta DVP14SS211R using ISPSoft for a file transformation. Inspect capabilities, create an isolated workspace, and produce a supported IL/MPU roundtrip with evidence. Keep this task offline.

Then explore a real project with its exact model, firmware, toolchain and operation. `target.resolve` selects the route; `capabilities.list` separates implementation from qualification. See [the feature map](FEATURES.md).

## Native engineering and DVP jobs

The [AX backend guide](../backends/ax/README.md) covers ScriptEngine configuration, profiles and workers. Native AX/WPLSoft/ISPSoft steps run on a host with the corresponding Windows IDE. Python file tools and the demo run independently. A remote agent can launch stdio over its own SSH transport to that host.

Use the [DVP runbook](DVP_AGENT_RUNBOOK.md), [job template](../examples/dvp-job.json) and [workflow](../workflows/dvp-final.json). WPLSoft editing/compile/download uses Computer Use; the serial MCP handles profile-bound observation. Signals and authority belong to the current external job profile.
