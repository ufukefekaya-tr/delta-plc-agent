"""Inspect the PLC skill goal DAG and invalidate dependent evidence on drift."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
STATES={'PLANNED','READY','RUNNING','VERIFYING','ACCEPTED','BLOCKED','STALE','SUPERSEDED'}


def load(root=ROOT):
    portfolio=json.loads((root/'goals/plc-skill/portfolio.json').read_text('utf-8'))
    goals={}
    for name in portfolio['goals']:
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()):raise ValueError('Goal path escapes repository')
        value=json.loads(path.read_text('utf-8'))
        if not re.fullmatch(r'S[012]-G\d+(?:-\d+)*',value.get('id','')) or path != (root/f'goals/plc-skill/{value["id"]}.json').resolve():
            raise ValueError('Invalid goal identity/path')
        if value['id'] in goals:raise ValueError('Duplicate goal ID')
        goals[value['id']]=value
    return portfolio,goals


def validate(root=ROOT):
    portfolio,goals=load(root);visiting=set();done=set()
    def visit(ident):
        if ident in visiting:raise ValueError('Goal dependency cycle')
        if ident in done:return
        if ident not in goals:raise ValueError('Missing predecessor')
        visiting.add(ident);g=goals[ident]
        if g['status'] not in STATES:raise ValueError('Unknown goal state')
        for dep in g['dependencies']:visit(dep)
        path=(root/g['workflow']).resolve()
        if not path.is_relative_to(root.resolve()):raise ValueError('Workflow escapes repository')
        wf=json.loads(path.read_text('utf-8'))
        if wf['id']!=ident or not wf['steps'] or not g['acceptance_tests']:raise ValueError('Missing inner workflow/acceptance')
        if g['status']=='ACCEPTED':
            if any(goals[d]['status']!='ACCEPTED' for d in g['dependencies']):raise ValueError('Accepted goal has unaccepted predecessor')
            if not g['evidence'] or g['outcome'] not in {'CHANGED','NO_CHANGE','EXCLUDED_WITH_REASON'}:raise ValueError('Acceptance requires evidence and outcome')
        visiting.remove(ident);done.add(ident)
    for ident in goals:visit(ident)
    return portfolio,goals


def drifted(root,goals):
    affected=set()
    for ident,g in goals.items():
        for name,wanted in (g['input_hashes']|g.get('output_hashes',{})).items():
            p=(root/name).resolve()
            if not p.is_relative_to(root.resolve()) or not p.is_file():
                affected.add(ident)
                continue
            data=p.read_bytes()
            if g.get('hash_algorithm')=='sha256-text-lf-v1' and (p.name=='LICENSE' or p.suffix.lower() in {'.py','.md','.json','.toml','.yml','.yaml','.txt','.xml','.csv','.il'}):
                data=data.replace(b'\r\n',b'\n')
            if hashlib.sha256(data).hexdigest()!=wanted:affected.add(ident)
    while True:
        more={i for i,g in goals.items() if any(d in affected for d in g['dependencies'])}
        if more<=affected:return affected
        affected|=more


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--reconcile',action='store_true',help='Mark only drift-affected goals STALE; never execute work')
    args=parser.parse_args(argv)
    try:
        portfolio,goals=validate(args.root);affected=drifted(args.root,goals)
        if args.reconcile:
            for ident in sorted(affected):
                g=goals[ident];g['status']='STALE';g['revision']+=1
                g['history'].append({'decision':'Input drift invalidated this goal and downstream acceptance'})
                p=args.root/f'goals/plc-skill/{ident}.json'
                if p.is_symlink() or p.resolve()!=p.absolute():raise ValueError('Redirected goal output')
                p.write_text(json.dumps(g,indent=2)+'\n')
        result={'status':'DRIFT' if affected else 'PASS','portfolio_status':portfolio['status'],'drifted':sorted(affected),
                'goals':{i:{'status':g['status'],'ready':g['status'] in {'PLANNED','READY'} and i not in affected and all(goals[d]['status']=='ACCEPTED' for d in g['dependencies'])} for i,g in goals.items()}}
        print(json.dumps(result,indent=2));return 1 if affected else 0
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'status':'FAIL','error':str(exc)}));return 1


if __name__=='__main__':raise SystemExit(main())
