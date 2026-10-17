"""Offline adapter facade. No IDE launch, vendor compilation or PLC connection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import threading

from . import containers, dvp, packages
from ._vendor import ispsoft_generator as generator
from .limits import (FileTransformError, MAX_FILE_BYTES, MAX_MEMBERS, MAX_SOURCE_BYTES, atomic_write,
                     contained, digest, output_path, read_bytes, read_text, safe_member)


OPERATIONS = {
    'isp.inspect': 'Inspect supported unprotected ISP containers; model metadata is a hint, not target enrollment.',
    'isp.unpack': 'Export exact original and editable UTF-8 source into a new directory.',
    'isp.pack': 'Rebuild source from an exported directory with CRC and payload verification.',
    'isp.patch_st': 'Replace existing IL_ST_CODE and/or LOCAL_VAR sections in one named POU.',
    'isp.roundtrip': 'Repack a supported container and verify unchanged source bytes.',
    'isp.package_unpack': 'Extract bounded FBU/MPU/FCU/CLU package members into a new directory.',
    'isp.package_pack': 'Rebuild and verify ZipCrypto library packages from an exported directory.',
    'dvp.il_to_mpu': 'Generate a DVP POU package from the pinned, limited IL instruction set.',
    'dvp.mpu_to_il': 'Export supported Ladder networks to IL; reject unsupported/lossy node layouts.',
    'dvp.generate_isp': 'Generate DVP-SE Ladder/Program/FB/global/task source using an explicit compatible template and JSON.',
}
NATIVE_OPERATIONS = ('project.create', 'project.build', 'project.simulate', 'project.upload',
                     'deployment.execute', 'target.run', 'target.stop', 'target.observe.start',
                     'target.observe.read', 'target.observe.stop', 'job.status', 'job.cancel', 'wpl.write_dvp')
_GENERATOR_LOCK = threading.Lock()
_GENERATOR_MODELS = frozenset({'DVP12SE11R', 'DVP12SE11T'})
_DVP_SERIES = frozenset({'SE', 'SE2', 'SS2', 'SA2', 'SX2', 'SV2', 'ES2', 'EX2',
                         'EH3', 'EC3', 'ES3', 'EX3', 'SV3', 'SX3'})


def _model_key(value: str) -> str:
    return value.strip().upper().replace('-', '').replace(' ', '')


def _target_for(operation: str, args: dict) -> dict | None:
    # Direct library callers keep file-only compatibility. The universal service
    # supplies this immutable workspace target; callers cannot override it there.
    if 'universal_target' not in args:
        return None
    target = args['universal_target']
    if not isinstance(target, dict) or any(not isinstance(target.get(k), str) or not target[k].strip()
                                           for k in ('vendor', 'family', 'model')):
        raise FileTransformError('universal_target requires vendor, family and exact model')
    target = dict(target)
    target.update(vendor=target['vendor'].strip().lower(), family=target['family'].strip().upper(),
                  model=_model_key(target['model']))
    if target['vendor'] != 'delta' or target['family'] not in ('AS', 'DVP'):
        raise FileTransformError('Universal target does not belong to this Delta ISPSoft adapter')
    if target.get('toolchain') and str(target['toolchain']).strip().lower() != 'ispsoft':
        raise FileTransformError('Universal target toolchain conflicts with ISPSoft operation')
    if not target['model'].startswith(target['family']):
        raise FileTransformError('Universal target family and model disagree')
    if operation.startswith('dvp.') and target['family'] != 'DVP':
        raise FileTransformError('DVP-only transformation cannot be used for an AS target')
    if operation == 'dvp.generate_isp' and target['model'] not in _GENERATOR_MODELS:
        raise FileTransformError('DVP generator requires exact supported model DVP12SE11R or DVP12SE11T')
    return target


def _target_match(container: containers.Container | None, target: dict | None) -> dict:
    if target is None:
        return dict(status='unknown', reason='No universal target supplied; file-only operation')
    metadata = containers.inspect(container)['project_model'] if container is not None else None
    if not metadata:
        return dict(status='unknown', reason='No PLC model metadata in this file representation')
    hint = _model_key(metadata)
    family, model = target['family'], target['model']
    if hint in ('DVP', 'AS', 'AX'):
        if family != hint:
            raise FileTransformError(f'Project family {metadata!r} conflicts with universal target {family}')
    elif hint in _DVP_SERIES:
        if family != 'DVP' or not re.fullmatch(r'DVP\d+' + re.escape(hint) + r'(?:\d[A-Z0-9]*)?', model):
            raise FileTransformError(f'Project series {metadata!r} conflicts with universal target {model}')
    elif re.fullmatch(r'AS\d{3}', hint):
        if family != 'AS' or not model.startswith(hint):
            raise FileTransformError(f'Project series {metadata!r} conflicts with universal target {model}')
    elif hint.startswith(('DVP', 'AS', 'AX')):
        if hint != model:
            raise FileTransformError(f'Project model {metadata!r} conflicts with universal target {model}')
        return dict(status='metadata_match', project_model=metadata,
                    reason='Exact textual model matches; native/hardware compatibility remains unverified')
    # Family/series labels and uninterpreted identifiers never prove an exact PLC.
    return dict(status='unknown', project_model=metadata,
                reason='Only family/series or unqualified model metadata; exact target compatibility unverified')


def _json(path: Path) -> dict:
    value = json.loads(read_text(path))
    if not isinstance(value, dict):
        raise FileTransformError('Expected a JSON object')
    return value


def _publish_directory(target: Path, files: dict[str, bytes]) -> None:
    target = output_path(target)
    if len(files) > MAX_MEMBERS or sum(map(len, files.values())) > 3 * MAX_SOURCE_BYTES:
        raise FileTransformError('Export exceeds size limits')
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.plc-export-') as temp:
        staging = Path(temp) / 'payload'
        staging.mkdir()
        for name, data in files.items():
            path = contained(staging, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        os.rename(staging, target)


def _json_bytes(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def _read_export(directory: Path, name: str) -> bytes:
    return read_bytes(contained(directory, name), MAX_FILE_BYTES if name == 'original.isp' else MAX_SOURCE_BYTES)


def _validate_spec_size(value: object, depth: int = 0) -> int:
    if depth > 48:
        raise FileTransformError('JSON specification nesting exceeds limit')
    if isinstance(value, dict):
        count = 1 + sum(_validate_spec_size(v, depth+1) for v in value.values())
    elif isinstance(value, list):
        count = 1 + sum(_validate_spec_size(v, depth+1) for v in value)
    else:
        if isinstance(value, str) and (any(c in value for c in ('\x00', '\r', '\n')) or
                                      re.search(r'</?[A-Za-z_][^>]*>|\[/?[A-Z_]+\]|\*\)', value)):
            raise FileTransformError('Specification contains structural delimiters')
        count = 1
    if count > 20_000:
        raise FileTransformError('JSON specification exceeds structural size limit')
    return count


class DeltaFileAdapter:
    """Caller constrains explicit filesystem arguments to the task workspace.

    Embedded paths from manifests and JSON never gain ambient read authority.
    Only explicit *_path arguments may select API/knowledge library files.
    """

    def __init__(self, toolchain: str):
        self.toolchain = toolchain.lower().replace('-', '').replace('_', '')
        if self.toolchain not in ('ispsoft', 'wplsoft', 'diadesigner'):
            raise FileTransformError(f'Unknown Delta file toolchain: {toolchain}')

    def capabilities(self) -> list[dict]:
        available = self.toolchain == 'ispsoft'
        result = [dict(operation=op, implementation='available' if available else 'not_implemented',
                       qualification='software' if available else 'unverified', description=desc)
                  for op, desc in OPERATIONS.items()]
        result += [dict(operation=op, implementation='not_implemented', qualification='unverified',
                        description='Requires a separately implemented and qualified native IDE/target adapter.')
                   for op in NATIVE_OPERATIONS]
        return result

    def invoke(self, operation: str, arguments: dict) -> dict:
        if self.toolchain != 'ispsoft' or operation not in OPERATIONS:
            raise FileTransformError(f'{operation} is not implemented by {self.toolchain} file adapter')
        if not isinstance(arguments, dict):
            raise FileTransformError('Arguments must be an object')
        try:
            target = _target_for(operation, arguments)
            value = self._invoke(operation, arguments, target)
        except (OSError, UnicodeError, LookupError, KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
            if isinstance(exc, FileTransformError):
                raise
            raise FileTransformError(str(exc)) from exc
        return dict(ok=True, operation=operation, toolchain='ispsoft', evidence_kind='FILE_TRANSFORM',
                    vendor_compile=False, live_execution=False, **value)

    def _invoke(self, operation: str, args: dict, target: dict | None) -> dict:
        if operation == 'isp.pack':
            return self._pack_source(Path(args['input_dir']), Path(args['output_path']), target)
        if operation == 'isp.package_pack':
            return dict(self._pack_library(Path(args['input_dir']), Path(args['output_path'])),
                        target_match=_target_match(None, target))
        if operation == 'dvp.generate_isp':
            return self._generate(args, target)
        input_file = Path(args['input_path'])
        original = read_bytes(input_file)
        result = dict(input_sha256=digest(original), target_match=_target_match(None, target))
        if operation == 'isp.package_unpack':
            prefix, members = packages.unpack(original)
            files = {'prefix.bin': prefix}
            files.update({'contents/'+name: raw for name, raw in members.items()})
            manifest = dict(format='universal-plc-library-v1', source_sha256=digest(original),
                            prefix_sha256=digest(prefix), members=list(members))
            files['manifest.json'] = _json_bytes(manifest)
            target = Path(args['output_dir'])
            _publish_directory(target, files)
            return dict(**result, output_dir=str(target.resolve()), members=list(members))
        if operation == 'dvp.il_to_mpu':
            output, info = dvp.il_to_mpu(original.decode('utf-8-sig'), args.get('pou_name', 'Program0'), args.get('encoding', 'cp1254'))
            result.update(info)
        elif operation == 'dvp.mpu_to_il':
            text, info = dvp.mpu_to_il(original, args.get('encoding', 'cp1254'))
            output = text.encode('utf-8')
            result.update(info)
        else:
            container = containers.decode(original)
            result['target_match'] = _target_match(container, target)
            result.update(containers.inspect(container))
            result['source_sha256'] = digest(container.source)
            if operation == 'isp.inspect':
                return result
            if operation == 'isp.unpack':
                editable = containers.normalized(container.source.decode(container.encoding)).encode('utf-8')
                manifest = dict(format='universal-plc-isp-v1', original_sha256=digest(original),
                                editable_sha256=digest(editable), encoding=container.encoding,
                                newline=container.newline, stream_offset=container.offset)
                files = {'original.isp': original, 'project-source.txt': editable, 'manifest.json': _json_bytes(manifest)}
                target = Path(args['output_dir'])
                _publish_directory(target, files)
                return dict(**result, output_dir=str(target.resolve()))
            if operation == 'isp.roundtrip':
                output = container.rebuild(container.source)
                result['source_equal'] = True
            else:
                output = containers.patch_st(container, args['pou_name'],
                    read_text(args['code_path']) if args.get('code_path') else None,
                    read_text(args['variables_path']) if args.get('variables_path') else None)
                result['output_source_sha256'] = digest(containers.decode(output).source)
        target = atomic_write(args['output_path'], output, (input_file,))
        return dict(**result, output_path=str(target.resolve()), output_sha256=digest(output))

    def _pack_source(self, directory: Path, target: Path, universal_target: dict | None) -> dict:
        manifest = _json(contained(directory, 'manifest.json'))
        if manifest.get('format') != 'universal-plc-isp-v1':
            raise FileTransformError('Unsupported export manifest')
        original = _read_export(directory, 'original.isp')
        if digest(original) != manifest['original_sha256']:
            raise FileTransformError('Protected original container was modified')
        container = containers.decode(original)
        _target_match(container, universal_target)
        expected_editable = containers.normalized(container.source.decode(container.encoding)).encode('utf-8')
        if manifest['editable_sha256'] != digest(expected_editable):
            raise FileTransformError('Export manifest and original source disagree')
        editable = _read_export(directory, 'project-source.txt')
        if digest(editable) == manifest['editable_sha256']:
            source = container.source
        else:
            source = containers.normalized(editable.decode('utf-8-sig')).replace('\n', container.newline).encode(container.encoding)
        output = container.rebuild(source)
        match = _target_match(containers.decode(output), universal_target)
        atomic_write(target, output, (contained(directory, 'original.isp'),))
        return dict(input_sha256=digest(original), source_sha256=digest(source), source_equal=source == container.source,
                    output_path=str(target.resolve()), output_sha256=digest(output), target_match=match)

    def _pack_library(self, directory: Path, target: Path) -> dict:
        manifest = _json(contained(directory, 'manifest.json'))
        if manifest.get('format') != 'universal-plc-library-v1':
            raise FileTransformError('Unsupported package manifest')
        names = manifest['members']
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_MEMBERS:
            raise FileTransformError('Invalid package member list')
        prefix = _read_export(directory, 'prefix.bin')
        if digest(prefix) != manifest['prefix_sha256']:
            raise FileTransformError('Protected package prefix was modified')
        members, total = {}, 0
        for name in names:
            name = safe_member(name)
            raw = _read_export(directory, 'contents/'+name)
            total += len(raw)
            if total > MAX_SOURCE_BYTES or name in members:
                raise FileTransformError('Invalid package size or duplicate member')
            members[name] = raw
        contents = contained(directory, 'contents')
        actual = {p.relative_to(contents).as_posix() for p in contents.rglob('*') if p.is_file()}
        if actual != set(members):
            raise FileTransformError('Package member set changed')
        output = packages.pack(prefix, members)
        atomic_write(target, output)
        return dict(input_sha256=manifest['source_sha256'], output_sha256=digest(output),
                    output_path=str(target.resolve()), member_count=len(members))

    def _generate(self, args: dict, universal_target: dict | None) -> dict:
        template_path = Path(args['template_path'])
        original = read_bytes(template_path)
        container = containers.decode(original)
        _target_match(container, universal_target)
        if containers.inspect(container)['project_model'] != 'SE':
            raise FileTransformError('This generator profile requires an explicitly selected DVP-SE template')
        spec_path = Path(args['spec_path'])
        spec_bytes = read_bytes(spec_path, MAX_SOURCE_BYTES)
        spec = json.loads(spec_bytes.decode('utf-8-sig'))
        if not isinstance(spec, dict):
            raise FileTransformError('Specification must be a JSON object')
        _validate_spec_size(spec)
        plc = spec.get('plc', {})
        if plc.get('family', 'DVP').upper() != 'DVP' or plc.get('model', 'DVP-SE').upper() not in ('DVP-SE', 'SE'):
            raise FileTransformError('Specification and DVP-SE template model disagree')
        if spec.get('encoding', container.encoding).lower() != container.encoding.lower():
            raise FileTransformError('Specification encoding and template code page disagree')
        spec['encoding'] = container.encoding
        target = output_path(args['output_path'], (template_path, spec_path))
        with tempfile.TemporaryDirectory(prefix='plc-generator-') as temp:
            temp = Path(temp)
            (temp/'template.isp').write_bytes(original)
            libraries = {}
            library_hashes = {}
            for argument, keyword, kind in [('api_library_path', 'api_library', generator.APILibrary),
                                            ('special_devices_path', 'special_library', generator.SpecialDeviceLibrary),
                                            ('knowledge_path', 'knowledge', generator.KnowledgeBase)]:
                if args.get(argument):
                    path = temp/argument
                    content = read_bytes(args[argument], MAX_SOURCE_BYTES)
                    path.write_bytes(content)
                    library_hashes[argument] = digest(content)
                    libraries[keyword] = kind(path)
            # Upstream publishes warnings on a function attribute. Serialize that
            # tiny shared boundary so concurrent jobs cannot mix their evidence.
            with _GENERATOR_LOCK:
                generator.build_project(temp/'template.isp', spec, temp/'output.isp', **libraries)
                warnings = list(generator.build_project.last_warnings)
            output = read_bytes(temp/'output.isp')
            check = containers.decode(output)
            match = _target_match(check, universal_target)
        atomic_write(target, output, (template_path, spec_path))
        return dict(input_sha256=digest(original), spec_sha256=digest(spec_bytes), library_sha256=library_hashes,
                    output_path=str(target.resolve()), output_sha256=digest(output),
                    source_sha256=digest(check.source), warnings=warnings + ['Template hardware configuration is preserved; native IDE compile/readback remains required.'],
                    target_match=match, **containers.inspect(check))
