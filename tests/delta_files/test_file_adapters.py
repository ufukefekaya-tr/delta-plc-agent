from __future__ import annotations

import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from universal_plc.delta_files import DeltaFileAdapter, FileTransformError
from universal_plc.delta_files import containers, packages
from universal_plc.delta_files._vendor import mpu_codec
from universal_plc.delta_files.limits import digest


def source_text(model='SE', newline='\r\n'):
    return '''<PROJECT>
VERSION_CONTROL=ISPSoft_Version_1
Active_CodePage=1254
Prj_DT=MODEL
Prj_C_SourceCodeLocked=FALSE
Prj_Company=Neutral qualification fixture
</PROJECT>
<TASK>
[TASK_S]
T_PName=Program0
[TASK_E]
</TASK>
<GLOBAL_VAR>
TableName=Global Variables
</GLOBAL_VAR>
<GroupPOUFolder>
<ProgramFolder>
</ProgramFolder>
</GroupPOUFolder>
<SYSTEM_VAR>
[BIT_RANGE]
<M>0-511
[/BIT_RANGE]
[WORD_RANGE]
<D>7000-7999
[/WORD_RANGE]
[TIMER_RANGE]
<T>0-127
[/TIMER_RANGE]
[COUNTER_RANGE]
<C>0-111
[/COUNTER_RANGE]
[STEP_RANGE]
<S>10-511
[/STEP_RANGE]
</SYSTEM_VAR>
<FBInst>
</FBInst>
<POU>
P_Name=Program0
P_type=0
P_Lang=2
<LOCAL_VAR>
(* Değişken açıklaması *)
</LOCAL_VAR>
<IL_ST_CODE>
(* Türkçe: ölçüm, ışık, sayaç *)
Value := Value + 1;
</IL_ST_CODE>
</POU>
'''.replace('Prj_DT=MODEL', 'Prj_DT='+model).replace('\n', newline)


def make_container(source=None, offset=0xaa):
    raw = (source or source_text()).encode('cp1254')
    compressor = zlib.compressobj(9, wbits=-15)
    compressed = compressor.compress(raw) + compressor.flush()
    header = bytearray(offset)
    struct.pack_into('<I', header, 0x0c, 1020)
    struct.pack_into('<I', header, 0x10, 19)
    struct.pack_into('<I', header, 0x7c, len(raw))
    struct.pack_into('<I', header, 0x94, len(compressed)+6)
    struct.pack_into('<I', header, 0x98, offset-6)
    struct.pack_into('<H', header, offset-6, 8)
    struct.pack_into('<I', header, offset-4, zlib.crc32(raw) & 0xffffffff)
    return bytes(header) + compressed


