"""Explicit v2 execution ledger and bounded, host-observed progress evidence.

This module grants no ownership, retries, delivery or resume authority. It is
used only inside the existing Runner/Workflow lock and process guards. Legacy
plans do not acquire a ledger: their historical failures remain unknown.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


POLICY_FIELDS = frozenset({'version', 'max_continuations', 'max_waits', 'max_yields', 'max_no_progress'})
COUNTERS = ('invocations', 'continuations', 'waits', 'yields', 'failures', 'active_seconds',
            'wait_seconds', 'yield_seconds')


def validate(policy):
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        raise ValueError('execution_policy requires exactly version and all four positive bounds')
    if type(policy['version']) is not int or policy['version'] != 2:
        raise ValueError('execution_policy.version must be integer 2')
    for key in POLICY_FIELDS - {'version'}:
        if type(policy[key]) is not int or policy[key] < 1:
            raise ValueError('execution_policy.' + key + ' must be a positive integer')


def enabled(plan):
    return 'execution_policy' in plan


def initial():
    return dict(version=2, **{key: 0 for key in COUNTERS}, no_progress=0, history=[])


def ledger(row):
    value = row.get('execution_accounting')
    if not isinstance(value, dict) or value.get('version') != 2:
        raise ValueError('v2 execution ledger missing; automatic historical migration is forbidden')
    return value


def total(rows):
    result = dict(version=2, **{key: 0 for key in COUNTERS})
    for row in rows:
        value = ledger(row)
        for key in COUNTERS:
            result[key] += value[key]
    return result


def begin(row, sequence, timestamp):
    value = ledger(row)
    if any(h['sequence'] == sequence for h in value['history']):
        raise ValueError('invocation sequence already recorded')
    value['invocations'] += 1
    value['history'].append(dict(sequence=sequence, started_at=timestamp, outcome='running'))


def finish(row, sequence, outcome, timestamp, *, failure=False, checkpoint=None):
    """Charge a terminal receipt at most once; callers persist it under the lock."""
    value = ledger(row)
    record = next(h for h in value['history'] if h['sequence'] == sequence)
    if record['outcome'] != 'running':
        return False
    record.update(outcome=outcome, finished_at=timestamp, failure=failure)
    if checkpoint:
        record['checkpoint'] = checkpoint
    if failure:
        value['failures'] += 1
    counter = {'continue': 'continuations', 'waiting': 'waits', 'yielded': 'yields'}.get(outcome)
    if counter:
        value[counter] += 1
    return True


def settle_wait(row, timestamp, *, end=False):
    value = ledger(row)
    start = row.get('wait_accounted_at')
    if start is not None:
        key = 'yield_seconds' if row.get('status') == 'yielded' else 'wait_seconds'
        value[key] += max(0, timestamp - start)
        row['wait_accounted_at'] = timestamp
    if end:
        row.pop('wait_accounted_at', None)


def bound(row, policy):
    value = ledger(row)
    for key, limit in (('continuations', 'max_continuations'), ('waits', 'max_waits'),
                       ('yields', 'max_yields'), ('no_progress', 'max_no_progress')):
        if value[key] >= policy[limit]:
            return limit
    return None


def check_signature(ok, logs, count):
    # Runner checks stop at the first failure. Unknown trailing checks cannot
    # be promoted to passes or compared by log contents/timestamps.
    return [True] * count if ok else [True] * max(0, len(logs)-1) + [False] + [None] * max(0, count-len(logs))


SOURCE_SUFFIXES = frozenset({'.py', '.pyi', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx',
    '.java', '.kt', '.go', '.rs', '.c', '.h', '.cpp', '.hpp', '.cs', '.rb', '.sh', '.zsh',
    '.html', '.css', '.scss', '.sql', '.md', '.rst', '.vue', '.swift'})
SOURCE_NAMES = frozenset({'Makefile', 'Dockerfile', 'CMakeLists.txt', 'package.json', 'pyproject.toml'})
EXCLUDED = frozenset({'node_modules', 'vendor', 'private', 'build', 'dist', 'target', '__pycache__'})
SENSITIVE = re.compile(r'(?i)(secret|token|password|credential|api[_-]?key|auth|session|rollout)')
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 2048, 512*1024, 8*1024*1024


def safe_source(name):
    path = Path(name)
    return (not path.is_absolute() and '..' not in path.parts and bool(path.parts)
            and all(not p.startswith('.') and p not in EXCLUDED and not SENSITIVE.search(p) for p in path.parts)
            and (path.suffix in SOURCE_SUFFIXES or path.name in SOURCE_NAMES))


def _read_source(root_fd, parts):
    """Open every component without following symlinks, including a racing one."""
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(child, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
                return None
            data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                return None
            return hashlib.sha256(data).hexdigest(), len(data)
    finally:
        os.close(fd)


def source_snapshot(repo):
    """Hash a bounded safe source set, never task ledgers, auth or full history.

    Only file names come from git; source bytes come from the current workspace.
    Incomplete observations are recorded but cannot prove source progress.
    """
    result = dict(files={}, complete=True, method='safe-source-sha256-v1')
    try:
        with __import__('tempfile').TemporaryFile() as names:
            subprocess.run(['git', '-C', str(repo), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                           stdout=names, stderr=subprocess.DEVNULL, check=True, timeout=5)
            names.seek(0)
            data = names.read(1024*1024 + 1)
        if len(data) > 1024*1024:
            result['complete'] = False
        selected = sorted({name for name in data[:1024*1024].decode('utf-8', 'replace').split('\0') if safe_source(name)})
        if len(selected) > MAX_FILES:
            result['complete'] = False
        root_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            size = 0
            for name in selected[:MAX_FILES]:
                try:
                    value = _read_source(root_fd, Path(name).parts)
                except OSError:
                    value = None
                if value is None:
                    result['complete'] = False
                    continue
                fingerprint, length = value
                size += length
                if size > MAX_TOTAL_BYTES:
                    result['complete'] = False
                    break
                result['files'][name] = fingerprint
        finally:
            os.close(root_fd)
    except (OSError, subprocess.SubprocessError):
        result['complete'] = False
    result['fingerprint'] = hashlib.sha256(json.dumps(result['files'], sort_keys=True).encode()).hexdigest()
    return result


def progress(before, after, previous_checks, current_checks):
    source_changed = before['complete'] and after['complete'] and before['fingerprint'] != after['fingerprint']
    checks_improved = bool(previous_checks is not None and current_checks is not None and
                           any(new is True and old is False for old, new in zip(previous_checks, current_checks)))
    return dict(verified=source_changed or checks_improved, source_changed=source_changed,
                checks_improved=checks_improved)
