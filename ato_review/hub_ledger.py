"""Private hub ledger; no native calls, implicit adoption or task execution."""
from contextlib import contextmanager
from dataclasses import asdict
from functools import wraps
import fcntl
import json
import os
from pathlib import Path
import stat
import time
import threading
import uuid

from ato_common import ato_config
from ato_common import ato_core as core
from ato_review import hub_worktree
from ato_review.hub_contract import HubAgentSpec, HubError, digest, integer, require, strict_json, token, validate_task_spec


def canonical(value, *, exists=True):
    path=Path(value)
    require(path.is_absolute() and path==path.resolve(strict=exists),'UNSAFE_LEDGER_PATH')
    return path


def private(path, *, directory=False):
    path=canonical(path);info=path.lstat()
    require(info.st_uid==os.getuid() and stat.S_IMODE(info.st_mode)==(0o700 if directory else 0o600)
        and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink==1),'UNSAFE_LEDGER_MODE')
    return path


def load(path):
    path=private(path)
    require(path.stat().st_size<=1024*1024,'LEDGER_BOUND_EXCEEDED')
    return strict_json(path.read_bytes())


def save(path, value):
    path=Path(path);private(path.parent,directory=True)
    if path.exists() or path.is_symlink():private(path)
    core.private_save(path,value)


def mutation(method):
    @wraps(method)
    def call(self,*args,**kwargs):
        if self._lock_owner==threading.get_ident():return method(self,*args,**kwargs)
        with self.locked():return method(self,*args,**kwargs)
    return call


