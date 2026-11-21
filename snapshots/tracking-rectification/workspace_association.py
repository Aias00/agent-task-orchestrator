"""Explicit observation-only workspace associations for exact legacy workflows.

No actor credentials, workflow files, native metadata or execution grants are
written here. Native evidence is supplied exclusively by the trusted adapter.
"""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import uuid

import interaction
import recovery
import runner as core
import session_router

VERSION = 1
MAX_EVIDENCE_BYTES = 256 * 1024
MAX_RECORDS = 64
MAX_LEDGER_BYTES = 2 * 1024 * 1024


def canonical(value):
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError('an explicit canonical path is required')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('an absolute path is required')
    # Check components before resolve, including symlinks which point inward.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink paths are not workspace proof')
    resolved = path.resolve(strict=True)
    if str(resolved) != os.path.abspath(path):
        raise ValueError('noncanonical workspace path')
    return resolved


def sha(path):
    path = canonical(path)
    if not path.is_file():
        raise ValueError('regular evidence file required')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _adapter():
    return session_router.legacy_workspace_adapter()


def _git(project, workspace):
    # The first qualified relation is a direct child repository. Linked
    # worktrees require a separate explicit proof design and fail closed here.
    if project.parent != workspace or project == workspace:
        raise ValueError('project must be a direct child of the selected workspace')
    gitdir = canonical(project/'.git')
    if not gitdir.is_dir():
        raise ValueError('linked worktree associations are not supported without explicit proof')
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
    values = []
    for flag in ('--show-toplevel', '--absolute-git-dir', '--git-common-dir'):
        result = subprocess.run(['git', '-C', str(project), 'rev-parse', flag],
                                env=env, capture_output=True, text=True, timeout=10)
        if result.returncode or not result.stdout.strip():
            raise ValueError('actual Git repository cannot be verified')
        value = Path(result.stdout.strip())
        values.append(canonical(value if value.is_absolute() else project/value))
    if values != [project, gitdir, gitdir]:
        raise ValueError('actual Git root or repository identity differs')
    # Inode identity prevents a replacement repository at the same path from
    # inheriting the observation. HEAD/branch progress does not invalidate it.
    return dict(root=str(project), git_dir=str(gitdir), common_dir=str(gitdir),
                device=gitdir.stat().st_dev, inode=gitdir.stat().st_ino)


def _context(workflow):
    d = canonical(workflow)
    state = core.read(canonical(d/'state.json'))
    contract = core.read(canonical(d/'contract.json'))
    origin = core.read(canonical(d/'origin.json'))
    project = canonical(state['repo'])
    if (state.get('kind') != 'workflow' or state.get('id') != d.name
            or d.parent != project/'.codex/tasks/workflows'
            or origin.get('run_id') != d.name or origin.get('repo') != str(project)
            or core.digest(contract) != state.get('contract_hash')):
        raise ValueError('exact workflow/origin/project/contract identity mismatch')
    revision = state.get('execution_revision', 0)
    if type(revision) is not int or revision < 0:
        raise ValueError('invalid workflow execution generation')
    if (d/'events.jsonl').exists() or (d/'events.jsonl').is_symlink():
        canonical(d/'events.jsonl')
    # Validate existing permission fields; an association cannot legalize an
    # invalid contract or grant even if its state hash was also rewritten.
    import workflow as workflows
    workflows.validate(copy.deepcopy(contract))
    hub = interaction.config()
    if not hub or session_router.is_reference(hub.get('thread_id')):
        raise ValueError('a verified legacy hub is required')
    adapter = _adapter()
    adapter.workspace_origin_context(origin, hub)
    return d, state, origin, project, hub, adapter


def _authorize(origin, hub, adapter):
    actor = session_router.current_actor_locator()
    if actor not in (origin['origin_thread_id'], hub['thread_id']):
        raise ValueError('workspace association requires its verified origin or hub actor')
    # CTASK_SESSION_REF / caller role strings cannot replace native actor proof.
    adapter.check_legacy_actor(actor)
    return actor


def _proof(workflow, workspace):
    d, state, origin, project, hub, adapter = _context(workflow)
    workspace = canonical(workspace)
    native = adapter.workspace_identity(origin['origin_thread_id'])
    if native['workspace'] != str(workspace):
        raise ValueError('selected workspace differs from original header cwd')
    hub_native = adapter.workspace_identity(hub['thread_id'])
    return dict(version=VERSION, purpose='legacy-workspace-observation', workflow=str(d),
        workflow_id=d.name, project=str(project), workspace=str(workspace),
        contract_hash=state['contract_hash'], contract_sha256=sha(d/'contract.json'),
        origin_sha256=sha(d/'origin.json'), native=native, hub_native=hub_native,
        hub_sha256=sha(interaction.directory()/'hub.json'),
        execution_revision=state.get('execution_revision', 0),
        generation=recovery.generation(d, state), git=_git(project, workspace))


