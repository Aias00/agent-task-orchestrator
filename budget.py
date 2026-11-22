"""Task-specific adaptive execution envelopes. No model calls or business approvals.

The frozen contract remains unchanged. Policy, charges and automatic grants are
bound to its hash in a separate, locked, append-only-in-spirit audit ledger.
"""
import contextlib
import copy
import fcntl
import hashlib
import math
from pathlib import Path
import signal

import runner as core


def time_policy():
    """Machine-local user policy; absence preserves legacy installations exactly."""
    import interaction
    path = interaction.directory()/'time-policy.json'
    if not path.exists():
        return dict(mode='enforced', source='Legacy installation; no advisory policy recorded', revision=None)
    value = core.read(path)
    prior = 'ctask-time-policy-v1'
    for row in value['history']:
        body = {k: v for k, v in row.items() if k != 'checksum'}
        if row.get('previous') != prior or core.digest(body) != row.get('checksum'):
            raise ValueError('time policy history checksum mismatch')
        prior = row['checksum']
    if not value['history'] or value['history'][-1]['snapshot'] != {k: v for k, v in value.items() if k != 'history'}:
        raise ValueError('time policy projection does not match its history')
    if value['mode'] not in ('advisory', 'enforced'):
        raise ValueError('unknown execution time policy')
    return dict(mode=value['mode'], source=value['source'], revision=prior)


def set_time_policy(mode, source, request):
    """Change elapsed-time enforcement only. Never edit/start/unstop a task."""
    import interaction
    import recovery
    if mode not in ('advisory', 'enforced') or not source.strip() or not request.strip():
        raise ValueError('time policy requires mode, actual user source and stable request')
    hub = interaction.config()
    if hub:
        interaction.actor(hub['thread_id'])
    path = interaction.directory()/'time-policy.json'
    with recovery.locked(path.with_suffix('.lock')):
        time_policy()  # Verify before appending; a broken projection is never repaired silently.
        old = core.read(path) if path.exists() else dict(history=[])
        payload = dict(mode=mode, source=source, request=request)
        for row in old['history']:
            if row['details']['request'] == request:
                if row['details'] != payload:
                    raise ValueError('time policy request reused with different inputs')
                return time_policy()
        old.update(mode=mode, source=source, request=request, updated_at=core.now())
        row = dict(previous=old['history'][-1]['checksum'] if old['history'] else 'ctask-time-policy-v1',
                   details=payload, snapshot=copy.deepcopy({k: v for k, v in old.items() if k != 'history'}))
        row['checksum'] = core.digest(row)
        old['history'].append(row)
        core.save(path, old)
    return time_policy()


def time_enforced():
    return time_policy()['mode'] == 'enforced'


def deadline(directory, state=None, *, started=None):
    info = summary(directory, state)
    return (core.now() if started is None else started) + info['remaining_seconds'] if info['enforcement'] == 'enforced' else float('inf')


