from __future__ import annotations
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'tools'/f'{name}.py')
    obj=importlib.util.module_from_spec(spec);spec.loader.exec_module(obj);return obj
package=module('skill_package');goals=module('skill_goals')


class SkillPackageTests(unittest.TestCase):
    def test_detached_archive_is_reproducible_and_all_references_survive_without_checkout(self):
        with tempfile.TemporaryDirectory(prefix='detached PLC \u0131\u015f ') as d:
            d=Path(d).resolve();a=d/'first.zip';b=d/'second.zip'
            tree=subprocess.check_output(['git','write-tree'],cwd=ROOT).decode().strip()
            one=package.build(package.SKILL,a,tree);two=package.build(package.SKILL,b,tree)
            self.assertEqual(one['sha256'],two['sha256'])
            with zipfile.ZipFile(a) as z:
                self.assertEqual(len(z.namelist()),9)
                self.assertTrue(all(n.startswith('delta-plc-engineering/') for n in z.namelist()))
                z.extractall(d/'different-root')
            root=d/'different-root/delta-plc-engineering'
            with patch.object(package.subprocess,'check_output',side_effect=AssertionError('Detached validation must not call Git')):
                self.assertEqual(package.validate(root)['status'],'PASS')
            (root/'references/bootstrap.md').write_text('tampered')
            with self.assertRaises(ValueError):package.validate(root)
            with self.assertRaises(ValueError):package.build(package.SKILL,a,tree)

    def test_skill_build_rejects_fake_identity_and_uses_tree_bytes_after_working_copy_changes(self):
        with self.assertRaises(ValueError):package.build(package.SKILL,Path('unused.zip'),'1'*40)
        with tempfile.TemporaryDirectory() as d:
            repo=Path(d).resolve();root=repo/'skills/delta-plc-engineering';shutil.copytree(package.SKILL,root)
            subprocess.check_call(['git','init','-q'],cwd=repo)
            subprocess.check_call(['git','-c','core.autocrlf=false','add','--','skills'],cwd=repo)
            tree=subprocess.check_output(['git','write-tree'],cwd=repo).decode().strip()
            pinned=subprocess.check_output(['git','show',tree+':skills/delta-plc-engineering/LICENSE'],cwd=repo)
            (root/'LICENSE').write_bytes(pinned.replace(b'\n',b'\r\n'))
            (root/'references/recovery.md').write_text('UNSTAGED MODIFICATION')
            archive=repo/'skill.zip';package.build(root,archive,tree,repository=repo)
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(z.read('delta-plc-engineering/LICENSE'),pinned)
                self.assertNotIn(b'UNSTAGED',z.read('delta-plc-engineering/references/recovery.md'))

    def test_extra_customer_shaped_file_and_escaping_reference_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve()/'skill';shutil.copytree(package.SKILL,root)
            (root/'private.isp').write_bytes(b'UNRELATED_CANARY')
            with self.assertRaises(ValueError):package.validate(root)
            (root/'private.isp').unlink()
            (root/'references/bootstrap.md').write_text('[outside](../../private.isp)')
            with self.assertRaises(ValueError):package.validate(root)

    def test_operation_name_is_insufficient_for_version_module_and_argument_compatibility(self):
        manifest=json.loads((package.SKILL/'references/compatibility.json').read_text())
        name='dvp.il_to_mpu';recipe=manifest['recipes'][name]
        valid=package.check_recipe(manifest,name,'0.4.0',recipe['module_sha256'],recipe['tools'])
        self.assertEqual(valid['status'],'COMPATIBLE');self.assertFalse(valid['grants_authority'])
        for change in ('version','module','schema','missing'):
            tools=copy.deepcopy(recipe['tools']);hashes=dict(recipe['module_sha256']);version='0.4.0'
            if change=='version':version='future-unqualified'
            elif change=='module':hashes[next(iter(hashes))]='0'*64
            elif change=='schema':tools['adapter.invoke']['required_fields']['arguments']={'type':'string'}
            else:tools.pop('adapter.invoke')
            result=package.check_recipe(manifest,name,version,hashes,tools)
            self.assertEqual(result['status'],'INCOMPATIBLE',change)
            self.assertEqual(result['recipe'],name);self.assertFalse(result['grants_authority'])

    def test_synthetic_catalog_is_rebuilt_from_an_input_free_generator(self):
        spec=importlib.util.spec_from_file_location('digital_bench',ROOT/'tests/dvpserial/synthetic_bench.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        stored=json.loads((ROOT/'tests/dvpserial/fixtures/digital-bench.json').read_text())
        self.assertEqual(stored,m.catalog())
        self.assertEqual(m.LANES,8);self.assertEqual(m.BANKS,4)
        self.assertFalse((ROOT/'tests/dvpserial/fixtures/thermal-regression.json').exists())
        self.assertEqual(stored['commands']['bench.lane.start']['coils'][0],'M256')


class GoalGraphTests(unittest.TestCase):
    def test_goal_workflows_have_real_dependencies_and_s0_does_not_wait_for_native(self):
        _,items=goals.validate(ROOT)
        for key,item in items.items():
            if key.startswith('S0-'):self.assertTrue(all(d.startswith('S0-') for d in item['dependencies']))
        self.assertNotEqual(items['S2-G0']['status'],'ACCEPTED')

    def test_input_drift_invalidates_only_affected_goals_and_dependents(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();p=root/'input';p.write_text('changed')
            gs={'S0-G0':{'input_hashes':{'input':'0'*64},'dependencies':[]},
                'S0-G1':{'input_hashes':{},'dependencies':['S0-G0']},
                'S2-G0':{'input_hashes':{},'dependencies':[]}}
            self.assertEqual(goals.drifted(root,gs),{'S0-G0','S0-G1'})

    def test_real_goal_inputs_and_outputs_invalidate_their_dependency_closure(self):
        _,items=goals.validate(ROOT)
        self.assertEqual(goals.drifted(ROOT,items),set())
        original=Path.read_bytes
        cases={'skills/delta-plc-engineering/references/recovery.md':'S0-G4',
               'tests/skills/test_skill_stdio.py':'S0-G6',
               'src/universal_plc/service.py':'S0-G0'}
        for name,owner in cases.items():
            with self.subTest(name=name):
                target=(ROOT/name).resolve()
                def changed(p):
                    data=original(p)
                    return data+b'\nDRIFT' if p.resolve()==target else data
                with patch.object(Path,'read_bytes',changed):affected=goals.drifted(ROOT,items)
                self.assertIn(owner,affected)
                self.assertIn('S0-G8',affected)
                if owner!='S0-G0':self.assertNotIn('S0-G0',affected)
                if owner!='S0-G0':self.assertNotIn('S2-G0',affected)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();(root/'artifact').write_bytes(b'changed output')
            gs={'S0-G0':{'input_hashes':{},'output_hashes':{'artifact':'0'*64},'dependencies':[]}}
            self.assertEqual(goals.drifted(root,gs),{'S0-G0'})

    def test_cycle_and_unproved_acceptance_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();shutil.copytree(ROOT/'goals/plc-skill',root/'goals/plc-skill')
            shutil.copytree(ROOT/'workflows/plc-skill',root/'workflows/plc-skill')
            p=root/'goals/plc-skill/S0-G0.json';g=json.loads(p.read_text());g['dependencies']=['S0-G1'];p.write_text(json.dumps(g))
            with self.assertRaises(ValueError):goals.validate(root)
            g['dependencies']=[];g['status']='ACCEPTED';g['evidence']=[];p.write_text(json.dumps(g))
            with self.assertRaises(ValueError):goals.validate(root)


class SourcePackageTests(unittest.TestCase):
    def tree(self,root,extra=None):
        def git(*args):return subprocess.check_output(['git',*args],cwd=root)
        if not (root/'.git').exists():git('init','-q')
        files={'LICENSE':b'MIT test attribution\n','README.md':b'Neutral package test\n'}
        if extra:files.update(extra)
        for name,data in files.items():
            p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        (root/'PUBLIC_SOURCE_MANIFEST.json').write_bytes((json.dumps({'files':{n:package.sha(b) for n,b in files.items()}})+'\n').encode())
        git('add','--',*files,'PUBLIC_SOURCE_MANIFEST.json')
        return git('write-tree').decode().strip()

    def test_source_archive_uses_pinned_tree_and_never_copies_untracked_state_or_history(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();tree=self.tree(root)
            (root/'untracked-customer.isp').write_bytes(b'NOT_TRACKED_CANARY')
            result=package.build_source(root,root/'source.zip',tree)
            self.assertFalse(result['private_git_history'])
            with zipfile.ZipFile(root/'source.zip') as z:
                self.assertEqual(set(z.namelist()),{'delta-plc-agent/LICENSE','delta-plc-agent/README.md','delta-plc-agent/PUBLIC_SOURCE_MANIFEST.json'})

    def test_indexed_native_file_cannot_be_hidden_by_an_updated_manifest(self):
        for name in ('private.isp','nested.zip','opaque.bin'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as d:
                root=Path(d).resolve();tree=self.tree(root,{name:b'CANARY'})
                with self.assertRaises(ValueError):package.build_source(root,root/'source.zip',tree)
                self.assertFalse((root/'source.zip').exists())


if __name__=='__main__':unittest.main()