class HubLedger:
    def __init__(self, identifier):
        self.identifier=token(identifier)
        self._lock_owner=None
        self.root=canonical(ato_config.ato_home(),exists=False)/'hubs'/identifier

    @classmethod
    def list_hubs(cls, repo=None):
        require(os.environ.get('ATO_HOME'),'EXPLICIT_ATO_HOME_REQUIRED')
        selected=str(hub_worktree.repository(repo)) if repo is not None else None
        parent=canonical(ato_config.ato_home(),exists=False)/'hubs'
        if not parent.exists() and not parent.is_symlink():return []
        private(parent,directory=True);result=[]
        for path in sorted(parent.glob('hub-*')):
            private(path,directory=True);ledger=cls(path.name);row=ledger.hub()
            require(isinstance(row['repo'],str),'INVALID_HUB_REPOSITORY')
            stored_repo=str(canonical(row['repo'],exists=False))
            if selected is not None and stored_repo!=selected:continue
            agent=HubAgentSpec(**{name:row['active_agent'].get(name) for name in ('kind','model','effort')})
            session=row['native_sessions'].get(agent.kind,{})
            state=session.get('state','none')
            require(state in ('none','active','parked','retired'),'INVALID_SESSION_STATE')
            tasks=private(path/'tasks',directory=True)
            children=list(tasks.iterdir())
            for child in children:private(child,directory=True)
            # Observation must not create .hub.lock (busy() normally does).
            busy=False;lock=path/'.hub.lock'
            if lock.exists() or lock.is_symlink():
                private(lock);fd=os.open(lock,os.O_RDONLY|os.O_NOFOLLOW)
                try:
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:busy=True
                finally:os.close(fd)
            result.append(dict(hub_id=path.name,repo=stored_repo,active_agent=asdict(agent),
                hub_busy=busy,active_session_state=state,task_count=len(children)))
        return result

    @classmethod
    def create(cls, repo, agent, *, max_turns=20):
        repo=canonical(repo);integer(max_turns,1,1000)
        require(type(agent) is HubAgentSpec)
        require(os.environ.get('ATO_HOME'),'EXPLICIT_ATO_HOME_REQUIRED')
        base=canonical(ato_config.ato_home(),exists=False)
        require(not base.is_relative_to(repo) and not repo.is_relative_to(base),'LEDGER_OUTSIDE_REPOSITORY_REQUIRED')
        base.mkdir(parents=True,exist_ok=True,mode=0o700)
        parent=base/'hubs';parent.mkdir(exist_ok=True,mode=0o700);private(parent,directory=True)
        identifier='hub-'+uuid.uuid4().hex;ledger=cls(identifier);ledger.root.mkdir(mode=0o700)
        (ledger.root/'tasks').mkdir(mode=0o700)
        save(ledger.root/'hub.json',dict(version=1,id=identifier,repo=str(repo),active_agent=asdict(agent),
            native_sessions={},session_rollover_policy=dict(max_turns=max_turns),generation=0,next_provenance='fresh_only'))
        ledger.brief();return ledger

    def hub(self):
        private(self.root,directory=True);row=load(self.root/'hub.json')
        require(row.get('id')==self.identifier and row.get('version')==1,'HUB_IDENTITY_MISMATCH')
        return row

    @contextmanager
    def locked(self):
        private(self.root,directory=True);path=self.root/'.hub.lock'
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        acquired=False
        try:
            private(path)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise HubError('HUB_BUSY') from None
            acquired=True
            self._lock_owner=threading.get_ident()
            yield self
        finally:
            if acquired:self._lock_owner=None
            os.close(fd)

    def busy(self):
        try:
            with self.locked():return False
        except HubError as error:
            if str(error)=='HUB_BUSY':return True
            raise

    def task(self, identifier):
        path=self.root/'tasks'/token(identifier);private(path,directory=True)
        return path

    @mutation
    def create_task(self, spec):
        validate_task_spec(spec);row=self.hub()
        require(spec['repo']==row['repo'],'TASK_REPOSITORY_MISMATCH')
        path=self.root/'tasks'/spec['task_id'];require(not path.exists(),'TASK_COLLISION')
        path.mkdir(mode=0o700);(path/'reviews').mkdir(mode=0o700);(path/'runtime').mkdir(mode=0o700)
        save(path/'spec.json',spec)
        save(path/'state.json',dict(version=1,task_id=spec['task_id'],spec_hash=digest(spec),status='created',
            worktree=None,active_workflow_dir=None,review_round=0,blocker_fingerprints=[],generation=0,last_verdict=None))
        self.event(spec['task_id'],'created');self.brief()
        return path

    def read_task(self, identifier):
        path=self.task(identifier);spec=load(path/'spec.json');state=load(path/'state.json')
        validate_task_spec(spec)
        require(state['task_id']==identifier and state['spec_hash']==digest(spec),'FROZEN_SPEC_CHANGED')
        require(spec['repo']==self.hub()['repo'],'TASK_REPOSITORY_MISMATCH')
        return spec,state

    @mutation
    def event(self, identifier, kind, **details):
        token(kind);path=self.task(identifier)/'events.jsonl'
        if path.exists():private(path)
        fd=os.open(path,os.O_CREAT|os.O_APPEND|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a') as stream:
            require(stream.tell()<4*1024*1024,'EVENT_BOUND_EXCEEDED')
            stream.write(json.dumps(dict(event=kind,time=time.time(),**details),allow_nan=False)+'\n')
            stream.flush();os.fsync(stream.fileno())

    @mutation
    def update_task(self, identifier, state):
        _,old=self.read_task(identifier)
        require(state['spec_hash']==old['spec_hash'] and state['generation']>=old['generation'],'TASK_GENERATION_CHANGED')
        save(self.task(identifier)/'state.json',state);self.brief()

    @mutation
    def switch_agent(self, agent, source):
        require(type(agent) is HubAgentSpec and isinstance(source,str) and source.strip(),'SWITCH_SOURCE_REQUIRED')
        row=self.hub();old=row['active_agent']['kind']
        if old in row['native_sessions']:row['native_sessions'][old]['state']='parked'
        row.update(active_agent=asdict(agent),generation=row['generation']+1,
                   next_provenance='ledger_reconstruction_from_'+old)
        row.setdefault('agent_history',[]).append(dict(event='agent_switched',old=old,new=agent.kind,at=time.time(),source_sha256=digest(source)))
        save(self.root/'hub.json',row);self.brief()

    @mutation
    def rollover(self, source):
        require(isinstance(source,str) and source.strip(),'ROLLOVER_SOURCE_REQUIRED')
        row=self.hub();kind=row['active_agent']['kind'];session=row['native_sessions'].get(kind)
        if session and session.get('session_id'):
            session.setdefault('retired_sessions',[]).append({k:session[k] for k in ('session_id','provenance','created_at','turn_count')})
            require(len(session['retired_sessions'])<=256,'SESSION_HISTORY_BOUND_EXCEEDED')
            session.update(session_id=None,state='retired',turn_count=0)
        row.update(generation=row['generation']+1,next_provenance='ledger_rollover')
        save(self.root/'hub.json',row);self.brief()

    @mutation
    def session(self, capabilities, mode):
        row=self.hub();kind=row['active_agent']['kind'];session=row['native_sessions'].get(kind)
        if session and session.get('turn_count',0)>=row['session_rollover_policy']['max_turns']:
            self.rollover('automatic max_turns');row=self.hub();session=row['native_sessions'].get(kind)
        require(mode in ('fresh_only','resume_if_supported','require_resume'))
        supported=capabilities.supported('session_resume')
        require(mode!='require_resume' or supported,'RESUME_NOT_QUALIFIED')
        rebuilding=row['next_provenance']!='native_resume'
        effective='resume_if_supported' if mode!='fresh_only' and supported else 'fresh_only'
        sid=session.get('session_id') if session and session.get('state')=='active' and not rebuilding and effective!='fresh_only' else None
        return sid,effective,'native_resume' if sid else row['next_provenance'] if rebuilding else 'ledger_reconstruction'

    @mutation
    def record_session(self, session_id, provenance, *, workspace=None):
        token(session_id);row=self.hub();kind=row['active_agent']['kind'];old=row['native_sessions'].get(kind,{})
        if workspace is not None:workspace=str(canonical(workspace))
        same=old.get('session_id')==session_id and old.get('state')=='active'
        retired=list(old.get('retired_sessions',[]))
        if old.get('session_id') and not same:
            retired.append({k:old[k] for k in ('session_id','provenance','created_at','turn_count')})
            retired=retired[-256:]
        row['native_sessions'][kind]=dict(session_id=session_id,provenance=provenance,
            created_at=old['created_at'] if same else time.time(),last_used_at=time.time(),
            turn_count=old.get('turn_count',0)+1 if same else 1,retired_sessions=retired,state='active',workspace=workspace)
        row['next_provenance']='native_resume';save(self.root/'hub.json',row);self.brief()

    @mutation
    def brief(self):
        row=self.hub();items=[]
        for path in sorted((self.root/'tasks').iterdir()):
            private(path,directory=True);spec,state=self.read_task(path.name)
            items.append(dict(task=path.name,objective=spec['worker_contract']['objective'][:512],status=state['status'],
                branch=spec['branch'],base_commit=(state.get('worktree') or {}).get('base_commit'),
                worktree=(state.get('worktree') or {}).get('path'),review_round=state['review_round'],
                verdict=(state.get('last_verdict') or {}).get('verdict'),
                open_blockers=[f['id'] for f in (state.get('last_verdict') or {}).get('findings',[]) if f['severity']=='blocker'],
                manual=[c['id'] for c in spec['worker_contract']['acceptance'] if c.get('kind')=='manual']))
        body='# Hub ledger handoff\n\nAuthoritative facts; no new authority.\n\n'+json.dumps(items,ensure_ascii=False,indent=2)+'\n'
        require(len(body.encode())<=32768,'HANDOFF_BOUND_EXCEEDED')
        path=self.root/'handoff-brief.md'
        if path.exists():private(path)
        tmp=self.root/('brief-'+uuid.uuid4().hex+'.tmp')
        fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
        return body