def number(value, label, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < (0 if not positive else 0.000001):
        raise ValueError(label + ' must be finite and ' + ('positive' if positive else 'nonnegative'))
    return value


def estimate(value):
    """A remaining-work forecast, not a universal extra duration or a spend reset."""
    if not isinstance(value, dict) or set(value) - {'items', 'risk_percent', 'max_extensions'}:
        raise ValueError('estimate requires items, optional risk_percent/max_extensions')
    items = value.get('items')
    if not isinstance(items, list) or not items:
        raise ValueError('estimate requires a nonempty remaining-work breakdown')
    seen, expected, upper = set(), 0, 0
    for item in items:
        if not isinstance(item, dict) or set(item) - {'id', 'description', 'seconds', 'upper_seconds', 'basis', 'evidence'}:
            raise ValueError('unknown estimate item fields')
        for key in ('id', 'description', 'basis'):
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError('each estimate item needs ' + key)
        if item['id'] in seen:
            raise ValueError('duplicate estimate item')
        seen.add(item['id'])
        low = number(item.get('seconds'), 'seconds')
        high = number(item.get('upper_seconds'), 'upper_seconds')
        if high < low:
            raise ValueError('upper_seconds cannot be below seconds')
        evidence = item.get('evidence')
        if not isinstance(evidence, list) or not evidence or not all(isinstance(p, str) and Path(p).is_absolute() for p in evidence):
            raise ValueError('estimate item requires absolute evidence paths')
        expected += low
        upper += high
    risk = number(value.get('risk_percent', 25), 'risk_percent')
    if risk > 100:
        raise ValueError('risk_percent must be at most 100; revise the breakdown instead')
    extensions = value.get('max_extensions', max(1, len(items)))
    if type(extensions) is not int or not 1 <= extensions <= 100:
        raise ValueError('max_extensions must be an integer in 1..100')
    if not upper:
        raise ValueError('estimate needs remaining active work; external/human waiting is not active work')
    return dict(expected_seconds=math.ceil(expected), upper_seconds=math.ceil(upper * (1 + risk / 100)),
                max_extensions=extensions, largest_item_seconds=math.ceil(max(i['upper_seconds'] for i in items)))


def validate(policy):
    if policy is None:
        return
    if not isinstance(policy, dict) or set(policy) != {'source', 'estimate'}:
        raise ValueError('budget requires exactly source and estimate')
    if not isinstance(policy['source'], str) or not policy['source'].strip():
        raise ValueError('budget requires an actual authorization source')
    estimate(policy['estimate'])


def binding(directory):
    d = Path(directory).resolve()
    state = core.read(d/'state.json')
    path = d/('contract.json' if state.get('kind') == 'workflow' else 'plan.json')
    plan = core.read(path)
    fingerprint = state.get('contract_hash', state.get('plan_hash'))
    if core.digest(plan) != fingerprint:
        raise ValueError('budget binding changed; frozen contract/plan must be preserved')
    return d, state, plan, fingerprint


@contextlib.contextmanager
def locked(directory):
    with (Path(directory)/'budget.lock').open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load(directory, fingerprint):
    p = Path(directory)/'budget.json'
    if not p.exists():
        return None
    value = core.read(p)
    if value.get('contract_hash') != fingerprint:
        raise ValueError('budget ledger belongs to a different frozen contract')
    # Each entry commits to all previous entries. Never silently repair a broken ledger.
    prior = fingerprint
    for row in value['history']:
        body = {k: v for k, v in row.items() if k != 'checksum'}
        if row.get('previous') != prior or core.digest(body) != row.get('checksum'):
            raise ValueError('budget history checksum mismatch')
        prior = row['checksum']
    committed = value['history'][-1]['snapshot'] if value['history'] else None
    if committed != {k: v for k, v in value.items() if k != 'history'}:
        raise ValueError('budget projection does not match its audit history')
    return value


def append(directory, ledger, kind, **details):
    row = dict(at=core.now(), kind=kind, previous=ledger['history'][-1]['checksum'] if ledger['history'] else ledger['contract_hash'],
               details=details, snapshot=copy.deepcopy({k: v for k, v in ledger.items() if k != 'history'}))
    row['checksum'] = core.digest(row)
    ledger['history'].append(row)
    core.save(Path(directory)/'budget.json', ledger)


def evidence_checksums(forecast):
    result = {}
    for item in forecast['items']:
        for name in item['evidence']:
            p = Path(name)
            if not p.is_file():
                raise ValueError('estimate evidence is missing: ' + name)
            result[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def configure(directory, policy, *, request, hard_limit_seconds=None, accepted_answer=None):
    """Explicit adoption may extend a legacy task; never edits or starts that task."""
    validate(policy)
    if not isinstance(request, str) or not request.strip():
        raise ValueError('budget configuration requires a stable request id')
    d, state, plan, fingerprint = binding(directory)
    if hard_limit_seconds is not None and (type(hard_limit_seconds) is not int or hard_limit_seconds <= 0):
        raise ValueError('hard limit must be a positive integer')
    if accepted_answer:
        import interaction
        interaction.accepted_budget_stop(accepted_answer, d)
        if hard_limit_seconds is None:
            raise ValueError('budget-stop adoption requires the explicit approved hard limit')
    elif (d/'STOP').exists() or state['status'] in ('stopped', 'completed'):
        raise ValueError('cannot configure a stopped or completed task')
    # Use the same repository lease as workers. Configuration does not race an executor.
    with (Path(state['repo'])/'.codex/tasks/.worker.lock').open('a+') as repo_lock:
        fcntl.flock(repo_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        core.ensure_resumable(d)
        d, state, plan, fingerprint = binding(d)
        if accepted_answer:
            interaction.accepted_budget_stop(accepted_answer, d)
        elif (d/'STOP').exists() or state['status'] in ('stopped', 'completed'):
            raise ValueError('cannot configure a stopped or completed task')
        with locked(d):
            old = load(d, fingerprint)
            request_hash = core.digest(policy if hard_limit_seconds is None and accepted_answer is None else
                dict(policy=policy, hard_limit_seconds=hard_limit_seconds, accepted_answer=accepted_answer))
            if old:
                matches = [r for r in old['history'] if r['kind'] == 'policy' and r['details']['request'] == request]
                if matches:
                    if matches[0]['details']['request_hash'] != request_hash:
                        raise ValueError('request id already used with a different policy')
                    return old
            data = estimate(policy['estimate'])
            proofs = evidence_checksums(policy['estimate'])
            external = old['external_seconds'] if old else 0
            spent = state.get('elapsed_seconds', 0) + external
            # This ceiling is computed for this remaining task. No global fixed-hour reserve.
            soft = math.ceil(max(plan['limits']['runtime_seconds'], spent + data['expected_seconds'], old['soft_seconds'] if old else 0))
            hard = math.ceil(max(soft, spent + data['upper_seconds'], old['hard_seconds'] if old else 0))
            if hard_limit_seconds is not None:
                if hard_limit_seconds < max(plan['limits']['runtime_seconds'], old['hard_seconds'] if old else 0) or hard_limit_seconds <= spent:
                    raise ValueError('explicit hard limit cannot erase old grants or leave no execution capacity')
                hard = hard_limit_seconds
                soft = min(soft, hard)
            ledger = old or dict(contract_hash=fingerprint, external_seconds=0, history=[])
            ledger.update(policy=copy.deepcopy(policy), evidence=proofs, soft_seconds=soft, hard_seconds=hard,
                          max_extensions=data['max_extensions'], extensions=0,
                          chunk_seconds=max(1, data['largest_item_seconds']),
                          review_seconds=max(1, math.ceil(min(data['largest_item_seconds'], data['expected_seconds'] * .15))),
                          progress=progress_signature(d), last_reason='policy recorded')
            append(d, ledger, 'policy', source=policy['source'], request=request, request_hash=request_hash,
                   spent_seconds=spent, forecast=data, accepted_answer=accepted_answer,
                   explicit_hard_limit=hard_limit_seconds)
            return ledger


def check_approved_resume(directory, identifier):
    import interaction
    q = interaction.accepted_budget_stop(identifier, directory)
    d, state, _, fingerprint = binding(directory)
    ledger = load(d, fingerprint)
    if not ledger or not any(r['kind']=='policy' and r['details'].get('accepted_answer')==identifier
                             for r in ledger['history']):
        raise ValueError('register the approved budget on this task before resuming')
    if time_enforced() and state.get('elapsed_seconds', 0) + ledger['external_seconds'] >= ledger['soft_seconds']:
        raise ValueError('approved task allocation is still exhausted')
    return q


def reforecast(directory, forecast, source, request):
    """Origin may revise the soft allocation inside the already sourced hard cap."""
    if not source.strip() or not request.strip():
        raise ValueError('reforecast requires an actual source and stable request id')
    data = estimate(forecast)
    proofs = evidence_checksums(forecast)
    d, state, _, fingerprint = binding(directory)
    if (d/'STOP').exists() or state['status'] in ('stopped', 'completed'):
        raise ValueError('cannot reforecast a stopped or completed task')
    with (Path(state['repo'])/'.codex/tasks/.worker.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        core.ensure_resumable(d)
        d, state, _, fingerprint = binding(d)
        with locked(d):
            ledger = load(d, fingerprint)
            if not ledger or not ledger.get('policy'):
                raise ValueError('reforecast requires a previously authorized adaptive envelope')
            payload = dict(request=request, source=source, estimate=forecast, evidence=proofs)
            prior = [r for r in ledger['history'] if r['kind']=='reforecast' and r['details']['request']==request]
            if prior:
                if prior[0]['details'] != payload:
                    raise ValueError('reforecast request already recorded with different inputs')
                return ledger
            spent = state.get('elapsed_seconds', 0) + ledger['external_seconds']
            if time_enforced() and (spent >= ledger['hard_seconds'] or spent+data['expected_seconds'] > ledger['hard_seconds']):
                raise ValueError('remaining work exceeds the authorized hard ceiling; an explicit new envelope is required')
            ledger['soft_seconds'] = max(ledger['soft_seconds'], math.ceil(spent+data['expected_seconds']))
            if not time_enforced():
                ledger['hard_seconds'] = max(ledger['hard_seconds'], math.ceil(spent+data['upper_seconds']))
            ledger['last_reason'] = ('Origin reforecast inside the existing hard ceiling' if time_enforced()
                                     else 'Advisory estimate updated; no execution ceiling or grant')
            append(d, ledger, 'reforecast', **payload)
            return ledger


def review(directory):
    if not time_enforced():
        return None  # Advisory estimates are observed, never a renewal/approval gate.
    d, state, _, fingerprint = binding(directory)
    if (d/'STOP').exists() or state['status'] in ('completed', 'stopped', 'needs_input', 'waiting'):
        return None
    ledger = load(d, fingerprint)
    if not ledger or not ledger.get('policy'):
        return None
    used = state.get('elapsed_seconds', 0) + ledger['external_seconds']
    if ledger['soft_seconds']-used > ledger['review_seconds']:
        return None
    return dict(key=ledger['history'][-1]['checksum'], source=ledger['policy']['source'],
                used_seconds=used, soft_seconds=ledger['soft_seconds'], hard_seconds=ledger['hard_seconds'])


def initialize(directory, plan):
    if plan.get('budget'):
        configure(directory, plan['budget'], request='frozen-contract-budget')


def progress_signature(directory):
    """Only independent successful checks are progress; output or model claims are not."""
    d = Path(directory)
    values, commands = set(), {}
    paths = [d/'events.jsonl']
    state = core.read(d/'state.json')
    if state.get('kind') == 'workflow':
        paths += [Path(s['run_dir'])/'events.jsonl' for s in state.get('stages', [])]
    for p in paths:
        if not p.exists():
            continue
        with p.open() as f:
            for line in f:
                try:
                    row = __import__('json').loads(line)
                except ValueError:
                    continue
                if row.get('event') == 'command_started' and '-check-' in row.get('evidence', ''):
                    commands[row['evidence']] = core.digest(row['argv'])
                if row.get('event') == 'command_finished' and row.get('code') == 0:
                    identity = commands.get(row.get('evidence'))
                    if identity:
                        values.add(identity)
    return core.digest(sorted(values))


def summary(directory, state=None):
    d, stored, plan, fingerprint = binding(directory)
    s = state if state is not None else stored
    ledger = load(d, fingerprint)
    used = number(s.get('elapsed_seconds', 0), 'elapsed seconds') + (number(ledger['external_seconds'], 'external seconds') if ledger else 0)
    soft = ledger['soft_seconds'] if ledger else plan['limits']['runtime_seconds']
    hard = ledger['hard_seconds'] if ledger else soft
    policy = time_policy()
    enforced = policy['mode'] == 'enforced'
    envelope = 'adaptive' if ledger and ledger.get('policy') else 'fixed'
    return dict(mode=envelope if enforced else 'advisory', envelope_mode=envelope,
                enforcement=policy['mode'], enforcement_source=policy['source'], policy_revision=policy['revision'],
                used_seconds=used, soft_seconds=soft, hard_seconds=hard,
                remaining_seconds=max(0, soft-used) if enforced else None,
                forecast_remaining_seconds=max(0, soft-used), forecast_exceeded=used >= soft,
                reserve_seconds=max(0, hard-soft),
                extensions=ledger['extensions'] if ledger else 0,
                external_seconds=ledger['external_seconds'] if ledger else 0,
                last_reason=ledger['last_reason'] if ledger else 'Legacy fixed limit; no implicit budget authorization',
                exhausted=enforced and used >= soft, history_file=str(d/'budget.json') if ledger else None)


def observation(directory, state, current, owner_alive):
    """Read-only watchdog projection of the existing budget, never another ledger."""
    info = summary(directory, state)
    used = number(info['used_seconds'], 'observed used seconds')
    hard = number(info['hard_seconds'], 'hard seconds', positive=True)
    active = (state.get('status') in ('running', 'planning', 'verifying') and owner_alive
              and not state.get('accounting_paused'))
    watermark = state.get('updated_at')
    if active and watermark is not None:
        used += max(0, current-number(watermark, 'accounting watermark'))
    hard_exhausted = used >= hard or (state.get('status')=='limit' and info['mode']=='fixed')
    eligible = state.get('status') not in ('completed', 'stopped', 'paused')
    level = (('exhausted' if info['enforcement']=='enforced' else 'overrun') if hard_exhausted else 'critical' if active and used >= .9*hard else
             'warning' if active and used >= .8*hard else None) if eligible else None
    info.update(observed_used_seconds=used, level=level,
                hard_remaining_seconds=max(0, hard-used) if info['enforcement']=='enforced' else None,
                blocks_execution=info['enforcement']=='enforced' and hard_exhausted,
                soft_exhausted=used >= info['soft_seconds'],
                external_origin_work='unknown unless captured by budget-run or evidence-backed budget-charge',
                accounting_scope='existing ledger plus live active heartbeat; external work is not inferred',
                no_budget_or_permission_change=True)
    return info


def poll(owner):
    """Deterministic reserve release before the soft boundary; no calls to a model."""
    current = core.now()
    if not time_enforced():
        owner.deadline = float('inf')
        return
    # Scheduling/initialization may place the next periodic poll beyond the
    # soft deadline. Recheck real progress at that boundary before guard halts.
    if (getattr(owner, '_accounted_at', None) is None or
            current < min(getattr(owner, '_next_budget_poll', 0), owner.deadline)):
        return
    owner._next_budget_poll = current + 1
    fingerprint = owner.state.get('contract_hash', owner.state.get('plan_hash'))
    if not (owner.dir/'budget.json').exists():
        return
    with locked(owner.dir):
        ledger = load(owner.dir, fingerprint)
        spent = owner.state.get('elapsed_seconds', 0) + max(0, current-owner._accounted_at) + ledger['external_seconds']
        remaining = ledger['soft_seconds'] - spent
        if remaining <= ledger['review_seconds'] and ledger['soft_seconds'] < ledger['hard_seconds']:
            signature = progress_signature(owner.dir)
            if signature != ledger['progress'] and ledger['extensions'] < ledger['max_extensions']:
                amount = min(ledger['chunk_seconds'], ledger['hard_seconds']-ledger['soft_seconds'])
                ledger.update(soft_seconds=ledger['soft_seconds']+amount, extensions=ledger['extensions']+1,
                              progress=signature, last_reason='New independent passing checks; reserve released')
                append(owner.dir, ledger, 'auto_extension', seconds=amount, spent_seconds=spent)
                owner.event('budget_extended', seconds=amount, soft_seconds=ledger['soft_seconds'], hard_seconds=ledger['hard_seconds'])
            else:
                reason = 'No new passing checks' if signature == ledger['progress'] else 'Automatic extension count exhausted'
                if getattr(owner, '_budget_review_reason', None) != reason:
                    owner._budget_review_reason = reason
                    owner.event('budget_review_required', reason=reason, remaining_seconds=max(0, remaining),
                                instruction='Origin re-estimates remaining work from evidence; do not ask user to repeat original scope')
        owner.deadline = current + max(0, ledger['soft_seconds'] - spent)


def charge(directory, seconds, source, evidence, request):
    """Import actual origin work once, with immutable evidence and an idempotent key."""
    number(seconds, 'charge seconds', positive=True)
    if not source.strip() or not request.strip():
        raise ValueError('actual charge source and stable request id are required')
    d, state, plan, fingerprint = binding(directory)
    proof = evidence_checksums({'items': [{'evidence': [str(Path(evidence).resolve())]}]})
    with (Path(state['repo'])/'.codex/tasks/.worker.lock').open('a+') as repo_lock:
        fcntl.flock(repo_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        core.ensure_resumable(d)
        with locked(d):
            ledger = load(d, fingerprint)
            if not ledger:
                ledger = dict(contract_hash=fingerprint, external_seconds=0, history=[], policy=None, evidence={},
                    soft_seconds=plan['limits']['runtime_seconds'], hard_seconds=plan['limits']['runtime_seconds'],
                    extensions=0, max_extensions=0, chunk_seconds=0, review_seconds=0, progress='', last_reason='Actual host work charged')
            payload = dict(seconds=seconds, source=source, evidence=proof, request=request)
            prior = [r for r in ledger['history'] if r['kind']=='host_charge' and r['details']['request']==request]
            if prior:
                if prior[0]['details'] != payload:
                    raise ValueError('charge request already recorded with different evidence or duration')
                return ledger
            ledger['external_seconds'] += seconds
            append(d, ledger, 'host_charge', **payload)
            return ledger


@contextlib.contextmanager
def external_wait(owner):
    """Pause both child and parent active clocks while waiting, preserving STOP guards."""
    owners = [owner]
    parent = getattr(getattr(owner, 'parent_guard', None), '__self__', None)
    if parent is not None:
        owners.append(parent)
    started = core.now()
    for obj in owners:
        obj.persist()
        obj._accounted_at = None
        obj._budget_waiting = True
        obj.state['accounting_paused'] = True
        obj.persist()
    try:
        yield
    finally:
        finished = core.now()
        for obj in owners:
            obj.deadline += max(0, finished-started)
            obj.state['waiting_seconds'] = obj.state.get('waiting_seconds', 0) + max(0, finished-started)
            obj._accounted_at = finished
            obj._budget_waiting = False
            obj.state.pop('accounting_paused', None)
            obj.persist()


def host_run(directory, argv, source):
    import sys
    from host_budget import run
    return run(directory, argv, source, accounting=sys.modules[__name__], core=core)
