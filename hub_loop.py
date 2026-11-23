"""Bounded, isolated work/check/review orchestration; human acceptance stays open."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import uuid

import budget
import ato_core as core
from hub_contract import (HubAgentSpec, HubTurnRequest, blocker_fingerprint,
                          require, validate_review_verdict, validate_task_spec)
from hub_ledger import HubLedger, canonical, private, save
from hub_registry import HubRegistry
from hub_sandbox import validate_loopback_proxy
import hub_worktree
import runner
import recovery
import workflow


def normalized_spec(spec):
    """Freeze defaults using the existing validators before any provisioning."""
    spec=copy.deepcopy(spec);validate_task_spec(spec)
    spec['repo']=str(hub_worktree.repository(spec['repo']))
    contract=spec['worker_contract'];contract.setdefault('memory',False)
    workflow.validate(contract)
    for stage in contract['stages']:
        stage['plan'].setdefault('memory',False)
        runner.validate(stage['plan'])
    hub_worktree.resolve_base(spec['repo'],spec['base_ref'])
    policy=spec['worktree_policy']
    if policy['parent_dir'] is not None:canonical(policy['parent_dir'])
    for name in spec['review_policy']['design_docs']:
        path=Path(spec['repo'])/name
        require(path.is_file() and path==path.resolve(),'UNSAFE_DESIGN_PATH')
    return spec


class GuardedWorkflow(workflow.Workflow):
    """The existing Workflow does not invoke parent_guard; bridge it externally."""
    def __init__(self,directory,guard):
        super().__init__(directory);self.hub_guard=guard

    def guard(self):
        self.hub_guard()
        super().guard()

    def verify_only(self):
        """Run only frozen host checks under the ordinary repository lock."""
        with (self.repo/'.codex/tasks/.worker.lock').open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.state=core.read(self.dir/'state.json')
            self.guard()
            runner.ensure_resumable(self.dir)
            with recovery.execution_owner(self.dir,self.state,explicit_run=True) as ownership:
                self._execution_owner=ownership
                started=core.now()
                self.deadline=budget.deadline(self.dir,self.state,started=started)
                self._accounted_at=started
                self.state.update(status='verifying',owner_pid=os.getpid(),owner_identity=core.process_identity(os.getpid()))
                self.persist()
                try:
                    self.check_total()
                    self.state['status']='needs_input' if all(v['status'] in ('passed','needs_input')
                        for v in self.state['criteria'].values()) else 'blocked'
                except core.Halt as error:self.state['status']=str(error)
                finally:
                    try:self.kill()
                    except OSError:self.state['status']='cleanup_blocked'
                    else:self.state['active_process']=None
                    self.state['owner_pid']=None;self.persist()


class HubLoop:
    def __init__(self,ledger,registry=None,*,worker_tools=None,worker_codex_home=None,proxy=None,notifier=None):
        require(isinstance(ledger,HubLedger));self.ledger=ledger
        self.proxy=validate_loopback_proxy(proxy)
        self.registry=registry if registry is not None else HubRegistry(proxy=self.proxy)
        # Trusted host injection for offline fixtures, never task-controlled.
        self.worker_tools=dict(worker_tools or {})
        require(set(self.worker_tools)<={'CTASK_CODEX_BIN','CTASK_CLAUDE_BIN','CTASK_PI_BIN'},'INVALID_WORKER_TOOLS')
        for value in self.worker_tools.values():canonical(value)
        if worker_codex_home is not None:
            path=canonical(worker_codex_home)
            repo=Path(self.ledger.hub()['repo'])
            require(path.is_dir() and path!=repo and not path.is_relative_to(repo)
                and not path.is_relative_to(self.ledger.root)
                and not self.ledger.root.is_relative_to(path),'UNSAFE_WORKER_CODEX_HOME')
            self.worker_codex_home=path
        else:
            self.worker_codex_home=None
        self.notifier=notifier

    def create_task(self,spec):
        return self.ledger.create_task(normalized_spec(spec))

    def stop(self,identifier):
        # Deliberately independent of .hub.lock: task-run owns that lock while
        # waiting for the child. STOP never grants retry or replays a PID.
        path=self.ledger.task(identifier)/'STOP'
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
        os.close(fd);private(path)
        return dict(status='stop_requested')

    def guard(self,identifier):
        self.ledger.read_task(identifier)
        if (self.ledger.task(identifier)/'STOP').exists():raise core.Halt('stopped')

    def transition(self,identifier,state,status,code=None):
        state.update(status=status,generation=state['generation']+1)
        if code is not None:state['code']=code
        else:state.pop('code',None)
        self.ledger.update_task(identifier,state)
        self.ledger.event(identifier,'transition',status=status,code=code)

    @contextmanager
    def stop_signals(self,identifier):
        previous={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM)}
        try:
            for sig in previous:signal.signal(sig,lambda *_:self.stop(identifier))
            yield
        finally:
            for sig,handler in previous.items():signal.signal(sig,handler)

    def binding(self,spec,state):
        binding=state['worktree'];require(isinstance(binding,dict),'WORKTREE_NOT_BOUND')
        root=hub_worktree.repository(binding['path'])
        require(binding['repo']==spec['repo'] and binding['branch']==spec['branch']
            and root!=Path(spec['repo']) and not root.is_relative_to(Path(spec['repo'])),'WORKTREE_BINDING_CHANGED')
        require(hub_worktree.git(root,'branch','--show-current').strip()==spec['branch']
            and str(Path(hub_worktree.git(root,'rev-parse','--git-common-dir').strip()).resolve())==binding['common_dir'],
            'WORKTREE_BINDING_CHANGED')
        require(hub_worktree.resolve_base(root,binding['base_commit'])==binding['base_commit'],'WORKTREE_BINDING_CHANGED')
        return root

    def barriers(self,spec,state):
        """Do not bypass old STOP, unresolved delivery, active process or owner."""
        root=self.binding(spec,state)
        for name in state.get('workflow_history',[]):
            path=canonical(name)
            require(path.is_relative_to(root/'.codex/tasks/workflows'),'WORKFLOW_BINDING_CHANGED')
            runner.ensure_resumable(path)
            row=core.read(path/'state.json')
            require(not row.get('owner_pid') and not row.get('active_process'),'WORKER_OWNERSHIP_UNRESOLVED')
            require(row['status'] in ('needs_input','completed') and not (path/'STOP').exists(),
                    'PRIOR_WORKFLOW_BARRIER')
            if row.get('active_run'):
                child=canonical(row['active_run'])
                require(child.is_relative_to(root/'.codex/tasks'),'WORKFLOW_BINDING_CHANGED')
                runner.ensure_resumable(child)
                current=core.read(child/'state.json')
                require(current['status']=='completed' and not current.get('owner_pid') and not current.get('active_process'),
                        'WORKER_OWNERSHIP_UNRESOLVED')

    @contextmanager
    def worker_environment(self):
        # Neither implicit origin/controller registration nor worker credentials
        # may escape into the operator's live ATO/native session namespace.
        root=self.ledger.root/'worker-ato';root.mkdir(mode=0o700,exist_ok=True)
        home=self.ledger.root/'worker-home';home.mkdir(mode=0o700,exist_ok=True)
        if self.worker_codex_home is not None:
            native=canonical(self.worker_codex_home)
            for path in (root,home):private(path,directory=True)
        else:
            native=home/'.codex';native.mkdir(mode=0o700,exist_ok=True)
            for path in (root,home,native):private(path,directory=True)
        old=dict(os.environ)
        tools=dict(self.worker_tools)
        for kind in ('codex','claude','pi'):
            name='CTASK_'+kind.upper()+'_BIN'
            if name not in tools:
                found=shutil.which(kind)
                if found:tools[name]=str(Path(found).resolve(strict=True))
        env=dict(PATH='/usr/bin:/bin:/usr/sbin:/sbin',HOME=str(home),LANG='C.UTF-8',
            ATO_HOME=str(root),CODEX_HOME=str(native),PYTHONDONTWRITEBYTECODE='1')
        if self.proxy is not None:
            env.update(HTTP_PROXY=self.proxy,HTTPS_PROXY=self.proxy,http_proxy=self.proxy,https_proxy=self.proxy,
                       NO_PROXY='127.0.0.1,localhost,::1')
        # Resolve code-owned executable names before clearing the ambient PATH;
        # retain only selected executable/Node loader directories, not all PATH.
        node=shutil.which('node')
        parents=[str(Path(value).parent) for value in tools.values()]
        if node:parents.append(str(Path(node).resolve().parent))
        env['PATH']=os.pathsep.join(dict.fromkeys([*parents,*env['PATH'].split(os.pathsep)]))
        env.update(tools)
        try:
            os.environ.clear();os.environ.update(env);yield
        finally:os.environ.clear();os.environ.update(old)

    def worker(self,identifier,spec,state,*,fix=False,checks_only=False):
        root=self.binding(spec,state);self.guard(identifier)
        for name in ('.codex','.codex/tasks'):
            path=root/name
            require(not path.is_symlink() and path==path.resolve(strict=False),'UNSAFE_WORKER_STATE_PATH')
        require(not hub_worktree.git(root,'ls-files','--','.codex/tasks').strip(),'TRACKED_WORKER_STATE_REFUSED')
        contract=copy.deepcopy(spec['worker_contract'])
        if fix:
            blockers=[f for f in state['last_verdict']['findings'] if f['severity']=='blocker']
            commands=[c for c in contract['acceptance'] if c['kind']=='command']
            # No verdict-supplied argv, sandbox, permissions, grants or agent.
            plan=dict(objective='Repair the reviewed blockers within the frozen scope',memory=False,
                tasks=[dict(id='fix_blockers',prompt='Fix only these blocker findings:\n'+json.dumps(blockers),
                            checks=[copy.deepcopy(argv) for c in commands for argv in c['checks']])])
            contract['stages']=[dict(id='hub-fix',covers=[c['id'] for c in commands],plan=plan)]
        self.transition(identifier,state,'running_worker')
        source=self.ledger.task(identifier)/('worker-contract-'+uuid.uuid4().hex+'.json');save(source,contract)
        signals={signum:signal.getsignal(signum) for signum in (signal.SIGINT,signal.SIGTERM)}
        log=self.ledger.task(identifier)/('worker-host-'+uuid.uuid4().hex+'.log')
        fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream,self.worker_environment(),redirect_stdout(stream),redirect_stderr(stream):
            directory=workflow.create(root,source)
            state.setdefault('workflow_history',[]).append(str(directory));state['active_workflow_dir']=str(directory)
            self.ledger.update_task(identifier,state)
            owner=GuardedWorkflow(directory,lambda:self.guard(identifier))
            try:
                if checks_only:owner.verify_only()
                else:
                    # A reviewed repair must execute even when original command
                    # checks already pass. Prestage without modifying Workflow.
                    if fix:
                        preset=owner.contract['stages'][0]
                        owner.stage(preset['plan'],preset['covers'],preset['id'])
                    owner.execute()
            finally:
                for signum,handler in signals.items():signal.signal(signum,handler)
        row=core.read(directory/'state.json')
        state['host_checks']=copy.deepcopy(row['criteria'])
        allowed=all(row['criteria'][c['id']]['status']=='passed' for c in contract['acceptance'] if c['kind']=='command')
        clean=not row.get('owner_pid') and not row.get('active_process') and not row.get('active_run')
        ready=row['status']=='needs_input' or not spec['review_policy']['enabled'] and row['status']=='completed'
        if not (allowed and clean and ready):
            status=row['status'] if row['status'] in ('stopped','cleanup_blocked','limit','needs_host') else 'blocked'
            self.transition(identifier,state,status,'WORKER_NOT_READY');return False
        self.ledger.update_task(identifier,state);return True

    def review(self,identifier,spec,state):
        root=self.binding(spec,state);self.barriers(spec,state);self.guard(identifier)
        if state['review_round']>=spec['review_policy']['max_review_rounds']:
            self.transition(identifier,state,'blocked','review_loop_exhausted');return False
        state['review_round']+=1;self.transition(identifier,state,'reviewing')
        hub=self.ledger.hub();agent=HubAgentSpec(**hub['active_agent']);adapter=self.registry.get(agent.kind)
        sid,mode,provenance=self.ledger.session(adapter.capabilities(),spec['review_policy']['session_mode'])
        bundle=hub_worktree.diff_bundle(state['worktree'])
        prompt=('Read-only technical review. Never edit, acknowledge, grant, merge, push or run a worker. '
            'Return only REVIEW_VERDICT_SCHEMA. Only blocker findings can request repair.\n'
            'handoff-brief.md\n'+self.ledger.brief()+'\nFrozen spec.json\n'+json.dumps(spec)
            +'\nLatest review\n'+json.dumps(state['last_verdict'])+'\nHost check evidence\n'+json.dumps(state.get('host_checks'))
            +'\nBounded source diff\n'+json.dumps(bundle))
        runtime=self.ledger.task(identifier)/'runtime'/('review-'+str(state['review_round']))
        runtime.mkdir(mode=0o700)
        result=adapter.run_turn(HubTurnRequest(agent,root,runtime,prompt,
            snapshot=lambda:hub_worktree.snapshot_integrity(root),
            stop=lambda:(self.ledger.task(identifier)/'STOP').exists(),resume_session_id=sid,
            session_mode=mode,timeout_seconds=spec['review_policy']['timeout_seconds'],provenance=provenance))
        if result.status!='success':
            status=result.status if result.status in ('stopped','cleanup_blocked') else 'blocked'
            self.transition(identifier,state,status,result.code);return False
        verdict=validate_review_verdict(result.output)
        self.ledger.record_session(result.session_id,provenance,workspace=root)
        state['last_verdict']=verdict
        report=self.ledger.task(identifier)/'reviews'/('round-'+str(state['review_round']))
        save(report.with_suffix('.json'),dict(verdict=verdict,metadata=result.metadata,provenance=provenance,
            session_mode_effective=mode,snapshot=bundle['snapshot']))
        fd=os.open(report.with_suffix('.md'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:
            stream.write('# Review round '+str(state['review_round'])+'\n\n'+json.dumps(verdict,ensure_ascii=False,indent=2)+'\n')
        self.ledger.event(identifier,'reviewed',round=state['review_round'],verdict=verdict['verdict'],provenance=provenance,
                          session_mode_effective=mode)
        if verdict['verdict']=='approve':
            self.transition(identifier,state,'ready_for_user_review');return False
        if verdict['verdict']=='blocked':
            self.transition(identifier,state,'blocked','REVIEWER_BLOCKED');return False
        fingerprint=blocker_fingerprint(bundle['snapshot']['tree_sha'],verdict)
        if fingerprint in state['blocker_fingerprints']:
            self.transition(identifier,state,'blocked','repeated_blockers');return False
        state['blocker_fingerprints'].append(fingerprint)
        if state['review_round']>=spec['review_policy']['max_review_rounds']:
            self.transition(identifier,state,'blocked','review_loop_exhausted');return False
        self.transition(identifier,state,'fixing');return True

    def run(self,identifier,*,review_only=False):
        with self.ledger.locked(),self.stop_signals(identifier):
            spec,state=self.ledger.read_task(identifier)
            # ready is a hard terminal; interrupted execution never adopts a
            # saved process/session automatically. An operator must investigate.
            if state['status']=='ready_for_user_review':return state
            if state['status'] in ('stopped','cleanup_blocked','cleaned','error','limit') or state.get('code') in ('INTERRUPTED_OPERATION','REVIEWER_MUTATION_VIOLATION'):
                return state
            if state['status'] in ('running_worker','reviewing','fixing'):
                self.transition(identifier,state,'blocked','INTERRUPTED_OPERATION');return state
            if not review_only and state['status']!='created':return state
            try:
                self.guard(identifier)
                if not state['worktree']:
                    require(not review_only,'WORKTREE_NOT_BOUND')
                    policy=spec['worktree_policy']
                    state['worktree']=hub_worktree.provision(spec['repo'],identifier,spec['branch'],spec['base_ref'],**policy)
                    self.ledger.update_task(identifier,state)
                self.barriers(spec,state)
                if not self.worker(identifier,spec,state,checks_only=review_only):return state
                if not spec['review_policy']['enabled']:
                    self.transition(identifier,state,'needs_input','MANUAL_REVIEW_REQUIRED');return state
                require(state['review_round']<spec['review_policy']['max_review_rounds'],'REVIEW_ROUNDS_EXHAUSTED')
                while self.review(identifier,spec,state):
                    if review_only:
                        self.transition(identifier,state,'blocked','REVIEW_ONLY_CHANGES_REQUIRED');break
                    if not self.worker(identifier,spec,state,fix=True):break
            except core.Halt:
                self.transition(identifier,state,'stopped','STOPPED')
            except (OSError,ValueError,KeyError,TypeError):
                # Public ledger never includes native stderr, arbitrary exception
                # text, environment values or model-supplied control fields.
                self.transition(identifier,state,'blocked','HUB_OPERATION_REFUSED')
            finally:
                self.ledger.brief()
                if (state['status'] in ('ready_for_user_review','blocked','stopped','cleanup_blocked')
                        and state.get('notice')!='delivery_unknown'
                        and state.get('notice_status')!=state['status']):
                    # A durable outbox marker is not a delivery claim. A trusted
                    # host may supply a notifier; unknown delivery is never retried.
                    state['notice']='recorded_only' if self.notifier is None else 'delivery_unknown'
                    state['notice_status']=state['status']
                    self.ledger.update_task(identifier,state)
                    if self.notifier is not None:
                        try:self.notifier(dict(hub=self.ledger.identifier,task=identifier,status=state['status']))
                        except Exception:pass
                        else:
                            state['notice']='delivered';self.ledger.update_task(identifier,state)
            return state
