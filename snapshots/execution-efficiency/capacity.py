"""Durable, non-preemptive endpoint admission. Capacity never grants authority.

All changes serialize in SQLite transactions, including ordering and occupancy.
Claims have no expiry: only an exact receipt with confirmed cleanup releases a
slot. Restarting a caller, queue polling and elapsed time cannot reclaim it.
The public store is also usable with isolated temporary databases in tests.
"""
import contextlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid


def alias(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', value)
            or re.search(r'(?i)(^|[-_])(key|token|password|secret|credential)([-_]|$)', value)):
        raise ValueError('capacity endpoint must be a plain non-secret alias, never a URL or credential')
    return value


def limits(limit, reserved_interactive):
    if (type(limit) is not int or limit < 1 or type(reserved_interactive) is not int
            or not 0 <= reserved_interactive < limit):
        raise ValueError('capacity requires integer limit > 0 and 0 <= reserved_interactive < limit')


def validate(selection):
    if not isinstance(selection, dict) or set(selection) != {'endpoint', 'priority'}:
        raise ValueError('capacity requires exactly endpoint and priority; plans cannot set global limits')
    alias(selection['endpoint'])
    if selection['priority'] not in ('interactive', 'background'):
        raise ValueError('capacity priority must be interactive or background')


def identity(request_id, owner, generation):
    if any(not isinstance(s, str) or not s.strip() or len(s) > 1024 for s in (request_id, owner)):
        raise ValueError('capacity request and owner must be nonempty bounded identifiers')
    if type(generation) is not int or generation < 0:
        raise ValueError('capacity generation must be a nonnegative integer')


