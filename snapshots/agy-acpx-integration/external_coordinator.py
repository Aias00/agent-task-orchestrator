"""Scoped external CLI over the canonical ATO workflow ledger.

Request receipts are admission barriers/locators, never a second task ledger.
There is deliberately no background launcher, human decision API or grant API.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import ato_config
import local_session_adapter as local
import recovery
import runner as core
import session_router
import workflow

SCOPE = 'external-coordinator-v1'
ACTIONS = frozenset({'init', 'submit', 'status', 'events', 'result', 'stop', 'continue'})


def isolated_roots():
    if not os.environ.get('ATO_HOME') or not os.environ.get('CODEX_HOME'):
        raise ValueError('explicit private ATO_HOME and CODEX_HOME required')
    ato, native = ato_config.ato_home(), ato_config.codex_home()
    if (ato == native or ato in native.parents or native in ato.parents
            or ato == Path.home()/'.codex' or native == Path.home()/'.codex'):
        raise ValueError('external CLI requires separate private homes; global native state is forbidden')
    ato.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = ato.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('ATO_HOME must be private (0700) and owned by this user')
    return ato, native


def repository(path):
    path = Path(path).resolve(strict=True)
    root = Path(subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
        check=True, capture_output=True, text=True, timeout=5).stdout.strip()).resolve()
    if path != root:
        raise ValueError('scope must name the exact Git repository root')
    ato, native = isolated_roots()
    if any(p == root or root in p.parents or p in root.parents for p in (ato, native)):
        raise ValueError('private authority/native homes must be outside and separate from the repository')
    # Canonical workflow storage/locks must never escape through symlinks.
    for relative in ('.git', '.codex', '.codex/tasks', '.codex/tasks/workflows'):
        if (root/relative).is_symlink():
            raise ValueError('external workflow authority paths cannot be symlinks')
    return root


def reviewed_contract(value):
    value = workflow.validate(copy.deepcopy(value))
    if value['memory'] or value['sandbox'] not in ('read-only', 'workspace-write'):
        raise ValueError('external contracts require memory=false and a restricted sandbox')
    if value.get('permissions') or value.get('host_handoff') or value.get('budget'):
        raise ValueError('external coordinator cannot grant permissions, host actions or budget changes')
    if not value.get('agent'):
        raise ValueError('external contract must explicitly select a registered worker')
    # No implicit/default native planner. This endpoint executes only reviewed stages.
    if not value['stages']:
        raise ValueError('external contract requires reviewed stages; automatic planning is unsupported')
    for stage in value['stages']:
        plan = stage['plan']
        if any(plan.get(key) for key in ('permissions', 'host_handoff', 'budget')):
            raise ValueError('reviewed stages cannot add permissions, host actions or budget changes')
        for key in ('agent', 'sandbox', 'memory', 'model'):
            if key in plan and plan[key] != value.get(key):
                raise ValueError('reviewed stage cannot override workflow '+key)
    return value


def fingerprint(repo, contract_hash):
    return core.digest(dict(repo=str(repo), contract_hash=contract_hash))


def requests():
    import interaction
    directory = interaction.directory()/'external-requests'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise ValueError('request receipt directory must be private')
    return directory


def load_scope(actor_file):
    isolated_roots()
    proof, reference, _ = local.load_actor_file(actor_file)
    credential = local._verified_credential(reference, proof)
    scope = credential.get('binding')
    if (credential.get('role') != 'origin' or not isinstance(scope, dict)
            or scope.get('type') != SCOPE):
        raise ValueError('requires an operator-provisioned external origin capability')
    repo = repository(scope['repo'])
    contract = reviewed_contract(scope['contract'])
    if (core.digest(contract) != scope['contract_hash']
            or fingerprint(repo, scope['contract_hash']) != scope['request']):
        raise ValueError('external scope contract fingerprint changed')
    session, _ = session_router.resolve_v2(reference, project=repo)
    if session.identity.adapter != 'local_cli':
        raise ValueError('external origin must be a private local_cli endpoint')
    local.identity_record(session.identity.native_id, repo)
    return proof, reference, scope


def receipt(scope):
    path = requests()/(scope['request']+'.json')
    return core.read(path) if path.exists() else None


def bound_workflow(scope, reference, requested=None):
    row = receipt(scope)
    if not row or not row.get('workflow'):
        raise ValueError('request has no reconciled workflow; unknown creation must not be replayed')
    d = Path(row['workflow'])
    repo = Path(scope['repo'])
    if (d.resolve() != d or d.parent != repo/'.codex/tasks/workflows'
            or not re.fullmatch(r'[0-9]{8}-[0-9]{6}-[0-9a-f]{8}', d.name)
            or (requested is not None and Path(requested).resolve() != d)):
        raise ValueError('workflow is outside this exact request/repository scope')
    state, contract = core.read(d/'state.json'), core.read(d/'contract.json')
    origin = core.read(d/'origin.json')
    if (state.get('kind') != 'workflow' or state['repo'] != scope['repo']
            or state['contract_hash'] != scope['contract_hash']
            or core.digest(contract) != scope['contract_hash']
            or origin.get('origin_session_ref') != reference
            or row['reference'] != reference or row['request'] != scope['request']):
        raise ValueError('canonical workflow, origin or frozen contract binding mismatch')
    return d, state


def authenticate_scope_binding(binding, reference, *, workflow_id, contract_hash, generation):
    """Called by the local_cli adapter after token and role verification.

    Exact contract capability lasts for this workflow lifecycle, never user role.
    The current revision is still required by canonical session authorization.
    """
    d, state = bound_workflow(binding, reference, workflow_id)
    if (contract_hash != binding['contract_hash']
            or generation != state.get('execution_revision', 0)):
        raise ValueError('external origin generation/contract mismatch')
    return str(d)


def authorized(actor_file, requested=None):
    proof, reference, scope = load_scope(actor_file)
    d, state = bound_workflow(scope, reference, requested)
    actor = session_router.authenticate_actor(reference, proof, role='origin', project=scope['repo'],
        workflow_id=str(d), contract_hash=scope['contract_hash'], generation=state.get('execution_revision', 0))
    session_router.authorize_actor(actor, 'workflow.report', project=scope['repo'], workflow_id=str(d),
        contract_hash=scope['contract_hash'], generation=state.get('execution_revision', 0))
    return d, state, scope


def view(directory):
    # Do not expose prompts, argv, model text, actor files or token material.
    state = core.read(directory/'state.json')
    stages = []
    for row in state['stages']:
        child = core.read(Path(row['run_dir'])/'state.json')
        stages.append(dict(directory=row['run_dir'], status=child['status'],
            tasks={k: {field: v.get(field) for field in ('status', 'attempts', 'attempt_sequence',
                'acpx_delivery_unknown', 'acpx_requires_reconciliation', 'evidence')}
                for k, v in child['tasks'].items()}))
    return dict(workflow=str(directory), status=state['status'], completion_scope='overall',
        criteria=state['criteria'], remaining=[k for k, v in state['criteria'].items() if v['status'] != 'passed'],
        stages=stages, stopped=(directory/'STOP').exists(), owner_pid=state.get('owner_pid'),
        owner_alive=recovery.alive(state.get('owner_pid')), active_run=state.get('active_run'),
        execution_revision=state.get('execution_revision', 0), planning_attempts=state['planning_attempts'],
        elapsed_seconds=state['elapsed_seconds'], native_origin_activity='unknown',
        native_resume='unsupported', human_acceptance_inferred=False)


def submit(actor_file, contract_file, run=False):
    _, reference, scope = load_scope(actor_file)
    contract = reviewed_contract(core.read(contract_file))
    if core.digest(contract) != scope['contract_hash']:
        raise ValueError('submitted contract differs from exact operator-reviewed contract')
    path = requests()/(scope['request']+'.json')
    created = False
    with recovery.locked(requests()/(scope['request']+'.lock')):
        if path.exists():
            row = core.read(path)
            if not row.get('workflow'):
                return dict(request=scope['request'], admission='delivery_unknown',
                    next_step='operator must reconcile original creation; no replay')
            directory, _ = bound_workflow(scope, reference)
        else:
            # Durable barrier BEFORE create. A crash/partial create never makes a second workflow.
            local.private_create(path, dict(request=scope['request'], reference=reference,
                workflow=None, source='canonical ATO workflow creation admission'))
            frozen = requests()/(scope['request']+'.contract.json')
            local.private_create(frozen, scope['contract'])
            directory = workflow.create(scope['repo'], frozen, origin_ref=reference)
            core.save(path, dict(request=scope['request'], reference=reference, workflow=str(directory)))
            created = True
    if created and run:
        continue_workflow(actor_file, directory)
    return dict(request=scope['request'], admission='created' if created else 'existing', **view(directory))


class ScopedWorkflow(workflow.Workflow):
    def propose(self, memory):
        # This is a canonical workflow terminal condition, not an implicit model planner.
        return dict(decision='blocked', reason='Reviewed stages exhausted; remaining checks require operator review',
                    tasks=[], retry_after_seconds=1)


def continue_workflow(actor_file, requested=None):
    d, _, _ = authorized(actor_file, requested)
    # Serialize admission, including the gap before canonical execution takes its repo lock.
    with recovery.locked(d/'external-continuation.lock', blocking=False):
        d, state, _ = authorized(actor_file, requested)
        if (d/'STOP').exists() or (d/'user-closure.json').exists():
            raise ValueError('STOP/closure cannot be cleared by the external origin')
        if state['status'] not in ('created', 'blocked', 'needs_input', 'completed'):
            raise ValueError('active/interrupted/unknown execution requires operator reconciliation; no replay')
        core.ensure_resumable(d)
        for row in state['stages']:
            child = core.read(Path(row['run_dir'])/'state.json')
            if (state.get('active_run') == row['run_dir']
                    and any(task.get('attempts', 0) for task in child['tasks'].values())):
                # Legacy generic adapters may reset completed sibling attempts
                # while reopening a failed stage. This surface never admits that
                # retry path. acpx reconciliation is likewise operator-only.
                raise ValueError('consumed incomplete stage requires operator review; attempts cannot be reset')
            for task in child['tasks'].values():
                if (task.get('acpx_delivery_unknown') or task.get('acpx_requires_reconciliation')
                        or task['status'] in ('running', 'waiting')):
                    raise ValueError('unknown or unreconciled delivery cannot be continued/replayed')
        # retry=False preserves all attempts. Canonical execute owns repo flock,
        # fenced mailbox lease, STOP propagation and all independent final checks.
        ScopedWorkflow(d).execute(retry=False)
    return view(d)


def stop(actor_file, requested=None):
    d, _, _ = authorized(actor_file, requested)
    (d/'STOP').touch()
    return dict(workflow=str(d), stop_requested=True, acceptance_completed=False)


def events(directory, cursor=0, limit=50):
    if not 0 <= cursor or not 1 <= limit <= 200:
        raise ValueError('invalid event cursor/limit')
    path = directory/'events.jsonl'
    selected, position, scanned = [], 0, 0
    if path.exists():
        with path.open('rb') as stream:
            stream.seek(cursor)
            position = cursor
            while len(selected) < limit and scanned < 1024*1024:
                raw = stream.readline(65537)
                if not raw or not raw.endswith(b'\n'):
                    break  # Partial append is retried at the same cursor.
                scanned += len(raw)
                position = stream.tell()
                if len(raw) > 65536:
                    raise ValueError('event exceeds safe read bound')
                row = json.loads(raw)
                selected.append({key: row[key] for key in ('time', 'event', 'status', 'run_dir') if key in row})
    return dict(events=selected, cursor=position, human_read_recorded=False)


def result(directory):
    current = view(directory)
    current['accepted'] = (current['status'] == 'completed' and not current['remaining']
                           and not current['stopped'] and not current['owner_pid'])
    current['basis'] = 'canonical independent ATO criteria; assistant replies/EOF are not acceptance'
    return current


def main(argv=None):
    parser = argparse.ArgumentParser(description='Scoped ATO external origin (no human/grant/hub operations)')
    parser.add_argument('--actor-file', required=True, help='explicit operator-provisioned private origin proof')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('init', help='verify existing scope; does not provision credentials')
    p = sub.add_parser('submit'); p.add_argument('--contract', required=True); p.add_argument('--run', action='store_true')
    for action in ('status', 'events', 'result', 'stop', 'continue'):
        p = sub.add_parser(action); p.add_argument('--workflow')
        if action == 'events':
            p.add_argument('--cursor', type=int, default=0); p.add_argument('--limit', type=int, default=50)
    args = parser.parse_args(argv)
    try:
        if args.action == 'init':
            _, reference, scope = load_scope(args.actor_file)
            output = dict(repo=scope['repo'], contract_hash=scope['contract_hash'], request=scope['request'],
                origin=reference, actions=sorted(ACTIONS), native_session='external CLI endpoint; AGY activity unknown')
        elif args.action == 'submit':
            output = submit(args.actor_file, args.contract, args.run)
        elif args.action == 'continue':
            output = continue_workflow(args.actor_file, args.workflow)
        elif args.action == 'stop':
            output = stop(args.actor_file, args.workflow)
        else:
            d, _, _ = authorized(args.actor_file, args.workflow)
            output = events(d, args.cursor, args.limit) if args.action == 'events' else result(d) if args.action == 'result' else view(d)
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, core.Halt, subprocess.SubprocessError):
        # Native paths, token challenges and exception bodies are deliberately private.
        print(json.dumps(dict(error='ATO external command refused; verify exact scope, private actor, STOP and canonical evidence')),
              file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
