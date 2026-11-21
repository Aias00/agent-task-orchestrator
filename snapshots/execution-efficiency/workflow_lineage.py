"""Observation-only, evidence-pinned links between independently completed workflows."""
import hashlib
from pathlib import Path
import subprocess

import interaction
import recovery
import session_router
import runner as core


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _evidence(d, state, contract):
    if state['status'] != 'completed':
        raise ValueError('successor has not completed')
    result = {}
    for criterion in contract['acceptance']:
        item = state.get('criteria', {}).get(criterion['id'], {})
        if item.get('status') != 'passed' or not item.get('evidence'):
            raise ValueError('successor criterion lacks passed evidence: '+criterion['id'])
        result[criterion['id']] = {}
        for name in item['evidence']:
            path = Path(name).resolve()
            if not path.is_file():
                raise ValueError('missing criterion evidence: '+str(path))
            result[criterion['id']][str(path)] = sha(path)
        if criterion['kind'] == 'manual':
            ack = d/'acknowledgements'/f"{criterion['id']}.json"
            if not ack.is_file():
                raise ValueError('manual criterion lacks acknowledgement')
            data = core.read(ack)
            if (data.get('criterion') != criterion['id'] or
                    data.get('execution_revision') != state.get('execution_revision', 0) or
                    data.get('contract_hash') != state['contract_hash'] or
                    not data.get('note') or not Path(data.get('evidence', '')).is_file() or
                    sha(data['evidence']) != data.get('checksum')):
                raise ValueError('invalid manual acknowledgement')
            result[criterion['id']][str(ack.resolve())] = sha(ack)
    return result


def _validate(predecessor, successor, proof, source, check_actor):
    old, os, oc, origin = interaction.binding(predecessor, require_open=False)
    new, ns, nc, new_origin = interaction.binding(successor, require_open=False)
    hub = interaction.config()
    if not hub:
        raise ValueError('a current verified hub is required')
    interaction.verify_target(hub['thread_id'])
    if check_actor:
        actor = session_router.current_actor_locator()
        if actor not in (origin, hub['thread_id']):
            raise ValueError('link requires the bound origin or current hub')
    if old == new or Path(os['repo']).resolve() != Path(ns['repo']).resolve() or origin != new_origin:
        raise ValueError('workflow repository or origin differs')
    repo = Path(os['repo']).resolve()
    root = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'],
                          capture_output=True, text=True, check=False)
    if (root.returncode or Path(root.stdout.strip()).resolve() != repo
            or old.parent != repo/'.codex/tasks/workflows' or new.parent != old.parent
            or os.get('id') != old.name or ns.get('id') != new.name
            or core.read(old/'origin.json').get('run_id') != old.name
            or core.read(new/'origin.json').get('run_id') != new.name):
        raise ValueError('workflow path or actual git repository identity differs')
    if Path(core.read(old/'origin.json')['repo']).resolve() != Path(os['repo']).resolve() or Path(core.read(new/'origin.json')['repo']).resolve() != Path(ns['repo']).resolve():
        raise ValueError('origin repository differs')
    if not source or not source.strip():
        raise ValueError('explicit lineage source required')
    proof = Path(proof).resolve()
    data = core.read(proof)
    if (data.get('predecessor_workflow') != str(old) or data.get('successor_workflow') != str(new)
            or data.get('predecessor_contract_sha256') != sha(old/'contract.json')
            or data.get('successor_contract_sha256') != sha(new/'contract.json')
            or data.get('permission_expanded') is not False or data.get('origin_thread') != origin
            or data.get('hub_thread') != hub['thread_id'] or not (data.get('source') or data.get('note'))):
        raise ValueError('lineage evidence does not match workflow bindings')
    # Preserve every authority-bearing field. Objective wording and stage plans may differ;
    # criteria are compared separately, including full command and manual gates.
    omit = {'objective', 'acceptance', 'stages', 'scope'}
    if {k:v for k,v in oc.items() if k not in omit} != {k:v for k,v in nc.items() if k not in omit}:
        raise ValueError('workflow authority, grants, scope or limits changed')
    if oc['scope'] != nc['scope']:
        if (not nc['scope'].startswith(oc['scope']) or
                data.get('scope_annotation') != nc['scope'][len(oc['scope']):] or
                data.get('scope_annotation_observation_only') is not True):
            raise ValueError('scope must retain the original with explicitly evidenced observation-only annotation')
    mapping = data.get('acceptance_mapping')
    if not isinstance(mapping, dict) or set(mapping) != {x['id'] for x in oc['acceptance']}:
        raise ValueError('complete acceptance mapping is required')
    new_items = {x['id']: x for x in nc['acceptance']}
    if len(set(mapping.values())) != len(mapping) or not set(mapping.values()) <= set(new_items):
        raise ValueError('acceptance mapping must be one-to-one')
    for item in oc['acceptance']:
        match = new_items[mapping[item['id']]]
        if {k:v for k,v in item.items() if k != 'id'} != {k:v for k,v in match.items() if k != 'id'}:
            raise ValueError('original acceptance criterion changed: '+item['id'])
    for other in old.parent.glob('*/workflow-lineage.json'):
        if other.parent.resolve() != old and core.read(other).get('successor') == str(new):
            raise ValueError('successor already belongs to another predecessor')
    seen = {str(old)}
    cursor = new
    while True:
        if str(cursor) in seen:
            raise ValueError('lineage cycle')
        seen.add(str(cursor))
        link = cursor/'workflow-lineage.json'
        if not link.exists():
            break
        cursor = Path(core.read(link)['successor']).resolve()
    return dict(version=1, predecessor=str(old), successor=str(new), evidence=str(proof),
                evidence_sha256=sha(proof), source=source, origin=origin, hub=hub['thread_id'],
                predecessor_contract_sha256=sha(old/'contract.json'), successor_contract_sha256=sha(new/'contract.json'),
                predecessor_contract_semantic=core.digest(oc), successor_contract_semantic=core.digest(nc),
                predecessor_origin_sha256=sha(old/'origin.json'), successor_origin_sha256=sha(new/'origin.json'),
                successor_state_sha256=sha(new/'state.json'), completion_evidence=_evidence(new, ns, nc))


def link(predecessor, successor, evidence, source):
    d = Path(predecessor).resolve()
    state = core.read(d/'state.json')
    lock = Path(state['repo'])/'.codex/tasks/.lineage.lock'
    with recovery.locked(lock):
        result = _validate(d, successor, evidence, source, True)
        path = d/'workflow-lineage.json'
        if path.exists():
            if core.read(path) != result:
                raise ValueError('existing lineage differs; no replacement or branch allowed')
        else:
            core.save(path, result)
        return result


def delivery(directory, state):
    d = Path(directory).resolve()
    import workflow_closure
    closure = workflow_closure.projection(d, state)
    if closure is not None:
        return closure
    result = dict(raw_status=state['status'], effective_status=state['status'], successor=None,
                  validation='unlinked', historical=dict(stop=(d/'STOP').exists()))
    path = d/'workflow-lineage.json'
    if not path.exists():
        return result
    try:
        recorded = core.read(path)
        current = _validate(d, recorded['successor'], recorded['evidence'], recorded['source'], False)
        if current != recorded:
            raise ValueError('lineage or pinned completion evidence changed')
        result.update(effective_status='completed', successor=recorded['successor'], validation='valid',
                      evidence=dict(path=recorded['evidence'], sha256=recorded['evidence_sha256']))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result.update(validation='invalid', error=str(exc), successor=recorded.get('successor') if 'recorded' in locals() else None)
    return result