class FileAdapters(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = DeltaFileAdapter('ISPSoft')
        self.input = self.root/'neutral.isp'
        self.input.write_bytes(make_container())
        self.original = self.input.read_bytes()

    def file(self, name, value):
        path = self.root/name
        path.write_bytes(value.encode('utf-8') if isinstance(value, str) else value)
        return str(path)

    def invoke(self, operation, **kwargs):
        return self.adapter.invoke(operation, kwargs)

    def test_inspect_explicit_evidence(self):
        result = self.invoke('isp.inspect', input_path=str(self.input))
        self.assertEqual(result['evidence_kind'], 'FILE_TRANSFORM')
        self.assertFalse(result['vendor_compile'])
        self.assertFalse(result['live_execution'])
        self.assertEqual(result['project_model'], 'SE')
        self.assertEqual(result['encoding'], 'cp1254')

    def test_roundtrip_both_offsets_and_newlines(self):
        for index, (offset, newline) in enumerate([(0xaa, '\r\n'), (0xac, '\r\r\n'), (0xaa, '\n')]):
            source = self.root/f'input-{index}.isp'
            source.write_bytes(make_container(source_text(newline=newline), offset))
            result = self.invoke('isp.roundtrip', input_path=str(source), output_path=str(self.root/f'out-{index}.isp'))
            self.assertTrue(result['source_equal'])
            self.assertEqual(containers.read_container(source).source, containers.read_container(result['output_path']).source)

    def test_unpack_pack_preserves_original_bytes(self):
        directory = str(self.root/'export')
        self.invoke('isp.unpack', input_path=str(self.input), output_dir=directory)
        result = self.invoke('isp.pack', input_dir=directory, output_path=str(self.root/'again.isp'))
        self.assertTrue(result['source_equal'])
        self.assertEqual(self.input.read_bytes(), self.original)

    def test_pack_edited_utf8_bom(self):
        directory = self.root/'export'
        self.invoke('isp.unpack', input_path=str(self.input), output_dir=str(directory))
        file = directory/'project-source.txt'
        file.write_text(file.read_text(encoding='utf-8').replace('Value + 1', 'Value + 2'), encoding='utf-8-sig')
        result = self.invoke('isp.pack', input_dir=str(directory), output_path=str(self.root/'again.isp'))
        self.assertFalse(result['source_equal'])
        self.assertIn(b'Value + 2', containers.read_container(result['output_path']).source)

    def test_patch_st_turkish_bom_preserves_unrelated_source(self):
        code = self.file('code.st', '\ufeff(* ölçüm, ışık, sayaç *)\nValue := 42;\n')
        result = self.invoke('isp.patch_st', input_path=str(self.input), output_path=str(self.root/'patched.isp'), pou_name='Program0', code_path=code)
        text = containers.read_container(result['output_path']).source.decode('cp1254')
        self.assertIn('ölçüm, ışık, sayaç', text)
        self.assertIn('Değişken açıklaması', text)
        self.assertIn('Value := 42;', text)
        self.assertEqual(self.original, self.input.read_bytes())

    def test_patch_vars_only(self):
        variables = self.file('vars.txt', '(* Değişken *)\n')
        result = self.invoke('isp.patch_st', input_path=str(self.input), output_path=str(self.root/'vars.isp'), pou_name='Program0', variables_path=variables)
        self.assertIn(b'Value := Value + 1;', containers.read_container(result['output_path']).source)

    def test_missing_pou_and_bad_encoding_rejected(self):
        for name, code in [('Missing', 'Value := 2;'), ('Program0', '(* 😀 *)')]:
            with self.subTest(name=name), self.assertRaises(FileTransformError):
                self.invoke('isp.patch_st', input_path=str(self.input), output_path=str(self.root/'bad.isp'), pou_name=name, code_path=self.file('bad.st', code))
        self.assertFalse((self.root/'bad.isp').exists())

    def test_original_and_existing_output_never_overwritten(self):
        for target in (self.input, self.root/'existing.isp'):
            if target != self.input:
                target.write_bytes(b'sentinel')
            before = target.read_bytes()
            with self.assertRaises(FileTransformError):
                self.invoke('isp.roundtrip', input_path=str(self.input), output_path=str(target))
            self.assertEqual(target.read_bytes(), before)

    def test_container_corruption_and_truncation(self):
        for index, data in enumerate([self.original[:20], self.original[:-1], self.original+b'junk', self.original[:166]+b'\x00'*4+self.original[170:]]):
            path = self.file(f'corrupt-{index}.isp', data)
            with self.subTest(index=index), self.assertRaises(FileTransformError):
                self.invoke('isp.inspect', input_path=path)

    def test_decompression_limit(self):
        with patch('universal_plc.delta_files.limits.MAX_SOURCE_BYTES', 30), self.assertRaises(FileTransformError):
            containers.decode(self.original)

    def test_protected_project_rejected(self):
        data = make_container(source_text().replace('Prj_C_SourceCodeLocked=FALSE', 'Prj_C_SourceCodeLocked=TRUE'))
        with self.assertRaises(FileTransformError):
            containers.decode(data)

    def test_modified_original_rejected(self):
        directory = self.root/'export'
        self.invoke('isp.unpack', input_path=str(self.input), output_dir=str(directory))
        (directory/'original.isp').write_bytes(make_container(source_text().replace('Value + 1', 'Value + 4')))
        with self.assertRaises(FileTransformError):
            self.invoke('isp.pack', input_dir=str(directory), output_path=str(self.root/'bad.isp'))

    def test_il_mpu_roundtrip(self):
        il = '\ufeff; Türkçe açıklama\nLD,M0\nANI,M1\nOUT,M2\nLD=,D0,K4\nMOV,K5,D1\nEND\n'
        result = self.invoke('dvp.il_to_mpu', input_path=self.file('sample.il', il), output_path=str(self.root/'sample.mpu'))
        back = self.invoke('dvp.mpu_to_il', input_path=result['output_path'], output_path=str(self.root/'back.il'))
        self.assertEqual(Path(back['output_path']).read_text(), 'LD,M0\nANI,M1\nOUT,M2\nLD=,D0,K4\nMOV,K5,D1\n')
        self.assertEqual(back['network_count'], 2)

    def test_il_all_supported_application_instructions(self):
        code = 'LD,M0\nMOV,K1,D0\nDADD,D0,K1,D2\nDSUB,D2,K1,D4\nDMUL,D4,K2,D6\nINC,D8\nZRST,D10,D12\nTMR,T0,K10\nDPLSY,D20,K0,Y0\nSTL,S0\nRET\n'
        result = self.invoke('dvp.il_to_mpu', input_path=self.file('ops.il', code), output_path=str(self.root/'ops.mpu'))
        self.invoke('dvp.mpu_to_il', input_path=result['output_path'], output_path=str(self.root/'ops-back.il'))
        self.assertEqual((self.root/'ops-back.il').read_text(), code)

    def test_il_unsupported_opcode_arity_structure(self):
        for index, code in enumerate(['LD,M0\nDDIV,D0,D1,D2', 'LD\nOUT,M0', 'LD,M0\nMOV,K1', 'LD,M0\nOUT,M1\nEND\nINC,D0', 'LD,M0\nOR,M1\nAND,M2\nOUT,M3', 'LD,M0']):
            with self.subTest(index=index), self.assertRaises(FileTransformError):
                self.invoke('dvp.il_to_mpu', input_path=self.file(f'bad-{index}.il', code), output_path=str(self.root/f'bad-{index}.mpu'))

    def test_unknown_mpu_node_cannot_be_silently_discarded(self):
        data, _ = __import__('universal_plc.delta_files.dvp', fromlist=['il_to_mpu']).il_to_mpu('LD,M0\nOUT,M1', 'Program0', 'cp1254')
        prefix, members = packages.unpack(data)
        members['Unzipped.src'] = members['Unzipped.src'].replace(b'TYPE=13', b'TYPE=88')
        path = self.file('unknown.mpu', packages.pack(prefix, members))
        with self.assertRaises(FileTransformError):
            self.invoke('dvp.mpu_to_il', input_path=path, output_path=str(self.root/'bad.il'))

    def test_library_multiple_members_rebuild(self):
        raw = packages.pack(mpu_codec.prefill_prefix(0), {'Unzipped.src': b'neutral', 'nested/catalog.txt': 'ölçüm'.encode('cp1254')})
        directory = str(self.root/'library')
        self.invoke('isp.package_unpack', input_path=self.file('library.fbu', raw), output_dir=directory)
        result = self.invoke('isp.package_pack', input_dir=directory, output_path=str(self.root/'rebuilt.fbu'))
        self.assertEqual(packages.unpack(raw)[1], packages.unpack(Path(result['output_path']).read_bytes())[1])

    def test_zip_traversal_and_duplicate_names(self):
        for names in [['../escape'], ['C:/escape'], ['x', 'X'], ['CON']]:
            with self.subTest(names=names):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as archive:
                    for name in names:
                        archive.writestr(name, b'no')
                archive_bytes = stream.getvalue()
                data = mpu_codec.prefill_prefix(len(archive_bytes)) + archive_bytes
                with self.assertRaises(FileTransformError):
                    packages.unpack(data)

    def test_manifest_traversal_rejected(self):
        data = packages.pack(mpu_codec.prefill_prefix(0), {'Unzipped.src': b'neutral'})
        directory = self.root/'library'
        self.invoke('isp.package_unpack', input_path=self.file('p.mpu', data), output_dir=str(directory))
        manifest_path = directory/'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['members'] = ['../../external.txt']
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(FileTransformError):
            self.invoke('isp.package_pack', input_dir=str(directory), output_path=str(self.root/'bad.mpu'))

    def test_generator_neutral_program(self):
        spec = dict(plc=dict(family='DVP', model='DVP-SE'), variables=[], programs=[dict(name='Neutral', networks=[dict(kind='coil', logic=dict(type='contact', name='M0'), coil=dict(name='M1'), comment='Nötr yazılım testi')])])
        result = self.invoke('dvp.generate_isp', template_path=str(self.input), spec_path=self.file('spec.json', json.dumps(spec)), output_path=str(self.root/'generated.isp'))
        text = containers.read_container(result['output_path']).source.decode('cp1254')
        self.assertIn('P_Name=Neutral', text)
        self.assertIn('T_PName=Neutral', text)
        self.assertIn('Prj_Company=Neutral qualification fixture', text)
        self.assertIn('Nötr yazılım testi', text)
        self.assertEqual(self.original, self.input.read_bytes())

    def test_generator_rejects_model_mismatch(self):
        spec = dict(plc=dict(family='AS', model='AS228'), programs=[])
        with self.assertRaises(FileTransformError):
            self.invoke('dvp.generate_isp', template_path=str(self.input), spec_path=self.file('bad-spec.json', json.dumps(spec)), output_path=str(self.root/'bad.isp'))

    def test_generator_function_block_and_global_instance(self):
        spec = dict(variables=[dict(name='Instance0', fb_type='NeutralFB')],
                    function_blocks=[dict(name='NeutralFB', variables=[
                        dict(name='Request', data_type='BOOL', scope='VAR_INPUT'),
                        dict(name='Response', data_type='BOOL', scope='VAR_OUTPUT')],
                        networks=[dict(kind='coil', logic=dict(type='contact', name='Request'), coil=dict(name='Response'))])],
                    programs=[dict(name='NeutralMain', networks=[dict(kind='fb_call', fb='NeutralFB', instance='Instance0', inputs=['M100'], outputs=['M101'])])])
        result = self.invoke('dvp.generate_isp', template_path=str(self.input), spec_path=self.file('fb.json', json.dumps(spec)), output_path=str(self.root/'fb.isp'))
        text = containers.read_container(result['output_path']).source.decode('cp1254')
        self.assertIn('P_Name=NeutralFB', text)
        self.assertIn('DEV_NAME=Instance0', text)
        self.assertIn('Global->Instance0->Request=', text)
        self.assertIn('Global->Instance0->Response=', text)

    def test_generator_rejects_structural_injection(self):
        spec = dict(programs=[dict(name='Neutral\n</POU>')])
        with self.assertRaises(FileTransformError):
            self.invoke('dvp.generate_isp', template_path=str(self.input), spec_path=self.file('injected.json', json.dumps(spec)), output_path=str(self.root/'bad.isp'))

    def test_inactive_mpu_network_is_not_silently_enabled(self):
        from universal_plc.delta_files.dvp import il_to_mpu
        data, _ = il_to_mpu('LD,M0\nOUT,M1', 'Program0', 'cp1254')
        prefix, members = packages.unpack(data)
        members['Unzipped.src'] = members['Unzipped.src'].replace(b'NET_ACTIVE=TRUE', b'NET_ACTIVE=FALSE')
        path = self.file('inactive.mpu', packages.pack(prefix, members))
        with self.assertRaises(FileTransformError):
            self.invoke('dvp.mpu_to_il', input_path=path, output_path=str(self.root/'bad.il'))

    def test_atomic_publish_collision_preserves_existing_file(self):
        target = self.root/'raced.isp'
        def race(source, destination):
            Path(destination).write_bytes(b'external-writer')
            raise FileExistsError('Concurrent output creation')
        with patch('universal_plc.delta_files.limits.os.link', side_effect=race), self.assertRaises(FileTransformError):
            self.invoke('isp.roundtrip', input_path=str(self.input), output_path=str(target))
        self.assertEqual(target.read_bytes(), b'external-writer')
        self.assertFalse(list(self.root.glob('.plc-*')))

    def test_capabilities_do_not_claim_native(self):
        for toolchain in ['ispsoft', 'wplsoft', 'diadesigner']:
            adapter = DeltaFileAdapter(toolchain)
            for cap in adapter.capabilities():
                if cap['operation'] in ['project.build', 'deployment.execute', 'target.run'] or toolchain != 'ispsoft':
                    self.assertEqual(cap['implementation'], 'not_implemented')
            with self.assertRaises(FileTransformError):
                adapter.invoke('project.build', {})

    def test_universal_as_target_cannot_use_dvp_operations(self):
        target = dict(vendor='delta', family='AS', model='AS228T-A', toolchain='ispsoft')
        for operation in ('dvp.generate_isp', 'dvp.il_to_mpu', 'dvp.mpu_to_il'):
            with self.subTest(operation=operation), self.assertRaisesRegex(FileTransformError, 'DVP-only'):
                self.invoke(operation, universal_target=target)

    def test_generator_requires_supported_exact_universal_model(self):
        spec = dict(programs=[dict(name='Neutral', networks=[dict(kind='coil', logic=dict(type='contact', name='M0'), coil=dict(name='M1'))])])
        spec_path = self.file('target-spec.json', json.dumps(spec))
        for model in ('DVP12SE11R', 'DVP12SE11T'):
            with self.subTest(model=model):
                result = self.invoke('dvp.generate_isp', template_path=str(self.input), spec_path=spec_path,
                                     output_path=str(self.root/(model+'.isp')),
                                     universal_target=dict(vendor='delta', family='DVP', model=model, toolchain='ispsoft'))
                self.assertEqual(result['target_match']['status'], 'unknown')
        for model in ('DVP-SE', 'DVP14SS211R', 'DVP12SE211R', 'DVP12SE99X'):
            with self.subTest(model=model), self.assertRaisesRegex(FileTransformError, 'exact supported model'):
                self.invoke('dvp.generate_isp', universal_target=dict(vendor='delta', family='DVP', model=model))

    def test_isp_operations_reject_conflicting_universal_metadata(self):
        target = dict(vendor='delta', family='AS', model='AS228T-A', toolchain='ispsoft')
        for operation in ('isp.inspect', 'isp.unpack', 'isp.patch_st', 'isp.roundtrip'):
            with self.subTest(operation=operation), self.assertRaisesRegex(FileTransformError, 'conflicts'):
                self.invoke(operation, input_path=str(self.input), universal_target=target)
        self.invoke('isp.unpack', input_path=str(self.input), output_dir=str(self.root/'export'))
        with self.assertRaisesRegex(FileTransformError, 'conflicts'):
            self.invoke('isp.pack', input_dir=str(self.root/'export'), output_path=str(self.root/'wrong.isp'), universal_target=target)
        self.assertFalse((self.root/'wrong.isp').exists())

    def test_isp_pack_checks_edited_target_before_publishing(self):
        target = dict(vendor='delta', family='DVP', model='DVP12SE11R', toolchain='ispsoft')
        directory = self.root/'export'
        self.invoke('isp.unpack', input_path=str(self.input), output_dir=str(directory), universal_target=target)
        source = directory/'project-source.txt'
        source.write_text(source.read_text(encoding='utf-8').replace('Prj_DT=SE', 'Prj_DT=AS228T-A'), encoding='utf-8')
        with self.assertRaisesRegex(FileTransformError, 'conflicts'):
            self.invoke('isp.pack', input_dir=str(directory), output_path=str(self.root/'wrong.isp'), universal_target=target)
        self.assertFalse((self.root/'wrong.isp').exists())

    def test_family_hints_are_not_exact_target_proof(self):
        cases = [('SE', 'DVP', 'DVP12SE11R'), ('DVP', 'DVP', 'DVP12SE11R'),
                 ('AS', 'AS', 'AS228T-A'), ('AS228', 'AS', 'AS228T-A'), ('UNKNOWN', 'AS', 'AS228T-A')]
        for index, (metadata, family, model) in enumerate(cases):
            with self.subTest(metadata=metadata):
                path = self.file(f'hint-{index}.isp', make_container(source_text(model=metadata)))
                result = self.invoke('isp.inspect', input_path=path,
                                     universal_target=dict(vendor='delta', family=family, model=model))
                self.assertEqual(result['target_match']['status'], 'unknown')

    def test_exact_metadata_match_and_conflict(self):
        path = self.file('exact.isp', make_container(source_text(model='AS228T-A')))
        target = dict(vendor='delta', family='AS', model='AS228T-A')
        result = self.invoke('isp.inspect', input_path=path, universal_target=target)
        self.assertEqual(result['target_match']['status'], 'metadata_match')
        self.assertFalse(result['vendor_compile'])
        with self.assertRaisesRegex(FileTransformError, 'conflicts'):
            self.invoke('isp.inspect', input_path=path, universal_target={**target, 'model': 'AS228R-A'})

    def test_other_dvp_series_conflicts_with_se_file(self):
        with self.assertRaisesRegex(FileTransformError, 'conflicts'):
            self.invoke('isp.inspect', input_path=str(self.input),
                        universal_target=dict(vendor='delta', family='DVP', model='DVP14SS211R'))

    def test_invalid_or_wrong_adapter_universal_target_rejected(self):
        for target in (None, {}, {'vendor': 'delta', 'family': 'AS'},
                       dict(vendor='another', family='AS', model='AS228T-A'),
                       dict(vendor='delta', family='DVP', model='AS228T-A'),
                       dict(vendor='delta', family='AS', model='AS228T-A', toolchain='wplsoft')):
            with self.subTest(target=target), self.assertRaises(FileTransformError):
                self.invoke('isp.inspect', input_path=str(self.input), universal_target=target)

    def test_canonical_native_operation_names_remain_not_implemented(self):
        capabilities = {c['operation']: c for c in self.adapter.capabilities()}
        for operation in ('project.create', 'target.observe.start', 'target.observe.read', 'target.observe.stop', 'job.status', 'job.cancel'):
            self.assertEqual(capabilities[operation]['implementation'], 'not_implemented')

    def test_pinned_source_hashes(self):
        root = Path(__file__).resolve().parents[2]
        manifest = json.loads((root/'backends/delta_sources/manifest.json').read_text())
        for record in manifest['files']:
            with self.subTest(path=record['local_path']):
                self.assertEqual(digest((root/record['local_path']).read_bytes()), record['local_sha256'])


if __name__ == '__main__':
    unittest.main()
