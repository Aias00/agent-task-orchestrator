"""Accepted-answer delivery observations. These records never confer authority.

The ledger belongs to ATO, not to either workflow. All public projections
revalidate pinned proofs; original states, questions and lineage stay intact.
"""
import copy
import hashlib
import math
import os
from pathlib import Path
import subprocess

import interaction
import recovery
import runner as core
import session_router

VERSION = 1
MAX_RECORDS = 128
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 32 * 1024 * 1024


def canonical(value):
    if not isinstance(value, (str, Path)) or not str(value) or not Path(value).is_absolute():
        raise ValueError('explicit absolute observation path required')
    path = Path(value)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink is not immutable delivery proof')
    resolved = path.resolve(strict=True)
    if str(resolved) != os.path.abspath(path):
        raise ValueError('noncanonical observation path')
    return resolved


def sha(path, limit=MAX_EVIDENCE_BYTES):
    path = canonical(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('bounded regular delivery evidence required')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    sha(path, MAX_JSON_BYTES)
    value = core.read(path)
    if not isinstance(value, dict):
        raise ValueError('structured delivery proof required')
    return value


def _ledger_path(create=False):
    folder = interaction.directory()
    canonical(folder)
    path = folder/'approved-deliveries.json'
    lock = folder/'approved-deliveries.lock'
    for item in (path, lock):
        if item.exists() or item.is_symlink():
            canonical(item)
    return path, lock


def _ledger():
    path, _ = _ledger_path()
    if not path.exists():
        return dict(version=VERSION, records=[])
    data = read(path)
    if (set(data) != {'version', 'records'} or data['version'] != VERSION
            or not isinstance(data['records'], list) or len(data['records']) > MAX_RECORDS):
        raise ValueError('invalid approved-delivery ledger')
    questions, deliveries, assignments = set(), set(), set()
    edges = []
    for row in data['records']:
        if (not isinstance(row, dict) or row.get('checksum') != core.digest(
                {k: v for k, v in row.items() if k != 'checksum'})):
            raise ValueError('approved-delivery ledger changed')
        proof = row['proof']
        question = proof['question']['id']
        delivery = proof['delivery']['workflow']
        if question in questions or delivery in deliveries:
            raise ValueError('duplicate or conflicting approved delivery')
        questions.add(question); deliveries.add(delivery)
        for criterion in proof['approved_criteria']:
            assignment = (proof['original']['workflow'], criterion)
            if assignment in assignments:
                raise ValueError('approved scope already assigned to a different delivery')
            assignments.add(assignment)
        edges.append((proof['original']['workflow'], delivery))
    _acyclic(edges)
    return data


def _acyclic(edges):
    # Include existing compatible-lineage edges without writing them or
    # interpreting them as approved-delivery authority. Mixed cycles are still
    # cycles. Only exact workflow siblings may occur in this bounded graph.
    edges = list(edges)
    pending = list({p for edge in edges for p in edge})
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        if len(seen) >= MAX_RECORDS * 4:
            raise ValueError('delivery observation graph exceeds bound')
        seen.add(name)
        d = canonical(name)
        path = d/'workflow-lineage.json'
        if path.exists() or path.is_symlink():
            successor = canonical(read(path)['successor'])
            if successor.parent != d.parent:
                raise ValueError('lineage leaves the exact workflow repository')
            edges.append((str(d), str(successor)))
            pending.append(str(successor))
    graph = {}
    for left, right in edges:
        graph.setdefault(left, []).append(right)
    def visit(node, active, done):
        if node in active:
            raise ValueError('approved-delivery cycle')
        if node in done:
            return
        active.add(node)
        for child in graph.get(node, []):
            visit(child, active, done)
        active.remove(node); done.add(node)
    done = set()
    for node in graph:
        visit(node, set(), done)


def _git(repo):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
    values = []
    for flag in ('--show-toplevel', '--absolute-git-dir', '--git-common-dir'):
        proc = subprocess.run(['git', '-C', str(repo), 'rev-parse', flag], env=env,
                              capture_output=True, text=True, timeout=10)
        if proc.returncode or not proc.stdout.strip():
            raise ValueError('actual repository identity cannot be verified')
        value = Path(proc.stdout.strip())
        values.append(canonical(value if value.is_absolute() else repo/value))
    if values[0] != repo:
        raise ValueError('workflow is not at the actual Git root')
    return dict(root=str(repo), git_dir=str(values[1]), common_dir=str(values[2]),
                identities=[[p.stat().st_dev, p.stat().st_ino] for p in values[1:]])


def _context(directory):
    import agents  # Initialize built-in, code-owned session adapters; no I/O.
    d = canonical(directory)
    for name in ('state.json', 'contract.json', 'origin.json'):
        canonical(d/name)
        read(d/name)
    d, state, contract, origin = interaction.binding(d, require_open=False)
    meta = read(d/'origin.json')
    repo = canonical(state['repo'])
    if (state.get('id') != d.name or meta.get('run_id') != d.name
            or meta.get('repo') != str(repo) or d.parent != repo/'.codex/tasks/workflows'):
        raise ValueError('exact workflow/origin/repository identity mismatch')
    revision = state.get('execution_revision', 0)
    if type(revision) is not int or revision < 0:
        raise ValueError('invalid execution generation')
    if (d/'events.jsonl').exists() or (d/'events.jsonl').is_symlink():
        canonical(d/'events.jsonl')
    import workflow
    workflow.validate(copy.deepcopy(contract))
    criteria = state.get('criteria')
    if (not isinstance(criteria, dict) or set(criteria) != {x['id'] for x in contract['acceptance']}
            or any(not isinstance(value, dict) for value in criteria.values())):
        raise ValueError('workflow criterion identity or result structure changed')
    identity = dict(workflow=str(d), contract_hash=state['contract_hash'],
        contract_sha256=sha(d/'contract.json'), origin_sha256=sha(d/'origin.json'),
        generation=recovery.generation(d, state))
    return d, state, contract, origin, identity


def _completion(d, state, contract):
    if state['status'] != 'completed' or (d/'STOP').exists() or (d/'STOP').is_symlink():
        raise ValueError('independent delivery must be completed and not stopped')
    import workflow_closure
    if workflow_closure.active_execution(state):
        raise ValueError('delivery executor is still active or unverified')
    if state.get('active_run') and workflow_closure.active_execution(read(canonical(Path(state['active_run'])/'state.json'))):
        raise ValueError('delivery stage executor is still active or unverified')
    evidence = {}
    for criterion in contract['acceptance']:
        key = criterion['id']
        item = state.get('criteria', {}).get(key, {})
        paths = item.get('evidence')
        if (item.get('status') != 'passed' or not isinstance(paths, list) or not paths
                or len(paths) > 128 or len(paths) != len(set(paths))):
            raise ValueError('delivery criterion lacks passed evidence: '+key)
        evidence[key] = {str(canonical(p)): sha(p) for p in paths}
        if criterion['kind'] == 'manual':
            ackpath = canonical(d/'acknowledgements'/(key+'.json'))
            ack = read(ackpath)
            if (ack.get('criterion') != key or ack.get('contract_hash') != state['contract_hash']
                    or ack.get('execution_revision') != state.get('execution_revision', 0)
                    or not isinstance(ack.get('note'), str) or not ack['note'].strip()
                    or sha(ack.get('evidence')) != ack.get('checksum')):
                raise ValueError('manual delivery requires its actual matching acknowledgement')
            evidence[key][str(ackpath)] = sha(ackpath)
            evidence[key][str(canonical(ack['evidence']))] = ack['checksum']
    return evidence


def _authority_delta(original, delivery):
    # Conservatively treat EVERY differing non-objective/acceptance field as an
    # authority change. No language parser decides what an answer permitted.
    ignored = {'objective', 'acceptance'}
    return {k: dict(original_present=k in original, delivery_present=k in delivery,
                    original=original.get(k), delivery=delivery.get(k))
            for k in sorted((set(original) | set(delivery)) - ignored)
            if (k in original) != (k in delivery) or original.get(k) != delivery.get(k)}


def _base(original, identifier, delivery):
    old, os_, oc, origin, old_id = _context(original)
    new, ns, nc, new_origin, new_id = _context(delivery)
    if old == new or os_['repo'] != ns['repo'] or origin != new_origin:
        raise ValueError('approved delivery requires the exact same repository and origin')
    hub = interaction.config()
    if not hub:
        raise ValueError('current verified approval hub required')
    interaction.verify_target(hub['thread_id'])
    qpath = canonical(interaction.question_path(identifier))
    q = read(qpath)
    if (q.get('id') != identifier or q.get('workflow') != str(old)
            or q.get('contract_hash') != os_['contract_hash'] or q.get('origin_thread') != origin
            or q.get('hub_thread') != hub['thread_id'] or q.get('generation') != old_id['generation']):
        raise ValueError('question/origin/hub/contract/generation mismatch')
    if (q.get('state') != 'accepted' or not q.get('accepted_at') or not q.get('answered_at')
            or not isinstance(q.get('answer'), str) or not q['answer'].strip()
            or not isinstance(q.get('answer_source'), str) or not q['answer_source'].strip()
            or not isinstance(q.get('answer_delivery'), dict) or not q['answer_delivery']):
        raise ValueError('actual sourced answer accepted by its origin required')
    times = [q.get(key) for key in ('created_at', 'answered_at', 'accepted_at')]
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in times)
            or times != sorted(times)):
        raise ValueError('actual chronological question/answer/acceptance timestamps required')
    # STOP on the original is historical. This path intentionally does not
    # validate an open execution request or clear any stop/retry barrier.
    delta = _authority_delta(oc, nc)
    result = dict(version=VERSION, purpose='approved-delivery-observation',
        original=old_id, delivery=dict(new_id, state_sha256=sha(new/'state.json')),
        question=dict(id=identifier, sha256=sha(qpath), answer=q['answer'],
                      answer_source=q['answer_source'], accepted_at=q['accepted_at']),
        origin=origin, hub=hub['thread_id'], hub_sha256=sha(interaction.directory()/'hub.json'),
        sessions=session_router.observation_bindings(read(old/'origin.json'), hub),
        repository=_git(canonical(os_['repo'])), authority_delta=delta,
        permission_expanded=bool(delta), completion_evidence=_completion(new, ns, nc),
        approved_criteria={}, deferred_criteria=[c['id'] for c in oc['acceptance']])
    return result, os_, oc, nc


