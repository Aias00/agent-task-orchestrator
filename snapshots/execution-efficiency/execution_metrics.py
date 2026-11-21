"""Versioned observational spans in the existing bounded task trace.

No duration is model inference time unless a producer explicitly measures a
model_request span. Command duration is worker_unknown. These records grant no
execution, acceptance, retry or ownership authority and never migrate ledgers.
"""
import contextlib
import contextvars
import functools
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import time
import uuid

import task_trace

PHASES = frozenset({'queue', 'warmup', 'worker_unknown', 'model_request', 'tool',
    'independent_check', 'external_wait', 'checkpoint', 'restart',
    'orchestration_unknown', 'verification_reuse'})
MAX_SPANS = 4096
_CURRENT = contextvars.ContextVar('ato_observed_span', default=None)
_TASK = contextvars.ContextVar('ato_observed_task', default=None)
_ACTIVE_VALUE = contextvars.ContextVar('ato_observed_span_value', default=None)


def _number(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


def _write(directory, value, *, finish=False):
    """Best effort metadata only; bounded and idempotent terminal publication."""
    path = Path(directory)/'trace.sqlite3'
    try:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink():
                return False
        else:
            os.close(fd)
        with contextlib.closing(sqlite3.connect(str(path), timeout=2)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS phase_spans (seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, data TEXT NOT NULL)')
            with db:
                if finish:
                    old = db.execute('SELECT data FROM phase_spans WHERE id=?', (value['id'],)).fetchone()
                    if not old:
                        return False
                    prior = json.loads(old[0])
                    if prior.get('duration_seconds') is not None:
                        return prior == value
                    if any(prior[k] != value[k] for k in prior if k != 'process_started') or (prior.get('process_started') is True and value.get('process_started') is not True):
                        return False
                    db.execute('UPDATE phase_spans SET data=? WHERE id=?', (json.dumps(value), value['id']))
                else:
                    if db.execute('SELECT count(*) FROM phase_spans').fetchone()[0] >= MAX_SPANS:
                        # A single sentinel, not one unbounded record per drop.
                        db.execute('CREATE TABLE IF NOT EXISTS phase_overflow (id INTEGER PRIMARY KEY CHECK(id=1))')
                        db.execute('INSERT OR IGNORE INTO phase_overflow VALUES(1)')
                        return False
                    db.execute('INSERT INTO phase_spans(id,data) VALUES(?,?)', (value['id'], json.dumps(value)))
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return False


@contextlib.contextmanager
def span(directory, phase):
    """Monotonic duration, wall anchor, automatic same-trace parent relation.

    A process crash leaves an open span; queries never turn its age into time.
    An exceptional exit closes an observed span but does not imply successful
    delivery/cleanup/checks. No argv, prompt, token, tool content or native log.
    """
    if phase not in PHASES:
        raise ValueError('unknown timing phase')
    directory = str(Path(directory).resolve())
    parent = _CURRENT.get()
    value = dict(version=1, id=uuid.uuid4().hex, phase=phase,
                 parent=parent[1] if parent and parent[0] == directory else None,
                 started_at=time.time(), clock='monotonic_elapsed_wall_anchor')
    if phase in ('worker_unknown', 'independent_check', 'orchestration_unknown'):
        value['process_started'] = False
    scope = _TASK.get()
    if scope and scope[0] == directory:
        value['task'] = scope[1]
    started = time.monotonic()
    recorded = _write(directory, value)
    token = _CURRENT.set((directory, value['id'])) if recorded else None
    value_token = _ACTIVE_VALUE.set(value) if recorded else None
    outcome = 'returned'
    try:
        yield value['id'] if recorded else None
    except BaseException:
        outcome = 'raised'
        raise
    finally:
        duration = max(0, time.monotonic()-started)
        if token is not None:
            _CURRENT.reset(token)
            _ACTIVE_VALUE.reset(value_token)
        if recorded:
            _write(directory, dict(value, duration_seconds=duration,
                finished_at=value['started_at']+duration, outcome=outcome), finish=True)


def process_started():
    """Called only after Popen returns; not a guessed native/model startup."""
    value = _ACTIVE_VALUE.get()
    if value is not None and 'process_started' in value:
        value['process_started'] = True


def task_scope(fn):
    """Carry only a validated task ID through nested same-directory spans."""
    @functools.wraps(fn)
    def wrapped(owner, task, *args, **kwargs):
        key = task.get('id') if isinstance(task, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
            return fn(owner, task, *args, **kwargs)
        token = _TASK.set((str(Path(owner.dir).resolve()), key))
        try:
            return fn(owner, task, *args, **kwargs)
        finally:
            _TASK.reset(token)
    return wrapped


def measure(phase, *, when=None):
    """Instrument a Runner/Workflow operation without altering its protocol."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(owner, *args, **kwargs):
            if when is not None and not when(owner):
                return fn(owner, *args, **kwargs)
            selected = phase(owner, args, kwargs) if callable(phase) else phase
            with span(owner.dir, selected):
                return fn(owner, *args, **kwargs)
        return wrapped
    return decorate


def command_phase(owner, args, kwargs):
    if (args[4] if len(args) > 4 else kwargs.get('agent_kind')) is not None:
        return 'worker_unknown'
    name = args[1] if len(args) > 1 else kwargs.get('name', '')
    return 'independent_check' if '-check-' in name else 'orchestration_unknown'


def project(spans, *, truncated=False):
    """Partition the union of closed intervals; parents/children never sum.

    Nested spans charge the deepest observed phase. Concurrent leaves with
    different phases charge concurrent_unknown, not two copies of wall time.
    Missing, malformed and open observations stay explicit; no extrapolation.
    """
    issues = ['span_bound_exceeded'] if truncated else []
    rows, seen = {}, set()
    for row in spans:
        try:
            key = row['id']
            if (type(row['version']) is not int or row['version'] != 1 or not isinstance(key, str) or key in seen
                    or row['phase'] not in PHASES or not _number(row['started_at'])):
                raise ValueError()
            seen.add(key)
            rows[key] = row
        except (KeyError, TypeError, ValueError):
            issues.append('invalid_or_duplicate_span')
    valid, ancestry = {}, {}
    for key, row in rows.items():
        parents, current = set(), row
        try:
            while current.get('parent'):
                parent = current['parent']
                if parent == key or parent in parents or parent not in rows or len(parents) >= 64:
                    raise ValueError()
                parents.add(parent)
                current = rows[parent]
            ancestry[key] = parents
            duration = row.get('duration_seconds')
            if duration is None:
                issues.append('open_span')
                continue
            if (not _number(duration) or not _number(row.get('finished_at')) or
                    abs(row['finished_at'] - (row['started_at']+duration)) > .00001):
                raise ValueError()
            for parent in parents:
                p = rows[parent]
                if row['started_at'] < p['started_at'] or (p.get('finished_at') is not None and row['finished_at'] > p['finished_at']):
                    raise ValueError()
            valid[key] = row
        except (KeyError, TypeError, ValueError):
            issues.append('invalid_parent_or_interval')
    totals = {p: 0.0 for p in PHASES}
    totals['concurrent_unknown'] = 0.0
    points = sorted({x for r in valid.values() for x in (r['started_at'], r['finished_at'])})
    for left, right in zip(points, points[1:]):
        active = {k for k, r in valid.items() if r['started_at'] <= left and r['finished_at'] >= right}
        ancestors = set().union(*(ancestry[k] for k in active)) if active else set()
        leaves = active - ancestors
        phases = {valid[k]['phase'] for k in leaves}
        if phases:
            totals[next(iter(phases)) if len(phases) == 1 else 'concurrent_unknown'] += right-left
    covered = sum(totals.values())
    envelope = points[-1]-points[0] if points else None
    observed = {r['phase'] for r in valid.values()}
    # Unobserved phases remain unknown, including an empty old trace.
    for p in PHASES:
        if p not in observed:
            totals[p] = None
    return dict(version=1, available=bool(rows), complete=bool(rows) and not issues,
        basis='exclusive interval union; deepest child wins; concurrent ambiguity is unknown',
        clock='monotonic elapsed with wall anchors; not provider latency',
        phases_seconds=totals, covered_seconds=covered, observed_wall_seconds=envelope,
        uninstrumented_gap_seconds=max(0, envelope-covered) if envelope is not None else None,
        model_latency='measured' if 'model_request' in observed else 'unknown',
        model_request_count=sum(r['phase'] == 'model_request' for r in valid.values()) if 'model_request' in observed else None,
        span_count=len(rows), closed_span_count=len(valid), open_span_count=sum(r.get('duration_seconds') is None for r in rows.values()),
        check_attempts=sum(r['phase'] == 'independent_check' for r in valid.values()),
        check_executions=sum(r['phase'] == 'independent_check' and r.get('process_started') is True for r in valid.values()),
        check_launches_unknown=sum(r['phase'] == 'independent_check' and (r.get('duration_seconds') is None or type(r.get('process_started')) is not bool) for r in rows.values()),
        worker_process_startups=sum(r['phase'] == 'worker_unknown' and r.get('process_started') is True for r in valid.values()),
        check_reuses=sum(r['phase'] == 'verification_reuse' for r in valid.values()),
        issues=sorted(set(issues)), spans=list(rows.values()))


def query(directory, task=None):
    path = Path(directory)/'trace.sqlite3'
    empty = project([])
    if path.is_symlink() or not path.is_file():
        return empty
    try:
        with task_trace.read_snapshot(path) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase_spans'").fetchone():
                return empty
            rows = db.execute('SELECT data FROM phase_spans ORDER BY seq LIMIT ?', (MAX_SPANS+1,)).fetchall()
            overflow = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase_overflow'").fetchone()
        decoded = [json.loads(r[0]) for r in rows[:MAX_SPANS]]
        if any(not isinstance(r, dict) for r in decoded):
            raise ValueError('invalid span record')
        return project([r for r in decoded if task is None or r.get('task') == task],
                       truncated=bool(overflow) or len(rows) > MAX_SPANS)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return dict(empty, issues=['trace_unavailable'])


def view(directory, ledger=None, *, children=(), task=None):
    sources = [query(d, task=task) for d in dict.fromkeys([str(directory), *map(str, children)])]
    result = project([s for source in sources for s in source['spans']],
                     truncated=any('span_bound_exceeded' in source['issues'] for source in sources))
    result['issues'] = sorted(set(result['issues']) | {i for source in sources for i in source['issues']})
    result['complete'] = result['complete'] and not result['issues']
    result['source_count'] = len(sources)
    result.pop('spans', None)
    # Existing v2 ledgers have different scope (settled command sums, including
    # checks, and waits). Preserve them, never add them to span phase totals.
    result['v2_ledger'] = {k: (ledger or {}).get(k) for k in
        ('active_seconds', 'wait_seconds', 'yield_seconds', 'unsettled_wait_seconds', 'unsettled_yield_seconds')}
    result['ledger_basis'] = 'separate cumulative ledger; not additive with span wall/phase totals'
    return result
