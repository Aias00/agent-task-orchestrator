"""Observation-only identity for once-per-delivery completion notifications.

No title, repository, command or artifact digest alone establishes equivalence.
Compatible lineage is revalidated, including a bijective full acceptance map.
The separate receipt route requires an explicit shared operation receipt in
both completed results, equal full verification payloads and equal authority.
"""
import copy
import itertools
import json
from pathlib import Path

import approved_delivery as evidence
import interaction
import recovery
import runner as core
import session_router

LIMIT = 128


def _context(directory):
    d, state, contract, origin, identity = evidence._context(directory)
    return d, state, contract, origin, identity


def _pair(left, right):
    a, b = _context(left), _context(right)
    hub = interaction.config()
    if not hub:
        raise ValueError('verified completion hub required')
    interaction.verify_target(hub['thread_id'])
    if a[0] == b[0] or a[1]['repo'] != b[1]['repo'] or a[3] != b[3]:
        raise ValueError('same-delivery requires distinct workflows with the exact repository and origin')
    if evidence._authority_delta(a[2], b[2]):
        raise ValueError('same-delivery authority differs')
    repository = evidence._git(evidence.canonical(a[1]['repo']))
    return a, b, hub, repository


def _payload(context, receipt=None):
    d, state, contract, _, _ = context
    complete = evidence._completion(d, state, contract)
    payload = []
    for criterion in contract['acceptance']:
        key = criterion['id']
        paths = state['criteria'][key]['evidence']
        # Manual acknowledgements are verified independently, never inferred
        # from matching output. Their contract-specific bytes need not match.
        ack = d/'acknowledgements'/(key+'.json')
        hashes = []
        for path in paths:
            p = evidence.canonical(path)
            if receipt is not None and p == receipt:
                continue
            if criterion['kind'] == 'manual' and p == ack:
                hashes.append(evidence.read(ack)['checksum'])
            else:
                hashes.append(evidence.sha(p))
        if not hashes:
            raise ValueError('operation receipt alone is not complete acceptance evidence')
        payload.append(dict(criterion={k: v for k, v in criterion.items() if k != 'id'},
                            evidence_sha256=sorted(hashes)))
    if len({core.digest(item) for item in payload}) != len(payload):
        raise ValueError('ambiguous complete acceptance mapping')
    return sorted(payload, key=core.digest), complete


def _operation(left, right, operation_id, receipt=None):
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 240:
        raise ValueError('explicit bounded physical operation ID required')
    a, b, hub, repository = _pair(left, right)
    ap, _ = _payload(a, receipt)
    bp, _ = _payload(b, receipt)
    if ap != bp:
        raise ValueError('complete acceptance or actual verification payload differs')
    authority = {k: v for k, v in a[2].items() if k not in ('objective', 'acceptance')}
    return dict(version=1, purpose='same-physical-delivery-operation', operation_id=operation_id,
                workflows=sorted([a[4], b[4]], key=lambda x: x['workflow']),
                origin=a[3], hub=hub['thread_id'], repository=repository,
                authority_sha256=core.digest(authority), acceptance=ap)


def operation_template(left, right, operation_id, *, actor_proof=None, reference=None):
    result = _operation(left, right, operation_id)
    evidence._authorize(left, result['origin'], result['hub'], actor_proof, reference)
    return result


def _proof(left, right, receipt):
    receipt = evidence.canonical(receipt)
    actual = evidence.read(receipt)
    operation = _operation(left, right, actual.get('operation_id'), receipt)
    if actual != operation:
        raise ValueError('shared operation receipt does not match exact complete delivery and authority')
    a, b, hub, _ = _pair(left, right)
    snapshots = []
    for context in (a, b):
        d, state, contract, _, identity = context
        if str(receipt) not in {p for c in state['criteria'].values() for p in c.get('evidence', [])}:
            raise ValueError('both completed results must contain the exact shared operation receipt')
        snapshots.append(dict(identity, state_sha256=evidence.sha(d/'state.json'),
                              completion_evidence=evidence._completion(d, state, contract)))
    return dict(version=1, purpose='completion-delivery-observation',
                workflows=sorted(snapshots, key=lambda x: x['workflow']),
                receipt=str(receipt), receipt_sha256=evidence.sha(receipt),
                operation=operation, hub_sha256=evidence.sha(interaction.directory()/'hub.json'),
                sessions=session_router.observation_bindings(evidence.read(a[0]/'origin.json'), hub))


def inspect(left, right, receipt, *, actor_proof=None, reference=None):
    proof = _proof(left, right, receipt)
    evidence._authorize(left, proof['operation']['origin'], proof['operation']['hub'], actor_proof, reference)
    return dict(expected_evidence=proof, observation_only=True, execution_authority=False)


def _path():
    folder = evidence.canonical(interaction.directory())
    path, lock = folder/'completion-deliveries.json', folder/'completion-deliveries.lock'
    for item in (path, lock):
        if item.exists() or item.is_symlink():
            evidence.canonical(item)
    return path, lock


