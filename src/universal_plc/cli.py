from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .environment import probe
from .models import PLCError
from .service import Service


def main() -> int:
    parser = argparse.ArgumentParser(description="Portable vendor-neutral PLC agent host")
    parser.add_argument("--root", type=Path, default=Path(".agent-state/universal-runtime"))
    parser.add_argument("--allow-read", action="append", type=Path, default=[])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Read-only host inventory, no IDE or PLC connection")
    commands.add_parser("catalog", help="Read portable model/tool mappings")
    commands.add_parser("serve", help="Run the stdio MCP server")
    for name in ("resolve", "capabilities"):
        command = commands.add_parser(name)
        command.add_argument("--target", required=True, help="TargetProfile as JSON")
    call = commands.add_parser("call", help="Call a bounded service method with JSON arguments")
    call.add_argument("method", choices=["workspace_create", "import_file", "invoke", "evidence_read", "workflow_start", "workflow_run", "workflow_status"])
    call.add_argument("--arguments", required=True)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            result = probe()
        else:
            service = Service(args.root, read_roots=args.allow_read)
            if args.command == "serve":
                from .mcp_server import create_server
                create_server(service).run(transport="stdio")
                return 0
            if args.command == "catalog":
                result = service.registry.document
            elif args.command in {"resolve", "capabilities"}:
                result = getattr(service, args.command)(json.loads(args.target))
            else:
                result = getattr(service, args.method)(**json.loads(args.arguments))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PLCError, OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc), "status": "rejected"}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
