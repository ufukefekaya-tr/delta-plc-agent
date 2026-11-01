"""Validate and reproducibly package the optional skill; never operate a PLC."""
from __future__ import annotations

import argparse
import hashlib
import io
import importlib.metadata
import importlib.resources
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills' / 'delta-plc-engineering'
MEMBERS = frozenset({'SKILL.md', 'LICENSE', 'references/bootstrap.md', 'references/ax.md',
    'references/ispsoft-diadesigner.md', 'references/dvp-wplsoft.md',
    'references/recovery.md', 'references/compatibility.json'})


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(root: Path) -> dict:
    root = root.absolute()
    if root.is_symlink() or root.resolve() != root:
        raise ValueError('Skill root must not be redirected')
    actual = set()
    total = 0
    for p in root.rglob('*'):
        if p.is_symlink() or p.resolve() != p.absolute():
            raise ValueError('Redirected skill member')
        if p.is_file():
            name = p.relative_to(root).as_posix()
            actual.add(name); total += p.stat().st_size
    if actual not in (MEMBERS, MEMBERS | {'PACKAGE_MANIFEST.json'}) or total > 256 * 1024:
        raise ValueError('Unexpected, missing or oversized skill member')
    for name in sorted(actual):
        data = (root/name).read_bytes()
        text = data.decode('utf-8')
        for ref in re.findall(r'\]\(([^)]+)\)', text):
            if '://' in ref:
                raise ValueError('Standalone guidance must bundle its references')
            target = (root/PurePosixPath(name).parent/ref.split('#')[0]).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError('Missing or escaping skill reference')
    entry = (root/'SKILL.md').read_text('utf-8')
    if not entry.startswith('---\n') or '\nname: delta-plc-engineering\n' not in entry:
        raise ValueError('Skill frontmatter/name mismatch')
    manifest = json.loads((root/'references/compatibility.json').read_text('utf-8'))
    if manifest['skill_version'] != '1.0.0' or manifest['license'] != 'MIT':
        raise ValueError('Unexpected skill identity')
    files={name:sha((root/name).read_bytes()) for name in sorted(MEMBERS)}
    if 'PACKAGE_MANIFEST.json' in actual:
        provenance=json.loads((root/'PACKAGE_MANIFEST.json').read_text('utf-8'))
        if provenance.get('files') != files or not re.fullmatch('[a-f0-9]{40}',provenance.get('source_tree','')):
            raise ValueError('Detached package provenance mismatch')
    return {'status':'PASS','members':len(actual),'bytes':total,'files':files}


def schema_contract(schema: dict) -> dict:
    """Select required argument structure, not display titles or descriptions."""
    def shape(value):
        keys=('type','anyOf','items','additionalProperties','enum')
        return {k:value[k] for k in keys if k in value}
    return {'required_fields':{name:shape(schema.get('properties',{}).get(name,{}))
                              for name in schema.get('required',[])}}


def check_recipe(manifest: dict, recipe: str, core_version: str,
                 module_hashes: dict[str,str], tools: dict[str,dict]) -> dict:
    """Check a *supplied* runtime inventory; this is not an authority gate.

    The actual connected tool schemas and installed module/artifact identity are
    required. A matching operation name alone cannot validate generic arguments.
    """
    spec = manifest['recipes'][recipe]
    problems = []
    if core_version not in spec['core_versions']:
        problems.append('core_version')
    for name, wanted in spec['module_sha256'].items():
        if module_hashes.get(name) != wanted:
            problems.append('module_identity:'+name)
    for name, contract in spec['tools'].items():
        schema = tools.get(name)
        if not isinstance(schema,dict):
            problems.append('missing_tool:'+name);continue
        if schema != contract:
            problems.append('tool_schema:'+name)
    return dict(status='COMPATIBLE' if not problems else 'INCOMPATIBLE',recipe=recipe,
                problems=problems,grants_authority=False,
                effect='Only this recipe is affected; use independently known read-only diagnostics')


def installed_inventory(manifest: dict) -> dict:
    """Read installed identity without hiding editable/source version skew."""
    import universal_plc
    version=importlib.metadata.version('universal-plc-agents')
    if version != universal_plc.__version__:
        raise ValueError('Installed distribution and loaded source versions differ')
    names=set().union(*(set(r['module_sha256']) for r in manifest['recipes'].values()))
    hashes={};root=importlib.resources.files('universal_plc')
    for name in sorted(names):
        relative=PurePosixPath(name)
        if '..' in relative.parts or relative.parts[:2] != ('src','universal_plc'):
            raise ValueError('Unknown installed source identity path')
        hashes[name]=sha(root.joinpath(*relative.parts[2:]).read_bytes())
    return dict(core_version=version,module_sha256=hashes,grants_authority=False)


