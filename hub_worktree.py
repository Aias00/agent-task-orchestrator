"""Isolated Git provisioning and content-sensitive review tripwire."""
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess

import ato_core as core
from hub_contract import SHARED_ENV_LINKS

SHARED = SHARED_ENV_LINKS


def canonical(value, *, exists=True):
    path=Path(value)
    if not path.is_absolute() or path!=path.resolve(strict=exists):raise ValueError('UNSAFE_WORKTREE_PATH')
    return path


def git(repo, *args):
    env=dict(PATH='/usr/bin:/bin',LANG='C')
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_OPTIONAL_LOCKS='0')
    result=subprocess.run(['git','-C',str(repo),*args],env=env,capture_output=True,timeout=30)
    if result.returncode:raise ValueError('GIT_OPERATION_REFUSED')
    return result.stdout.decode('utf-8')


def repository(value):
    repo=canonical(value)
    if Path(git(repo,'rev-parse','--show-toplevel').strip())!=repo:raise ValueError('EXACT_REPOSITORY_REQUIRED')
    return repo


def resolve_base(repo, base):
    if not isinstance(base,str) or not base or base.startswith('-'):raise ValueError('INVALID_BASE_REF')
    return git(repo,'rev-parse','--verify',base+'^{commit}').strip()


def provision(repo, task_id, branch, base_ref, *, parent_dir=None, shared_env_links=()):
    repo=repository(repo)
    if (not isinstance(task_id,str) or not task_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in task_id)
            or not isinstance(branch,str) or branch.startswith('-') or branch in ('main','master')):
        raise ValueError('INVALID_WORKTREE_IDENTITY')
    git(repo,'check-ref-format','refs/heads/'+branch)
    base=resolve_base(repo,base_ref)
    parent=canonical(parent_dir) if parent_dir else repo.parent
    target=parent/(repo.name+'-hub-'+task_id)
    if parent==repo or parent.is_relative_to(repo) or target.exists() or target.is_symlink():
        raise ValueError('WORKTREE_COLLISION')
    if git(repo,'for-each-ref','--format=%(refname)','refs/heads/'+branch).strip():raise ValueError('BRANCH_COLLISION')
    if len(set(shared_env_links))!=len(shared_env_links) or not set(shared_env_links)<=SHARED:
        raise ValueError('UNREVIEWED_SHARED_DEPENDENCY')
    for name in shared_env_links:
        source=repo/name
        if (not source.is_dir() or source.is_symlink() or source!=source.resolve() or git(repo,'ls-files','--',name).strip()
                or subprocess.run(['git','-C',str(repo),'check-ignore','--no-index','-q','--',name],capture_output=True).returncode):
            raise ValueError('SHARED_DEPENDENCY_NOT_IGNORED')
    git(repo,'worktree','add','-b',branch,str(target),base)
    # A failure after Git creation preserves the new worktree for inspection.
    for name in shared_env_links:
        destination=target/name
        if destination.exists() or destination.is_symlink() or git(target,'ls-files','--',name).strip():
            raise ValueError('SHARED_DEPENDENCY_COLLISION')
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.symlink_to(os.path.relpath(repo/name,destination.parent),target_is_directory=True)
    return dict(repo=str(repo),path=str(target),branch=branch,base_commit=base,
                common_dir=str(Path(git(target,'rev-parse','--git-common-dir').strip()).resolve()),shared_env_links=list(shared_env_links))