def _paths(workflow, create=False):
    # Caller validation rejects path aliases before selecting an observation.
    d = canonical(workflow)
    folder = interaction.directory()/'workspace-associations'
    if create:
        canonical(folder.parent)
        folder.mkdir(mode=0o700, exist_ok=True)
    if folder.exists():
        canonical(folder)
    path = folder/(core.digest(str(d))+'.json')
    if path.exists() or path.is_symlink():
        canonical(path)
    lock = path.with_suffix('.lock')
    if lock.exists() or lock.is_symlink():
        canonical(lock)
    return path, lock


def _read(path):
    if not path.exists():
        return dict(version=VERSION, records=[])
    if path.stat().st_size > MAX_LEDGER_BYTES:
        raise ValueError('workspace association ledger exceeds bound')
    ledger = core.read(path)
    if (not isinstance(ledger, dict) or set(ledger) != {'version', 'records'} or ledger['version'] != VERSION
            or not isinstance(ledger['records'], list) or not ledger['records']
            or len(ledger['records']) > MAX_RECORDS):
        raise ValueError('invalid workspace association ledger')
    for record in ledger['records']:
        if not isinstance(record, dict) or not isinstance(record.get('proof'), dict):
            raise ValueError('invalid workspace association record')
        data = {k: v for k, v in record.items() if k != 'checksum'}
        if record.get('checksum') != core.digest(data):
            raise ValueError('workspace association record changed')
        if (record.get('version') != VERSION or record.get('state') not in ('active', 'revoked')
                or not isinstance(record.get('source'), str) or not record['source'].strip()
                or record.get('registered_by') not in (record['proof']['native']['native_id'],
                                                       record['proof']['hub_native']['native_id'])):
            raise ValueError('invalid workspace association record')
    return ledger


def _seal(record):
    record['checksum'] = core.digest({k: v for k, v in record.items() if k != 'checksum'})


def validated(workflow):
    path, _ = _paths(workflow)
    ledger = _read(path)
    if not ledger['records'] or ledger['records'][-1]['state'] != 'active':
        raise ValueError('session project mismatch: no active workspace association')
    record = ledger['records'][-1]
    proof = _proof(workflow, record['proof']['workspace'])
    evidence = canonical(record['evidence'])
    if not evidence.is_file() or evidence.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError('bounded regular workspace evidence required')
    if record['proof'] != proof or sha(evidence) != record['evidence_sha256']:
        raise ValueError('workspace association is stale or evidence changed')
    if core.read(record['evidence']) != proof:
        raise ValueError('workspace evidence does not match exact current bindings')
    return record


def has_record(workflow):
    path, _ = _paths(workflow)
    return path.exists()


def inspect(workflow, workspace):
    d, state, origin, project, hub, adapter = _context(workflow)
    _authorize(origin, hub, adapter)
    proof = _proof(d, workspace)
    path, _ = _paths(d)
    ledger = _read(path)
    result = dict(expected_evidence=proof, association=None, validation='absent')
    if ledger['records']:
        result['association'] = ledger['records'][-1]
        try:
            validated(d)
            result['validation'] = 'valid'
        except (ValueError, OSError, KeyError) as exc:
            result.update(validation='invalid', reason=str(exc))
    return result


def register(workflow, workspace, evidence, source):
    if not isinstance(source, str) or not source.strip() or len(source) > 4000:
        raise ValueError('explicit bounded registration source required')
    d, state, origin, project, hub, adapter = _context(workflow)
    actor = _authorize(origin, hub, adapter)
    evidence = canonical(evidence)
    if not evidence.is_file() or evidence.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError('bounded regular workspace evidence required')
    path, lock = _paths(d, create=True)
    with recovery.locked(lock):
        proof = _proof(d, workspace)
        evidence_sha = sha(evidence)
        if core.read(evidence) != proof:
            raise ValueError('workspace evidence does not match exact current bindings')
        ledger = _read(path)
        if ledger['records'] and ledger['records'][-1]['state'] == 'active':
            prior = validated(d)
            if prior['proof'] == proof and prior['evidence'] == str(evidence):
                return prior  # Idempotent registration; no history overwrite.
            raise ValueError('revoke the existing association before replacement')
        if len(ledger['records']) >= MAX_RECORDS:
            raise ValueError('workspace association history bound reached')
        record = dict(version=VERSION, association_id='workspace-'+uuid.uuid4().hex,
            state='active', proof=proof, evidence=str(evidence), evidence_sha256=evidence_sha,
            source=source, registered_by=actor, registered_at=core.now())
        _seal(record)
        ledger['records'].append(record)
        core.save(path, ledger)
        return record


def revoke(workflow, source):
    if not isinstance(source, str) or not source.strip() or len(source) > 4000:
        raise ValueError('explicit bounded revocation source required')
    d, state, origin, project, hub, adapter = _context(workflow)
    actor = _authorize(origin, hub, adapter)
    path, lock = _paths(d, create=True)
    with recovery.locked(lock):
        ledger = _read(path)
        if not ledger['records']:
            raise ValueError('no workspace association to revoke')
        record = ledger['records'][-1]
        if actor not in (record['proof']['native']['native_id'], record['proof']['hub_native']['native_id']):
            raise ValueError('revocation actor differs from registered origin/hub')
        if record['state'] == 'active':
            record.update(state='revoked', revoked_by=actor, revoked_at=core.now(), revoke_source=source)
            _seal(record)
            core.save(path, ledger)
        return record