def pinned_skill(root: Path, source_tree: str, repository: Path) -> dict[str,bytes]:
    """Select immutable Git blobs, independent of working-directory newlines."""
    if not re.fullmatch('[a-f0-9]{40}',source_tree):raise ValueError('Exact Git tree identity required')
    repository=repository.resolve()
    try:prefix=root.resolve().relative_to(repository).as_posix()
    except ValueError:raise ValueError('Skill build root must belong to the source repository')
    def git(*args):
        try:return subprocess.check_output(['git',*args],cwd=repository,stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:raise ValueError('Missing or invalid pinned Git skill source') from exc
    if git('cat-file','-t',source_tree).strip()!=b'tree':raise ValueError('Expected a Git tree object')
    payload={}
    for row in git('ls-tree','-r','-z',source_tree,'--',prefix+'/').split(b'\0'):
        if not row:continue
        meta,name=row.split(b'\t',1);mode,kind,oid=meta.decode().split()
        name=name.decode('utf-8').removeprefix(prefix+'/')
        if mode not in {'100644','100755'} or kind!='blob' or name not in MEMBERS:
            raise ValueError('Unexpected pinned skill member')
        data=git('cat-file','blob',oid)
        if len(data)>256*1024:raise ValueError('Oversized pinned skill member')
        payload[name]=data
    if set(payload)!=MEMBERS:raise ValueError('Pinned skill is incomplete')
    with tempfile.TemporaryDirectory(prefix='plc-skill-build-') as d:
        candidate=Path(d).resolve()
        for name,data in payload.items():
            p=candidate/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
        validate(candidate)
    return payload


def build(root: Path, output: Path, source_tree: str, repository: Path=ROOT) -> dict:
    if not re.fullmatch('[a-f0-9]{40}', source_tree):
        raise ValueError('Exact Git source tree identity required')
    members=pinned_skill(root,source_tree,repository)
    if output.exists() or output.is_symlink():
        raise ValueError('Output must be new; artifacts are immutable')
    if not output.parent.is_dir():
        raise ValueError('Choose an existing output directory')
    payload = io.BytesIO()
    provenance = dict(schema_version=1,source_tree=source_tree,skill_version='1.0.0',
                      license='MIT',files={name:sha(data) for name,data in sorted(members.items())},evidence_scope='SOFTWARE_ONLY')
    with zipfile.ZipFile(payload,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name in sorted(MEMBERS | {'PACKAGE_MANIFEST.json'}):
            data = (json.dumps(provenance,sort_keys=True,indent=2)+'\n').encode() if name=='PACKAGE_MANIFEST.json' else members[name]
            info = zipfile.ZipInfo('delta-plc-engineering/'+name, date_time=(2026,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            z.writestr(info,data)
    data=payload.getvalue()
    with output.open('xb') as stream:stream.write(data)
    return dict(status='PASS',source_tree=source_tree,sha256=sha(data),bytes=len(data),members=len(MEMBERS)+1)


def build_source(repository: Path, output: Path, source_tree: str) -> dict:
    """Archive only a reviewed Git tree, never a working directory or history."""
    if not re.fullmatch('[a-f0-9]{40}',source_tree):raise ValueError('Exact source tree required')
    if output.exists() or output.is_symlink():raise ValueError('Source output must be new')
    def git(*args):
        try:return subprocess.check_output(['git',*args],cwd=repository,stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as exc:raise ValueError('Missing or invalid pinned source tree') from exc
    if git('cat-file','-t',source_tree).strip()!=b'tree':raise ValueError('Expected a Git tree object')
    raw=git('ls-tree','-r','-z',source_tree).split(b'\0');files={}
    for row in raw:
        if not row:continue
        meta,name=row.split(b'\t',1);mode,kind,oid=meta.decode().split();name=name.decode('utf-8')
        path=PurePosixPath(name)
        if (kind!='blob' or mode not in {'100644','100755'} or path.is_absolute() or '..' in path.parts
                or '\\' in name or ':' in name
                or name.startswith(('.agent-state/','PLC/','HMI/','profiles/'))
                or path.suffix.lower() in {'.isp','.dvp','.project','.projectarchive','.exe','.dll','.pdb',
                                            '.bin','.zip','.7z','.rar','.gz','.mpu','.fbu','.fcu','.clu'}
                or path.name=='thermal-regression.json'):
            raise ValueError('Non-public or redirected source member: '+name)
        data=git('cat-file','blob',oid)
        files[name]=data
        if len(files)>500 or sum(map(len,files.values()))>64*1024*1024:raise ValueError('Source package limit')
    if 'LICENSE' not in files or 'PUBLIC_SOURCE_MANIFEST.json' not in files:
        raise ValueError('Source attribution/manifest missing')
    manifest=json.loads(files['PUBLIC_SOURCE_MANIFEST.json'])
    expected={n:sha(b) for n,b in files.items() if n!='PUBLIC_SOURCE_MANIFEST.json'}
    if manifest.get('files')!=expected:raise ValueError('Source tree and reviewed manifest differ')
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo('delta-plc-agent/'+name,date_time=(2026,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            z.writestr(info,data)
    data=payload.getvalue()
    with output.open('xb') as stream:stream.write(data)
    return dict(status='PASS',source_tree=source_tree,sha256=sha(data),bytes=len(data),members=len(files),private_git_history=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=SKILL)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--source-tree')
    parser.add_argument('--source',action='store_true',help='Build reviewed source ZIP; --root is the Git repository')
    args=parser.parse_args(argv)
    try:
        result=(build_source(args.root,args.output,args.source_tree or '') if args.source else build(args.root,args.output,args.source_tree or '')) if args.output else validate(args.root)
        print(json.dumps(result,indent=2));return 0
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'FAIL','error':str(exc)}));return 1


if __name__=='__main__':
    raise SystemExit(main())
