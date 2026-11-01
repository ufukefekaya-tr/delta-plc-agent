"""Actual stdio recipe/argument contract, source isolation and evidence tests."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from mcp import Client, StdioServerParameters
from .test_skill_package import ROOT,package


class SkillStdioTests(unittest.TestCase):
    def test_recipe_against_real_connected_schema_and_file_roundtrip(self):
        async def run():
            with tempfile.TemporaryDirectory(prefix='PLC skill \u00f6rnek ') as d:
                root=Path(d).resolve();source=root/'neutral.il';source.write_text('LD,M0\nOUT,M1\n')
                env={k:v for k,v in os.environ.items() if not k.startswith('UNIVERSAL_PLC_')}
                env['PYTHONPATH']=str(ROOT/'src')
                params=StdioServerParameters(command=sys.executable,args=['-B','-m','universal_plc','--root',str(root/'runtime'),'--allow-read',str(root),'serve'],cwd=root,env=env)
                async with Client(params,read_timeout_seconds=20) as client:
                    listing=await client.list_tools()
                    contracts={t.name:package.schema_contract(t.model_dump(by_alias=True)['inputSchema']) for t in listing.tools}
                    manifest=json.loads((package.SKILL/'references/compatibility.json').read_text())
                    self.assertEqual(set(contracts),set(manifest['common_tools']))
                    inventory=package.installed_inventory(manifest)
                    self.assertEqual(package.check_recipe(manifest,'dvp.il_to_mpu',inventory['core_version'],inventory['module_sha256'],contracts)['status'],'COMPATIBLE')
                    async def call(name,args):
                        r=await client.call_tool(name,args);self.assertFalse(r.is_error,(name,r));return r.structured_content
                    target=dict(vendor='delta',family='DVP',model='DVP14SS211R',toolchain='ispsoft')
                    route=await call('target.resolve',{'target':target});self.assertEqual(route['adapter_id'],'delta.ispsoft')
                    ws=await call('workspace.create',{'target':target,'task_id':'neutral-skill-recipe','revision':'r1'})
                    wid=ws['workspace_id'];ctx=ws['context_digest']
                    await call('workspace.import',dict(workspace_id=wid,source_path=str(source),destination='neutral.il',expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest()))
                    for op,inp,out in [('dvp.il_to_mpu','neutral.il','neutral.mpu'),('dvp.mpu_to_il','neutral.mpu','readback.il')]:
                        result=await call('adapter.invoke',dict(workspace_id=wid,operation=op,arguments={'input_path':inp,'output_path':out},expected_context_digest=ctx))
                        record=await call('evidence.read',dict(workspace_id=wid,evidence_id=result['evidence_id']))
                        self.assertEqual(record['result']['evidence_kind'],'FILE_TRANSFORM')
                        self.assertFalse(record['result']['vendor_compile']);self.assertFalse(record['result']['live_execution'])
                    self.assertEqual((root/'runtime/workspaces'/wid/'files/readback.il').read_text().splitlines(),source.read_text().splitlines())
                    self.assertEqual(source.read_text(),'LD,M0\nOUT,M1\n')
        asyncio.run(run())


if __name__=='__main__':unittest.main()
