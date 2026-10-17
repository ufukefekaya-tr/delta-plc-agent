"""Build both wheels and test them from a fresh, unrelated installation directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


CHECK = r'''
import asyncio, hashlib, importlib.resources, json, os, pathlib, sys
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from universal_plc.registry import Registry
import universal_plc
root = pathlib.Path.cwd()
assert pathlib.Path(universal_plc.__file__).is_relative_to(pathlib.Path(sys.prefix))
include_ax = os.environ.get('PLC_SMOKE_PROFILE', 'all') == 'all'
if include_ax:
    from axmcp_offline.universal_adapter import AxAdapter
    import axmcp_offline
    assert pathlib.Path(axmcp_offline.__file__).is_relative_to(pathlib.Path(sys.prefix))
else:
    import importlib.util
    assert importlib.util.find_spec('axmcp_offline') is None
for resource in ('data/catalog.json','data/ax-models.json','data/workflows.json','delta_files/_vendor/LICENSE-ispsoft-codec.txt'):
    assert importlib.resources.files('universal_plc').joinpath(resource).is_file(), resource
if include_ax:
    assert importlib.resources.files('axmcp_offline').joinpath('ax_worker/project_factory.py').is_file()
    assert AxAdapter().invoke('environment.probe', {})
target={'vendor':'delta','family':'DVP','model':'DVP12SE11R','toolchain':'ispsoft'}
source=root/'inputs'/'neutral.il'; source.parent.mkdir(); source.write_text('LD,X0\nOUT,Y0\nEND\n',encoding='utf-8')
async def run():
    params=StdioServerParameters(command=sys.executable,args=['-m','universal_plc','--root',str(root/'state'),'--allow-read',str(source.parent),'serve'],env=dict(os.environ),cwd=str(root))
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            names={t.name for t in (await session.list_tools()).tools}
            assert len(names)==11, names
            async def call(name,args):
                response=await session.call_tool(name,args)
                assert not response.is_error, response
                return response.structured_content
            route=await call('target.resolve',{'target':target})
            assert route['adapter_id']=='delta.ispsoft'
            workspace=await call('workspace.create',{'target':target})
            wid=workspace['workspace_id']
            await call('workspace.import',{'workspace_id':wid,'source_path':str(source),'destination':'neutral.il','expected_sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
            async def invoke(operation, arguments):
                return await call('adapter.invoke',{'workspace_id':wid,'operation':operation,'arguments':arguments,'expected_context_digest':workspace['context_digest']})
            first=await invoke('dvp.il_to_mpu',{'input_path':'neutral.il','output_path':'neutral.mpu'})
            second=await invoke('dvp.mpu_to_il',{'input_path':'neutral.mpu','output_path':'readback.il'})
            assert not first['result']['vendor_compile'] and not second['result']['vendor_compile']
            record=await call('evidence.read',{'workspace_id':wid,'evidence_id':second['evidence_id']})
            assert record['result']['evidence_kind']=='FILE_TRANSFORM'
            text=(root/'state'/'workspaces'/wid/'files'/'readback.il').read_text('utf-8')
            assert 'X0' in text and 'Y0' in text
            return sorted(names)
print(json.dumps({'status':'PASS','kind':'SOFTWARE_ONLY','tools':asyncio.run(run()),'wheel_imports':True,'bundled_resources':True,'ax_replay':include_ax,'ax_absent':not include_ax,'stdio_file_roundtrip':True,'source_checkout_on_pythonpath':False}))
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', choices=('all', 'dvp'), default='all')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    environment = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'PYTHONHOME'} and not k.startswith(('AXMCP_', 'UNIVERSAL_PLC_'))}
    environment['PLC_SMOKE_PROFILE'] = args.profile
    def run(command, cwd, timeout=300):
        result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout
    with tempfile.TemporaryDirectory(prefix='PLC taşınabilir test ') as temporary:
        root = Path(temporary); wheels = root/'wheels'; wheels.mkdir()
        for package in ((repo, repo/'backends'/'ax') if args.profile == 'all' else (repo,)):
            run([sys.executable,'-m','pip','wheel','--no-deps','--wheel-dir',str(wheels),str(package)], root)
        venv.EnvBuilder(with_pip=True).create(root/'venv')
        python = root/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        wheel_paths = sorted(wheels.glob('*.whl'))
        run([str(python),'-m','pip','install','-c',str(repo/'requirements-lock.txt'),*(str(p) for p in wheel_paths)],root)
        unrelated = root/'unrelated'; unrelated.mkdir()
        check = unrelated/'smoke.py'; check.write_text(CHECK,encoding='utf-8')
        output = run([str(python),'-I',str(check)],unrelated)
        result = json.loads(output.strip().splitlines()[-1])
        result['wheels'] = [{'name':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in wheel_paths]
        result['python'] = sys.version.split()[0]
        result['platform'] = sys.platform
        text = json.dumps(result,ensure_ascii=False,indent=2)+'\n'
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(text,encoding='utf-8',newline='\n')
        print(text)


if __name__ == '__main__':
    main()