def _authorize(original, origin, hub, actor_proof=None, reference=None):
    who = reference or session_router.current_actor_locator()
    if who not in (origin, hub):
        raise ValueError('observation requires its verified origin or approval hub')
    interaction.question_actor(who, original, 'hub' if who == hub else 'origin',
        'interaction.present' if who == hub else 'workflow.report', actor_proof, require_open=False)
    return who


def inspect(original, identifier, delivery, *, actor_proof=None, reference=None):
    proof, _, oc, nc = _base(original, identifier, delivery)
    _authorize(original, proof['origin'], proof['hub'], actor_proof, reference)
    return dict(expected_evidence=proof, original_criteria=oc['acceptance'],
                delivery_criteria=nc['acceptance'], observation_only=True, execution_authority=False)


def _validate(proof):
    if type(proof.get('version')) is not int or type(proof.get('permission_expanded')) is not bool:
        raise ValueError('typed approved-delivery version and authority difference required')
    expected, state, oc, nc = _base(proof['original']['workflow'], proof['question']['id'],
                                     proof['delivery']['workflow'])
    mapped = proof.get('approved_criteria')
    deferred = proof.get('deferred_criteria')
    old_ids = {x['id'] for x in oc['acceptance']}
    new_ids = {x['id'] for x in nc['acceptance']}
    if (not isinstance(mapped, dict) or not mapped or not set(mapped) <= old_ids
            or not isinstance(deferred, list) or len(deferred) != len(set(deferred))
            or set(deferred) != old_ids - set(mapped)):
        raise ValueError('explicit approved mapping and complete original deferred criteria required')
    for values in mapped.values():
        if (not isinstance(values, list) or not values or any(not isinstance(x, str) for x in values)
                or len(values) != len(set(values)) or not set(values) <= new_ids):
            raise ValueError('approved scope must name actual completed delivery criteria')
    expected.update(approved_criteria=mapped, deferred_criteria=deferred)
    if proof != expected:
        raise ValueError('approved-delivery proof changed or does not match exact current bindings')
    return state, oc