def snapshot_integrity(worktree):
    root=repository(worktree);files={};total=0;shared=set()
    for name in SHARED:
        path=root/name
        # Vendored/tracked directories are source, even when their basename is
        # a supported dependency name. Only shared links/ignored untracked dirs
        # are excluded from traversal.
        excluded=path.is_symlink() or (path.is_dir() and not git(root,'ls-files','--',name).strip()
            and subprocess.run(['git','-C',str(root),'check-ignore','--no-index','-q','--',name],capture_output=True).returncode==0)
        if excluded:
            shared.add(name)
            info=path.lstat()
            files[name]=['shared-entry',info.st_mode,info.st_ino,os.readlink(path) if path.is_symlink() else None]
    # Include ignored task/scratch files as well, except explicitly shared
    # development dependencies. Symlinks are recorded, never traversed.
    for directory,folders,names in os.walk(root,followlinks=False):
        base=Path(directory)
        folders[:]=sorted(name for name in folders if name!='.git'
            and str((base/name).relative_to(root)) not in shared)
        for name in sorted([*names,*[n for n in folders if (base/n).is_symlink()]]):
            path=base/name;relative=str(path.relative_to(root));info=path.lstat()
            if relative in shared:continue
            if stat.S_ISLNK(info.st_mode):value=['link',os.readlink(path)]
            elif stat.S_ISREG(info.st_mode):
                total+=info.st_size
                if info.st_size>64*1024*1024 or total>256*1024*1024 or len(files)>20000:raise ValueError('INTEGRITY_BOUND_EXCEEDED')
                value=['file',stat.S_IMODE(info.st_mode),hashlib.sha256(path.read_bytes()).hexdigest()]
            else:raise ValueError('UNSAFE_REVIEW_FILE')
            files[relative]=value
    head=git(root,'rev-parse','HEAD').strip()
    # A blocker refers to the current source, including uncommitted fixes, not
    # just HEAD. Ignore task receipts for this fingerprint, but retain them in
    # files_sha above so a reviewer cannot mutate ignored files unnoticed.
    source_names=git(root,'ls-files','--cached','--others','--exclude-standard','-z').split('\x00')
    source={name:files.get(name,['missing']) for name in source_names if name and name not in shared}
    return dict(head_sha=head,tree_sha=core.digest(source),head_tree_sha=git(root,'rev-parse','HEAD^{tree}').strip(),
        status_porcelain=git(root,'status','--porcelain=v1','--untracked-files=all'),files_sha=core.digest(files),
        index_sha=hashlib.sha256(git(root,'diff','--cached','--binary','HEAD').encode()).hexdigest())


def assert_integrity(before, after):
    if before!=after:raise ValueError('REVIEWER_MUTATION_VIOLATION')


def diff_bundle(binding):
    root=repository(binding['path']);base=resolve_base(root,binding['base_commit'])
    diff=git(root,'diff','--no-ext-diff','--binary',base,'--')
    if len(diff.encode())>256*1024:raise ValueError('REVIEW_DIFF_BOUND_EXCEEDED')
    untracked={}
    for name in git(root,'ls-files','--others','--exclude-standard','-z').split('\x00'):
        if not name or name in SHARED:continue
        path=root/name
        if path.is_symlink():untracked[name]={'symlink':os.readlink(path)}
        else:
            if path.stat().st_size>128*1024:raise ValueError('REVIEW_DIFF_BOUND_EXCEEDED')
            untracked[name]=path.read_text(errors='replace')
    if len(str(untracked).encode())+len(diff.encode())>256*1024:raise ValueError('REVIEW_DIFF_BOUND_EXCEEDED')
    return dict(base_commit=base,head=git(root,'rev-parse','HEAD').strip(),diff=diff,untracked=untracked,
                status=git(root,'status','--porcelain=v1','--untracked-files=all'),snapshot=snapshot_integrity(root))


def cleanup(binding):
    root=repository(binding['path']);repo=repository(binding['repo'])
    if root==repo or root.is_relative_to(repo) or git(root,'branch','--show-current').strip()!=binding['branch']:
        raise ValueError('WORKTREE_BINDING_CHANGED')
    common=Path(git(root,'rev-parse','--git-common-dir').strip()).resolve()
    if str(common)!=binding['common_dir']:raise ValueError('WORKTREE_BINDING_CHANGED')
    status=git(root,'status','--porcelain=v1','--untracked-files=all').splitlines()
    # A directory-only .gitignore rule does not match a symlink. Only the
    # explicitly provisioned, unchanged shared link is exempt from dirty state;
    # this never changes the main repository's Git config/excludes or sources.
    for name in binding.get('shared_env_links',[]):
        path=root/name
        source=repo/name
        if (name not in SHARED or not path.is_symlink() or not source.is_dir() or source!=source.resolve()
                or os.readlink(path)!=os.path.relpath(source,path.parent) or path.resolve()!=source):
            raise ValueError('SHARED_DEPENDENCY_CHANGED')
        status=[line for line in status if line!='?? '+name]
    if status:raise ValueError('DIRTY_WORKTREE')
    handles=[]
    try:
        for directory in (root,repo):
            lock=directory/'.codex/tasks/.worker.lock'
            if lock.is_symlink():raise ValueError('UNSAFE_WORKER_LOCK')
            if lock.exists():
                stream=lock.open('r');handles.append(stream)
                try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ValueError('WORKTREE_BUSY') from None
        # Remove only the already-verified, provisioned links. Git treats a
        # directory-only ignore pattern differently for symlinks; never use
        # --force to hide another dirty file or delete the dependency target.
        for name in binding.get('shared_env_links',[]):(root/name).unlink()
        git(repo,'worktree','remove',str(root))  # Explicit human command only; retain branch.
    finally:
        for stream in handles:stream.close()