class CapacityStore:
    def __init__(self, path, clock=time.time, *, read_only=False):
        self.path, self.clock = Path(path), clock
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError('capacity database cannot be a symlink')
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS endpoints (endpoint TEXT PRIMARY KEY, '
                       'limit_value INTEGER NOT NULL, reserved_interactive INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS requests (sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'request_id TEXT UNIQUE NOT NULL, endpoint TEXT NOT NULL, priority TEXT NOT NULL, '
                       'owner TEXT NOT NULL, generation INTEGER NOT NULL, state TEXT NOT NULL, '
                       'enqueued_at REAL NOT NULL, claimed_at REAL, finished_at REAL, token TEXT UNIQUE)')
            db.execute('CREATE INDEX IF NOT EXISTS capacity_order ON requests(endpoint, state, priority, sequence)')

    @contextlib.contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _time(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('capacity clock must return finite seconds')
        return value

    def configure(self, endpoint, limit, reserved_interactive):
        alias(endpoint)
        limits(limit, reserved_interactive)
        with self._transaction() as db:
            # A reduction affects future admission only; never revoke holders.
            db.execute('INSERT INTO endpoints VALUES (?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET '
                       'limit_value=excluded.limit_value, reserved_interactive=excluded.reserved_interactive',
                       (endpoint, limit, reserved_interactive))

    def enqueue(self, endpoint, request_id, priority, owner, generation):
        validate(dict(endpoint=endpoint, priority=priority))
        identity(request_id, owner, generation)
        with self._transaction() as db:
            if not db.execute('SELECT 1 FROM endpoints WHERE endpoint=?', (endpoint,)).fetchone():
                raise ValueError('capacity endpoint is not configured: ' + endpoint)
            row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if (row['endpoint'], row['priority'], row['owner'], row['generation']) != (endpoint, priority, owner, generation):
                    raise ValueError('capacity request identity differs; no owner/generation/priority reassignment')
            else:
                db.execute('INSERT INTO requests(request_id, endpoint, priority, owner, generation, state, enqueued_at) '
                           "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                           (request_id, endpoint, priority, owner, generation, self._time()))
                row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            return dict(row)

    def claim(self, request_id):
        with self._transaction() as db:
            row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if not row or row['state'] not in ('pending', 'active'):
                return None
            if row['state'] == 'active':
                return dict(row)  # Idempotent receipt, never a second lease.
            endpoint = row['endpoint']
            config = db.execute('SELECT * FROM endpoints WHERE endpoint=?', (endpoint,)).fetchone()
            occupied = db.execute("SELECT priority FROM requests WHERE endpoint=? AND state='active'", (endpoint,)).fetchall()
            if len(occupied) >= config['limit_value']:
                return None
            if row['priority'] == 'background':
                # Reserve stays unused by background even with no interactive queue.
                if sum(r['priority'] == 'background' for r in occupied) >= config['limit_value'] - config['reserved_interactive']:
                    return None
                interactive = db.execute("SELECT COUNT(*) FROM requests WHERE endpoint=? AND state='pending' AND priority='interactive'", (endpoint,)).fetchone()[0]
                # Background may enter when every queued interaction still fits
                # in the remaining free slots (in particular the reserve).
                # At contention for a slot, interaction is always served first.
                if interactive > config['limit_value'] - len(occupied) - 1:
                    return None
            first = db.execute("SELECT request_id FROM requests WHERE endpoint=? AND state='pending' AND priority=? ORDER BY sequence LIMIT 1",
                               (endpoint, row['priority'])).fetchone()
            if first['request_id'] != request_id:
                return None
            db.execute("UPDATE requests SET state='active', token=?, claimed_at=? WHERE request_id=?",
                       (uuid.uuid4().hex, self._time(), request_id))
            return dict(db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone())

    def release(self, request_id, token, cleanup_confirmed=True, *, owner=None, generation=None):
        if cleanup_confirmed is not True or not isinstance(token, str) or not token:
            return False
        with self._transaction() as db:
            row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if (not row or row['state'] != 'active' or row['token'] != token
                    or (owner is not None and row['owner'] != owner)
                    or (generation is not None and (type(generation) is not int or row['generation'] != generation))):
                return False
            db.execute("UPDATE requests SET state='released', finished_at=? WHERE request_id=?", (self._time(), request_id))
            return True

    def cancel(self, request_id, owner, generation):
        identity(request_id, owner, generation)
        with self._transaction() as db:
            return db.execute("UPDATE requests SET state='cancelled', finished_at=? WHERE request_id=? AND owner=? AND generation=? AND state='pending'",
                              (self._time(), request_id, owner, generation)).rowcount == 1

    def snapshot(self, endpoint):
        alias(endpoint)
        # A read-only connection avoids schema creation or write transactions.
        with contextlib.closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            config = db.execute('SELECT * FROM endpoints WHERE endpoint=?', (endpoint,)).fetchone()
            if not config:
                raise ValueError('capacity endpoint is not configured: ' + endpoint)
            rows = [dict(r) for r in db.execute('SELECT * FROM requests WHERE endpoint=? ORDER BY sequence', (endpoint,))]
        timestamp = self._time()
        for row in rows:
            row.pop('token', None)  # Query is not a release credential.
            row['wait_seconds'] = max(0, (row['claimed_at'] if row['claimed_at'] is not None else
                                         row['finished_at'] if row['finished_at'] is not None else timestamp) - row['enqueued_at'])
        return dict(version=1, endpoint=endpoint, limit=config['limit_value'], reserved_interactive=config['reserved_interactive'],
                    active=[r for r in rows if r['state'] == 'active'], pending=[r for r in rows if r['state'] == 'pending'],
                    requests=rows, oldest_background_wait_seconds=max((r['wait_seconds'] for r in rows
                        if r['state'] == 'pending' and r['priority'] == 'background'), default=0),
                    fairness='interactive priority; FIFO within priority; background starvation possible',
                    reclamation='exact receipt and confirmed cleanup only; no TTL')


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate capacity configuration field: ' + key)
        value[key] = item
    return value


def trusted_config():
    """Read only ATO_HOME/capacity.json, never a plan/worker-supplied path.

    Deployment must keep ATO_HOME outside worker writable roots. Same-uid code
    with unrestricted filesystem access is not a security boundary provided by
    this queue. No permissions are broadened here.
    """
    import ato_config
    home = ato_config.ato_home()
    metadata = home.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        raise ValueError('trusted capacity home must be owned by this user and not group/world writable')
    path = home/'capacity.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ValueError('trusted ATO_HOME/capacity.json unavailable; capacity opt-in cannot run') from error
    with os.fdopen(fd, 'r') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o022 or metadata.st_size > 65536):
            raise ValueError('capacity.json must be a bounded, owned regular file without group/world write')
        config = json.loads(stream.read(65537), object_pairs_hook=_object)
    if (not isinstance(config, dict) or set(config) != {'version', 'endpoints'}
            or type(config['version']) is not int or config['version'] != 1
            or not isinstance(config['endpoints'], dict) or not config['endpoints']):
        raise ValueError('capacity.json requires version=1 and nonempty endpoints')
    for endpoint, entry in config['endpoints'].items():
        alias(endpoint)
        if not isinstance(entry, dict) or set(entry) != {'limit', 'reserved_interactive'}:
            raise ValueError('capacity endpoint requires exactly limit and reserved_interactive')
        limits(**entry)
    return home, config


def configured_store(selection):
    validate(selection)
    home, config = trusted_config()
    endpoint = selection['endpoint']
    if endpoint not in config['endpoints']:
        raise ValueError('capacity alias missing from trusted configuration: ' + endpoint)
    store = CapacityStore(home/'capacity.sqlite3')
    store.configure(endpoint, **config['endpoints'][endpoint])
    return store


def cancel_stopped(directory):
    """The existing stop command may cancel pending receipts, never claims.

    This also works when a queue owner died. It does not release repository
    ownership, kill work, infer cleanup, remove STOP or authorize a restart.
    Exact deterministic bindings are checked before touching the fixed store.
    Task state is left to its current owner to avoid competing JSON writers.
    """
    import ato_config
    import runner
    d = Path(directory).resolve()
    state = runner.read(d/'state.json')
    if not (d/'STOP').exists():
        return
    plan = runner.read(d/('contract.json' if state.get('kind') == 'workflow' else 'plan.json'))
    if 'capacity' not in plan:
        return  # Legacy STOP does not gain new plan/queue requirements.
    contract = state.get('contract_hash') or state.get('plan_hash')
    if runner.digest(plan) != contract:
        raise ValueError('STOP capacity cancellation requires the frozen plan binding')
    _cancel_pending(d, state, plan)
    if state.get('kind') == 'workflow' and state.get('active_run'):
        child = Path(state['active_run']).resolve()
        relation = runner.read(child/'parent.json')
        cs = runner.read(child/'state.json')
        if (relation['workflow_dir'] != str(d) or relation['workflow_id'] != state['id']
                or cs['repo'] != state['repo'] or child.parent != Path(state['repo'])/'.codex/tasks'
                or cs['id'] != child.name
                or not any(s['run_dir'] == str(child) and s['covers'] == relation['covers'] for s in state['stages'])):
            raise ValueError('STOP capacity cancellation stage binding differs')
        # Parent STOP already fences the child through parent_guard. Do not add
        # an independent child STOP/history mutation just to cancel its queue.
        child_plan = runner.read(child/'plan.json')
        if child_plan.get('capacity') != plan['capacity']:
            raise ValueError('STOP child capacity selection differs from workflow')
        _cancel_pending(child, cs, child_plan)


def _cancel_pending(directory, state, plan):
    """Cancel exact receipts without competing with the owner's task writer."""
    import ato_config
    import runner
    contract = state.get('contract_hash') or state.get('plan_hash')
    if runner.digest(plan) != contract:
        raise ValueError('STOP capacity cancellation plan binding differs')
    path = ato_config.ato_home()/'capacity.sqlite3'
    if not path.is_file():
        return
    store = CapacityStore(path)
    rows = (dict(planner=state.get('planning_execution', state.get('capacity_planning', {})))
            if state.get('kind') == 'workflow' else state.get('tasks', {}))
    for key, row in rows.items():
        receipt = row.get('capacity_request', {})
        if receipt.get('state') != 'pending':
            continue
        owner = runner.digest(dict(directory=str(directory), repo=str(Path(state['repo']).resolve()),
                                   contract=contract, task=key))
        generation = receipt['generation']
        request = runner.digest(dict(owner=owner, generation=generation))
        if (receipt['owner'] != owner or receipt['request_id'] != request or receipt['store'] != str(path)
                or receipt['endpoint'] != plan['capacity']['endpoint'] or receipt['priority'] != plan['capacity']['priority']):
            raise ValueError('STOP capacity receipt binding differs')
        store.cancel(request, owner, generation)