def register(original, identifier, delivery, evidence, source, *, actor_proof=None, reference=None):
    if not isinstance(source, str) or not source.strip() or len(source) > 4000:
        raise ValueError('explicit bounded observation source required')
    evidence = canonical(evidence)
    proof = read(evidence)
    if (proof.get('original', {}).get('workflow') != str(canonical(original))
            or proof.get('delivery', {}).get('workflow') != str(canonical(delivery))
            or proof.get('question', {}).get('id') != identifier):
        raise ValueError('evidence does not bind requested question and workflows')
    _validate(proof)
    who = _authorize(original, proof['origin'], proof['hub'], actor_proof, reference)
    path, lock = _ledger_path(create=True)
    with recovery.locked(lock):
        _validate(proof)
        data = _ledger()
        for row in data['records']:
            prior = row['proof']
            if prior['question']['id'] == identifier or prior['delivery']['workflow'] == str(canonical(delivery)):
                _current(row)
                if prior == proof and row['evidence'] == str(evidence) and row['evidence_sha256'] == sha(evidence):
                    return row
                raise ValueError('approved delivery already assigned; no replacement or conflicting branch')
            if (prior['original']['workflow'] == proof['original']['workflow']
                    and set(prior['approved_criteria']) & set(proof['approved_criteria'])):
                raise ValueError('approved scope already assigned to another delivery')
        edges = [(r['proof']['original']['workflow'], r['proof']['delivery']['workflow']) for r in data['records']]
        _acyclic(edges + [(proof['original']['workflow'], proof['delivery']['workflow'])])
        if len(data['records']) >= MAX_RECORDS:
            raise ValueError('approved-delivery observation bound reached')
        row = dict(version=VERSION, id='approved-'+core.digest([str(original), identifier, str(delivery)])[:24],
                   proof=proof, evidence=str(evidence), evidence_sha256=sha(evidence), source=source,
                   registered_by=who, registered_at=core.now(), observation_only=True, execution_authority=False)
        row['checksum'] = core.digest(row)
        data['records'].append(row)
        # Bound writes as well as reads; never leave an unreadable ledger.
        import json
        if len(json.dumps(data).encode()) > MAX_JSON_BYTES:
            raise ValueError('approved-delivery ledger exceeds bound')
        core.save(path, data)
        return row