def _ledger():
    path, _ = _path()
    data = evidence.read(path) if path.exists() else dict(version=1, records=[])
    if (set(data) != {'version', 'records'} or data['version'] != 1
            or not isinstance(data['records'], list) or len(data['records']) > LIMIT):
        raise ValueError('invalid completion identity ledger')
    assigned = set()
    for row in data['records']:
        if (not isinstance(row, dict) or row.get('checksum') != core.digest(
                {k: v for k, v in row.items() if k != 'checksum'})):
            raise ValueError('completion identity ledger changed')
        names = [x['workflow'] for x in row['proof']['workflows']]
        if len(names) != 2 or len(set(names)) != 2 or assigned.intersection(names):
            raise ValueError('ambiguous or conflicting same-delivery assignment')
        assigned.update(names)
    return data


def register(left, right, receipt, proof_path, source, *, actor_proof=None, reference=None):
    if not isinstance(source, str) or not source.strip() or len(source) > 4096:
        raise ValueError('explicit bounded same-delivery source required')
    proof_path = evidence.canonical(proof_path)
    expected = inspect(left, right, receipt, actor_proof=actor_proof, reference=reference)['expected_evidence']
    if evidence.read(proof_path) != expected:
        raise ValueError('same-delivery proof changed')
    who = evidence._authorize(left, expected['operation']['origin'], expected['operation']['hub'], actor_proof, reference)
    path, lock = _path()
    with recovery.locked(lock):
        if _proof(left, right, receipt) != expected or evidence.read(proof_path) != expected:
            raise ValueError('same-delivery evidence changed while registering')
        data = _ledger()
        names = {x['workflow'] for x in expected['workflows']}
        for row in data['records']:
            if names.intersection(x['workflow'] for x in row['proof']['workflows']):
                if (row['proof'] == expected and row['evidence'] == str(proof_path)
                        and row['evidence_sha256'] == evidence.sha(proof_path)):
                    return copy.deepcopy(row)
                raise ValueError('existing same-delivery assignment cannot be replaced or branched')
        if len(data['records']) >= LIMIT:
            raise ValueError('completion identity ledger full')
        row = dict(version=1, proof=expected, evidence=str(proof_path), evidence_sha256=evidence.sha(proof_path),
                   source=source, registered_by=who, registered_at=core.now(),
                   observation_only=True, execution_authority=False)
        row['checksum'] = core.digest(row)
        data['records'].append(row)
        if len(json.dumps(data).encode()) > evidence.MAX_JSON_BYTES:
            raise ValueError('completion identity ledger exceeds bound')
        core.save(path, data)
    return copy.deepcopy(row)


def identity(directory):
    """Revalidate the bounded connected component, including incoming lineage.

    Invalid or ambiguous connected proof blocks new notices; it never becomes
    permission to send a second notice. No workflow or old notice is rewritten.
    """
    root = evidence.canonical(directory)
    try:
        _context(root)
        records = _ledger()['records']
        edges = []
        for row in records:
            names = [x['workflow'] for x in row['proof']['workflows']]
            edges.append((names[0], names[1], 'receipt', row))
        # Only this exact workflow repository is considered, with bounded reads.
        paths = list(itertools.islice(root.parent.glob('*/workflow-lineage.json'), LIMIT+1))
        if len(paths) > LIMIT:
            raise ValueError('completion lineage exceeds bound')
        for path in paths:
            # An unreadable unrelated edge is not used as equivalence evidence.
            try:
                recorded = evidence.read(path)
                target = recorded['successor']
            except (OSError, ValueError, KeyError, TypeError):
                if path.parent == root:
                    raise ValueError('invalid completion lineage')
                continue
            edges.append((str(path.parent), target, 'lineage', recorded))
        members, selected = {str(root)}, []
        while True:
            added = False
            for edge in edges:
                a, b, kind, row = edge
                if edge in selected or not members.intersection((a, b)):
                    continue
                if len(selected) >= LIMIT:
                    raise ValueError('completion identity graph exceeds bound')
                if kind == 'receipt':
                    if (evidence.sha(row['evidence']) != row['evidence_sha256']
                            or evidence.read(row['evidence']) != row['proof']
                            or _proof(a, b, row['proof']['receipt']) != row['proof']):
                        raise ValueError('same-delivery pinned evidence changed')
                else:
                    import workflow_lineage
                    evidence._git(evidence.canonical(_context(a)[1]['repo']))
                    current = workflow_lineage._validate(a, b, row['evidence'], row['source'], False)
                    if current != row:
                        raise ValueError('compatible completion lineage changed')
                    proof = evidence.read(row['evidence'])
                    _, _, ac, _, _ = _context(a)
                    _, _, bc, _, _ = _context(b)
                    # A successor with additional acceptance is distinct work.
                    if len(ac['acceptance']) != len(bc['acceptance']):
                        continue
                    if set(proof['acceptance_mapping'].values()) != {x['id'] for x in bc['acceptance']}:
                        raise ValueError('ambiguous full completion mapping')
                selected.append(edge)
                members.update((a, b)); added = True
            if not added:
                break
        contexts = [_context(name) for name in sorted(members)]
        bindings = [c[4] for c in contexts]
        return dict(validation='valid', members=bindings,
                    identity=core.digest(bindings), observation_only=True, execution_authority=False,
                    relations=[dict(kind=e[2], left=e[0], right=e[1]) for e in selected])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return dict(validation='invalid', error=str(exc), observation_only=True, execution_authority=False)
