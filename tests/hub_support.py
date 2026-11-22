"""Private synthetic hubs and Git repositories; no native/model credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.PIPE).decode().strip()


def repository(root):
    repo=Path(root)/'project';repo.mkdir()
    git(repo,'init','-q','-b','main');git(repo,'config','user.name','Fixture');git(repo,'config','user.email','fixture@example.invalid')
    (repo/'.gitignore').write_text('.codex/\n.venv/\n__pycache__/\n')
    (repo/'result.txt').write_text('initial')
    git(repo,'add','.');git(repo,'commit','-qm','synthetic baseline')
    return repo


def verdict(decision='approve',severity=None):
    return dict(verdict=decision,summary='synthetic review',findings=[] if severity is None else [dict(
        id='F1',severity=severity,file='result.txt',lines='L1',category='correctness',title='Fix the fixture',
        evidence='synthetic source observation',required_fix='Write fixed result')],checked_items=['source and checks'],residual_risks=[])


def spec(repo,task='task-one',rounds=2):
    checks=[[sys.executable,'-c',"from pathlib import Path; assert Path('result.txt').read_text().startswith('done')"]]
    return dict(schema_version=1,task_id=task,repo=str(repo),base_ref='main',branch='feat/'+task,
        worker_contract=dict(objective='Synthetic offline change',scope='result.txt only',authorization='Synthetic fixture only',
            memory=False,sandbox='workspace-write',agent=dict(kind='codex'),
            acceptance=[dict(id='result',description='actual host check',kind='command',checks=checks),
                        dict(id='human',description='human final review',kind='manual')],
            stages=[dict(id='implement',covers=['result'],plan=dict(objective='Synthetic change',memory=False,
                tasks=[dict(id='write',prompt='Write done to result.txt',checks=checks)]))]),
        review_policy=dict(enabled=True,agent=dict(kind='agy',model=None,effort='high'),
            session_mode='resume_if_supported',max_review_rounds=rounds,timeout_seconds=5,
            checklist=['Check synthetic changes'],design_docs=[]),
        worktree_policy=dict(parent_dir=None,shared_env_links=[]))


def runtime(root):
    path=Path(root)/'runtime';path.mkdir(mode=0o700)
    for name in ('home','tmp','trusted'):(path/name).mkdir(mode=0o700)
    return path