def _current(row):
    if sha(row['evidence'], MAX_JSON_BYTES) != row['evidence_sha256'] or read(row['evidence']) != row['proof']:
        raise ValueError('approved-delivery evidence changed')
    return _validate(row['proof'])


def _projection(row):
    proof = row['proof']
    result = dict(id=row['id'], question=proof['question']['id'],
        original_workflow=proof['original']['workflow'], delivery_workflow=proof['delivery']['workflow'],
        observation_only=True, execution_authority=False, relation_completes_original=False)
    try:
        state, contract = _current(row)
        deferred = proof['deferred_criteria']
        remaining = [c['id'] for c in contract['acceptance'] if state.get('criteria', {}).get(c['id'], {}).get('status') != 'passed']
        result.update(validation='valid', approved_scope_status='delivered',
            original_workflow_complete=state['status'] == 'completed',
            approved_criteria=proof['approved_criteria'], deferred_criteria=deferred,
            remaining_deferred_criteria=[x for x in deferred if x in remaining],
            original_remaining_criteria=remaining, original_raw_status=state['status'],
            original_criteria={c['id']: dict(definition=c, result=state.get('criteria', {}).get(c['id'], {})) for c in contract['acceptance']},
            authority_delta=proof['authority_delta'], permission_expanded=proof['permission_expanded'],
            completed_criteria=proof['completion_evidence'],
            evidence=dict(path=row['evidence'], sha256=row['evidence_sha256']),
            historical=dict(stop=(Path(proof['original']['workflow'])/'STOP').exists()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(validation='invalid', approved_scope_status='unverified', error=str(exc))
    return result


def projection(original):
    """Read-only query. An invalid proof never suppresses unresolved work."""
    result = dict(validation='unlinked', relations=[], observation_only=True, execution_authority=False)
    try:
        records = _ledger()['records']
        rows = [r for r in records if r['proof']['original']['workflow'] == str(Path(original).absolute())]
        result['relations'] = [_projection(r) for r in rows]
        if rows:
            result['validation'] = 'valid' if all(r['validation'] == 'valid' for r in result['relations']) else 'invalid'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(validation='invalid', error=str(exc))
    return result


def for_question(q):
    result = projection(q['workflow'])
    matches = [r for r in result['relations'] if r['question'] == q['id']]
    if matches:
        return matches[0]
    if result['validation'] == 'invalid':
        return dict(validation='invalid', error=result.get('error', 'approved-delivery proof invalid'))
    return None


def diagnostic_scope(original, state):
    result = projection(original)
    relations = result['relations'] if result['validation'] == 'valid' else []
    approved = {key for row in relations for key in row['approved_criteria']}
    criteria = state.get('criteria', {})
    if not isinstance(criteria, dict):
        result.update(validation='invalid', error='original criterion results are malformed', diagnostic_criteria=[])
        return result
    result['diagnostic_criteria'] = [key for key, value in criteria.items()
        if (not isinstance(value, dict) or value.get('status') != 'passed') and key not in approved]
    return result
