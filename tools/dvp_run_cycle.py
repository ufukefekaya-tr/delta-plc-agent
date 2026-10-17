"""Explicit attended MCP RUN -> temperature read -> STOP cycle, no download."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
TARGET = dict(vendor='delta', family='DVP', model='DVP14SS211R', toolchain='dvpserial')


async def cycle(profile: Path, output: Path):
    if output.exists():
        raise ValueError('Evidence output already exists; choose a new path')
    env = os.environ.copy()
    env['PYTHONPATH'] = os.pathsep.join([str(ROOT/'src'), str(ROOT/'.agent-state/deps'), env.get('PYTHONPATH', '')])
    env['UNIVERSAL_PLC_DVPSERIAL_CONFIG'] = str(profile.resolve())
    params = StdioServerParameters(command=sys.executable,
        args=['-B', '-m', 'universal_plc', '--root', str(ROOT/'.agent-state/dvpserial-runtime'), 'serve'],
        cwd=str(ROOT), env=env)
    report = dict(started_at=datetime.now(timezone.utc).isoformat(), target=TARGET,
                  current_plc_program=True, program_downloaded=False, calls=[], stop_verified=False)
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2)+'\n').encode())
    save()
    async with Client(params, read_timeout_seconds=40) as client:
        async def call(name, arguments):
            result = await client.call_tool(name, arguments)
            if result.is_error or result.structured_content is None:
                raise RuntimeError(f'MCP failure {name}: {result.content}')
            data = result.structured_content
            report['calls'].append(dict(tool=name, result=data)); save()
            return data
        workspace = await call('workspace.create', dict(target=TARGET, task_id='dvpserial-run-cycle', revision='current-plc-program'))
        wid = workspace['workspace_id']; report['workspace_id'] = wid
        async def workflow(kind, operations):
            started = await call('workflow.start', dict(workspace_id=wid, workflow=kind))
            result = await call('workflow.run', dict(workspace_id=wid, run_id=started['run_id'],
                steps=[dict(operation=op, arguments={}) for op in operations]))
            for completed in result.get('completed', []):
                await call('evidence.read', dict(workspace_id=wid, evidence_id=completed['evidence_id']))
            return [item['result'] for item in result.get('completed', [])]
        try:
            running = await workflow('W5', ['target.run'])
            report['run_result'] = running[0] if running else None
            save()
            if not running or not running[0].get('state_verified') or running[0].get('running') is not True:
                raise RuntimeError('RUN not verified; proceeding directly to STOP cleanup')
            print('MCP RUN verified; reading current program temperatures', flush=True)
            await asyncio.sleep(2)
            readings = await workflow('W5', ['target.status', 'target.status'])
            report['readings'] = readings
            print(json.dumps(dict(readings=[dict(running=r.get('running'), capacity_steps=r.get('capacity_steps'),
                errors=r.get('error_codes')) for r in readings])), flush=True)
            save()
        finally:
            # STOP is a separately authorized cleanup operation, attempted even
            # when RUN ACK/reading is uncertain. RUN is never automatically replayed.
            try:
                stopped = await workflow('W5', ['target.stop'])
                report['stop_result'] = stopped[0] if stopped else None
                verified = await workflow('W5', ['target.status'])
                report['final_status'] = verified[0] if verified else None
                report['stop_verified'] = bool(verified and verified[0].get('ok') and verified[0].get('running') is False)
                print(json.dumps(dict(stop_verified=report['stop_verified'])), flush=True)
            finally:
                await workflow('W6', ['target.disconnect'])
                report['finished_at'] = datetime.now(timezone.utc).isoformat(); save()
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    result = asyncio.run(cycle(args.profile, args.output))
    return 0 if result['stop_verified'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
