"""Local, agent-neutral durable mailbox. No CLI invocation or implicit authorization.

See docs/message-protocol.md for the public API and crash/late-receipt contract.
Only a trusted local host should create routes and register endpoint identities.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid


SCHEMA_VERSION = 2
TERMINAL = frozenset({'replied', 'completed', 'turn_completed', 'failed', 'expired', 'cancelled'})


class MailboxError(ValueError):
    pass


class IdentityError(MailboxError):
    pass


class ConflictError(MailboxError):
    pass


@dataclass(frozen=True)
class EndpointCredential:
    endpoint_id: str
    session_id: str
    project: str
    token: str = field(repr=False)


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise MailboxError(label+' must be a nonempty string (at most 4096 characters)')
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise MailboxError(label+' must be a finite number')
    return value


def _newer_generation(current, previous):
    """Only supported durable generation formats can advance, never rewind."""
    if previous is None:
        return current is not None
    if type(current) is int and type(previous) is int:
        return current > previous >= 0
    if (isinstance(current, list) and isinstance(previous, list)
            and len(current) == len(previous) == 2
            and type(current[1]) is int and type(previous[1]) is int):
        return (current[1] >= previous[1] >= 0
                and (current[0] or 0) >= (previous[0] or 0) and current != previous)
    return False


class MessageBus:
    """An explicit database path prevents accidental access to live hub data.

    Each call uses its own FULL/WAL SQLite transaction. ``clock`` is injectable
    for deterministic lease/timeout tests; production uses wall-clock seconds.
    """

    def __init__(self, path, *, clock=time.time):
        self.path = Path(path).expanduser().resolve()
        self.clock = clock
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create private new stores without changing modes on existing state.
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with closing(self._connect()) as con:
            version = con.execute('pragma user_version').fetchone()[0]
            if version not in (0, 1, SCHEMA_VERSION):
                raise MailboxError('unsupported mailbox schema version: '+str(version))
            con.execute('pragma journal_mode=WAL')
            con.executescript('''
                create table if not exists endpoints(
                    id text primary key, kind text not null, session_id text not null,
                    project text not null, source text not null, token_hash text not null,
                    state text not null, created_at real not null, updated_at real not null,
                    owner text, owner_token text, owner_until real,
                    unique(kind, session_id));
                create table if not exists bindings(
                    id text primary key, sender text not null references endpoints(id),
                    target text not null references endpoints(id), task_id text not null,
                    contract_hash text not null, source text not null, state text not null,
                    created_at real not null, updated_at real not null,
                    unique(sender,target,task_id,contract_hash));
                create table if not exists messages(
                    seq integer primary key autoincrement, id text unique not null,
                    sender text not null references endpoints(id),
                    target text not null references endpoints(id),
                    binding_id text not null references bindings(id),
                    kind text not null, correlation_id text not null, reply_to text references messages(id),
                    task_id text not null, contract_hash text not null,
                    idempotency_key text not null, fingerprint text not null,
                    state text not null, busy integer not null default 0,
                    external_possible integer not null default 0,
                    created_at real not null, updated_at real not null, expires_at real,
                    claim_token text, claim_until real, claim_owner text, attempt_id text,
                    external_id text, accepted_at real, reply_id text,
                    unique(sender,idempotency_key));
                create unique index if not exists endpoint_serial
                    on messages(target) where busy=1;
                create index if not exists pending_messages on messages(target,state,seq);
                create table if not exists bodies(
                    message_id text primary key references messages(id), body text not null);
                create table if not exists events(
                    seq integer primary key autoincrement, at real not null,
                    message_id text references messages(id), endpoint_id text references endpoints(id),
                    kind text not null, data text not null);
                create table if not exists receipts(
                    endpoint_id text not null references endpoints(id), receipt_id text not null,
                    fingerprint text not null, message_id text not null references messages(id),
                    kind text not null, late integer not null, at real not null,
                    primary key(endpoint_id,receipt_id));
                create table if not exists receipt_bodies(
                    endpoint_id text not null, receipt_id text not null, body text not null,
                    primary key(endpoint_id,receipt_id),
                    foreign key(endpoint_id,receipt_id) references receipts(endpoint_id,receipt_id));
                create table if not exists execution_leases(
                    resource text primary key, project text not null, task_id text not null,
                    contract_hash text not null, generation text not null, role text not null,
                    owner text not null, token_hash text, fence integer not null,
                    lease_until real not null, state text not null, source text not null);
            ''')
            # Additive migration: old rows retain v1 semantics and exact IDs.
            # BEGIN IMMEDIATE also makes concurrent first-open migrations safe.
            con.execute('begin immediate')
            for table, columns in {
                'endpoints': {'generation': 'integer not null default 0'},
                'bindings': {'generation': "text not null default 'null'",
                             'route_generation': 'integer not null default 0'},
                'messages': {'protocol': 'integer not null default 1',
                    'generation': "text not null default 'null'",
                    'route_generation': 'integer not null default 0',
                    'endpoint_generation': 'integer', 'turn_id': 'text',
                    'canonical_id': 'text', 'authority_source': 'text'},
            }.items():
                present = {r[1] for r in con.execute('pragma table_info('+table+')')}
                for name, definition in columns.items():
                    if name not in present:
                        con.execute('alter table '+table+' add column '+name+' '+definition)
            con.execute('pragma user_version='+str(SCHEMA_VERSION))
            con.commit()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute('pragma foreign_keys=ON')
        con.execute('pragma synchronous=FULL')
        return con

    @contextmanager
    def _transaction(self):
        con = self._connect()
        try:
            con.execute('begin immediate')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _event(self, con, kind, *, message=None, endpoint=None, **data):
        con.execute('insert into events(at,message_id,endpoint_id,kind,data) values(?,?,?,?,?)',
                    (self.clock(), message, endpoint, kind, _json(data)))

    def _endpoint(self, con, identifier):
        row = con.execute('select * from endpoints where id=?', (identifier,)).fetchone()
        if row is None:
            raise IdentityError('unknown endpoint')
        return dict(row)

    def _authenticate(self, con, credential):
        if not isinstance(credential, EndpointCredential):
            raise IdentityError('endpoint credential required')
        ep = self._endpoint(con, credential.endpoint_id)
        if (ep['session_id'] != credential.session_id or ep['project'] != credential.project
                or not hmac.compare_digest(ep['token_hash'], _hash(credential.token))):
            raise IdentityError('endpoint session, project or capability mismatch')
        return ep

    def register_endpoint(self, endpoint_id, *, kind, session_id, project, source):
        """Register an immutable identity and return its credential exactly once.

        Source is the host's verification evidence, not model-provided proof.
        Re-registration never rotates identities or silently steals a session.
        """
        for value, label in [(endpoint_id, 'endpoint_id'), (session_id, 'session_id'), (source, 'source')]:
            _text(value, label)
        if not isinstance(kind, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', kind):
            raise MailboxError('invalid transport kind')
        project = str(Path(project).expanduser().resolve(strict=True))
        if not Path(project).is_dir():
            raise MailboxError('project must be a verified local directory')
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._transaction() as con:
            try:
                con.execute('''insert into endpoints(id,kind,session_id,project,source,token_hash,
                    state,created_at,updated_at) values(?,?,?,?,?,?,'active',?,?)''',
                            (endpoint_id, kind, session_id, project, source, _hash(token), now, now))
            except sqlite3.IntegrityError as exc:
                raise ConflictError('endpoint ID or native session already registered') from exc
            self._event(con, 'endpoint_registered', endpoint=endpoint_id, kind_name=kind, source=source)
        return EndpointCredential(endpoint_id, session_id, project, token)

    def endpoint(self, endpoint_id):
        with self._transaction() as con:
            ep = self._endpoint(con, endpoint_id)
        return {k: v for k, v in ep.items() if k not in ('token_hash', 'owner_token')}

    def authenticate(self, credential):
        """Validate a host-held credential without exposing its hash."""
        with self._transaction() as con:
            self._authenticate(con, credential)
        return self.endpoint(credential.endpoint_id)

    def bind_route(self, *, sender, target, task_id, contract_hash, source, cross_project=False,
                   generation=None, route_generation=0, advance=False):
        """Trusted host API: explicit routing permission, never business/budget permission."""
        for value, label in [(task_id, 'task_id'), (contract_hash, 'contract_hash'), (source, 'source')]:
            _text(value, label)
        now = self.clock()
        generation = _json(generation)
        if type(route_generation) is not int or route_generation < 0:
            raise MailboxError('invalid route generation')
        with self._transaction() as con:
            a, b = self._endpoint(con, sender), self._endpoint(con, target)
            if a['state'] != 'active' or b['state'] != 'active':
                raise MailboxError('route requires active endpoints')
            if a['project'] != b['project'] and cross_project is not True:
                raise IdentityError('cross-project routing requires explicit host binding')
            old = con.execute('''select * from bindings where sender=? and target=? and
                task_id=? and contract_hash=?''', (sender, target, task_id, contract_hash)).fetchone()
            if old:
                if (advance and old['state'] == 'active' and old['source'] == source
                        and old['generation'] != generation
                        and _newer_generation(json.loads(generation), json.loads(old['generation']))):
                    con.execute('''update bindings set generation=?,route_generation=route_generation+1,
                        updated_at=? where id=?''', (generation, now, old['id']))
                    self._event(con, 'route_generation_advanced', binding_id=old['id'], source=source)
                    self._recover(con)
                    return dict(con.execute('select * from bindings where id=?', (old['id'],)).fetchone())
                if (old['state'] != 'active' or old['source'] != source
                        or old['generation'] != generation
                        or not advance and old['route_generation'] != route_generation):
                    raise ConflictError('existing route cannot be rebound or reauthorized implicitly')
                return dict(old)
            identifier = 'route-'+uuid.uuid4().hex
            con.execute('''insert into bindings(id,sender,target,task_id,contract_hash,source,state,
                created_at,updated_at,generation,route_generation) values(?,?,?,?,?,?,?,?,?,?,?)''',
                        (identifier, sender, target, task_id, contract_hash, source, 'active', now, now,
                         generation, route_generation))
            self._event(con, 'route_bound', binding_id=identifier, source=source,
                        sender=sender, target=target, cross_project=a['project'] != b['project'])
            return dict(con.execute('select * from bindings where id=?', (identifier,)).fetchone())

    def _binding(self, con, identifier):
        row = con.execute('select * from bindings where id=?', (identifier,)).fetchone()
        if row is None:
            raise IdentityError('unknown route binding')
        return dict(row)

    def _valid(self, con, message):
        binding = self._binding(con, message['binding_id'])
        return (binding['state'] == 'active'
                and (message.get('protocol', 1) == 1 or
                     (message['generation'] == binding['generation']
                      and message['route_generation'] == binding['route_generation']))
                and all(self._endpoint(con, message[k])['state'] == 'active' for k in ('sender', 'target')))

    def advance_route(self, binding_id, *, generation, source):
        """Trusted host fencing after an independently verified execution advance.

        STOP/supersession is irreversible for this route. A generation change
        cancels old envelopes and preserves possible native effects as busy.
        """
        _text(source, 'source')
        encoded = _json(generation)
        if generation is None:
            raise MailboxError('concrete generation required')
        with self._transaction() as con:
            binding = self._binding(con, binding_id)
            if binding['state'] != 'active':
                raise ConflictError('inactive routes cannot advance')
            if binding['generation'] != encoded:
                if not _newer_generation(generation, json.loads(binding['generation'])):
                    raise ConflictError('execution generation cannot rewind')
                con.execute('''update bindings set generation=?,route_generation=route_generation+1,
                    updated_at=? where id=?''', (encoded, self.clock(), binding_id))
                self._event(con, 'route_generation_advanced', binding_id=binding_id, source=source)
                self._recover(con)

    def set_binding_state(self, binding_id, state, *, source):
        if state not in ('paused', 'stopped', 'superseded'):
            raise MailboxError('routes may only be paused, stopped or superseded; bind a new contract to resume')
        _text(source, 'source')
        with self._transaction() as con:
            self._binding(con, binding_id)
            con.execute('update bindings set state=?,updated_at=? where id=?', (state, self.clock(), binding_id))
            self._event(con, 'route_'+state, binding_id=binding_id, source=source)
            self._recover(con)

    def set_endpoint_state(self, credential, state, *, source):
        if state not in ('active', 'paused', 'closed'):
            raise MailboxError('invalid endpoint state')
        _text(source, 'source')
        with self._transaction() as con:
            ep = self._authenticate(con, credential)
            if ep['state'] == 'closed' and state != 'closed':
                raise ConflictError('closed endpoints cannot be revived')
            con.execute('update endpoints set state=?,updated_at=? where id=?',
                        (state, self.clock(), ep['id']))
            self._event(con, 'endpoint_'+state, endpoint=ep['id'], source=source)
            self._recover(con)

    def acquire_endpoint(self, credential, *, owner, lease_seconds=30):
        _text(owner, 'owner')
        self._ttl(lease_seconds)
        with self._transaction() as con:
            self._recover(con)
            ep = self._authenticate(con, credential)
            if ep['state'] != 'active':
                raise ConflictError('endpoint is not active')
            if ep['owner_token'] and ep['owner_until'] > self.clock():
                raise ConflictError('endpoint is already owned')
            token = secrets.token_urlsafe(24)
            con.execute('update endpoints set owner=?,owner_token=?,owner_until=?,generation=generation+1 where id=?',
                        (owner, token, self.clock()+lease_seconds, ep['id']))
            self._event(con, 'owner_acquired', endpoint=ep['id'], owner=owner)
            return token

    @staticmethod
    def _ttl(seconds):
        if _number(seconds, 'lease_seconds') <= 0:
            raise MailboxError('lease_seconds must be positive')

    def _owner(self, con, credential, owner_token):
        ep = self._authenticate(con, credential)
        if (ep['state'] != 'active' or not ep['owner_token'] or not isinstance(owner_token, str)
                or not hmac.compare_digest(ep['owner_token'], owner_token) or ep['owner_until'] <= self.clock()):
            raise ConflictError('endpoint ownership lease lost')
        return ep

    def renew_endpoint(self, credential, owner_token, *, lease_seconds=30):
        self._ttl(lease_seconds)
        with self._transaction() as con:
            ep = self._owner(con, credential, owner_token)
            con.execute('update endpoints set owner_until=? where id=?',
                        (self.clock()+lease_seconds, ep['id']))

    def release_endpoint(self, credential, owner_token):
        with self._transaction() as con:
            ep = self._owner(con, credential, owner_token)
            # Releasing a process lease cannot make an in-flight native turn replayable.
            con.execute('update endpoints set owner=null,owner_token=null,owner_until=null where id=?', (ep['id'],))
            self._event(con, 'owner_released', endpoint=ep['id'], owner=ep['owner'])
            self._recover(con)

    def _message(self, con, identifier):
        row = con.execute('select * from messages where id=?', (identifier,)).fetchone()
        if row is None:
            raise MailboxError('unknown message')
        result = dict(row)
        result['body'] = con.execute('select body from bodies where message_id=?', (identifier,)).fetchone()[0]
        result['route_source'] = self._binding(con, result['binding_id'])['source']
        return result

    def message(self, identifier):
        """Read-only snapshot; call recover() to reconcile deadlines explicitly."""
        with self._transaction() as con:
            return self._message(con, identifier)

    def _enqueue(self, con, credential, *, binding_id, target, kind, body, idempotency_key,
                 task_id, contract_hash, correlation_id=None, reply_to=None, expires_at=None,
                 protocol=1, generation=None, canonical_id=None, authority_source=None):
        ep = self._authenticate(con, credential)
        for value, label in [(target, 'target'), (kind, 'kind'), (idempotency_key, 'idempotency_key')]:
            _text(value, label)
        if not isinstance(body, str) or len(body.encode()) > 1024*1024:
            raise MailboxError('body must be text of at most 1 MiB')
        if expires_at is not None:
            _number(expires_at, 'expires_at')
        if correlation_id is not None:
            _text(correlation_id, 'correlation_id')
        binding = self._binding(con, binding_id)
        if protocol not in (1, 2):
            raise MailboxError('unsupported message protocol')
        if protocol == 2:
            _text(canonical_id, 'canonical_id')
            _text(authority_source, 'authority_source')
            if generation is None or binding['generation'] != _json(generation):
                raise IdentityError('execution generation does not match route')
        expected = (binding['sender'], binding['target'])
        if reply_to:
            parent = self._message(con, reply_to)
            if (parent['target'] != ep['id'] or parent['sender'] != target
                    or parent['binding_id'] != binding_id or correlation_id != parent['correlation_id']):
                raise IdentityError('reply does not match its original message')
            expected = (parent['target'], parent['sender'])
        if ((ep['id'], target) != expected or binding['task_id'] != task_id
                or binding['contract_hash'] != contract_hash):
            raise IdentityError('sender, target, task or contract does not match the verified route')
        contents = dict(binding_id=binding_id, sender=ep['id'], target=target, kind=kind,
            body=body, task_id=task_id, contract_hash=contract_hash, correlation_id=correlation_id,
            reply_to=reply_to, expires_at=expires_at)
        if protocol == 2:
            contents.update(protocol=protocol, generation=generation, canonical_id=canonical_id,
                            authority_source=authority_source)
        fingerprint = _hash(_json(contents))
        old = con.execute('select id,fingerprint from messages where sender=? and idempotency_key=?',
                          (ep['id'], idempotency_key)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise ConflictError('idempotency key reused with different contents')
            return self._message(con, old['id'])
        if (binding['state'] != 'active' or ep['state'] != 'active'
                or self._endpoint(con, target)['state'] != 'active'):
            raise ConflictError('route or endpoint is not active')
        if expires_at is not None and expires_at <= self.clock():
            raise MailboxError('new message is already expired')
        identifier = 'msg-'+uuid.uuid4().hex
        now = self.clock()
        con.execute('''insert into messages(id,sender,target,binding_id,kind,correlation_id,reply_to,
            task_id,contract_hash,idempotency_key,fingerprint,state,created_at,updated_at,expires_at)
            values(?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,?)''',
                    (identifier, ep['id'], target, binding_id, kind, correlation_id or identifier, reply_to,
                     task_id, contract_hash, idempotency_key, fingerprint, now, now, expires_at))
        con.execute('insert into bodies values(?,?)', (identifier, body))
        con.execute('''update messages set protocol=?,generation=?,route_generation=?,
            canonical_id=?,authority_source=? where id=?''',
            (protocol, _json(generation), binding['route_generation'], canonical_id, authority_source, identifier))
        self._event(con, 'enqueued', message=identifier, endpoint=ep['id'])
        return self._message(con, identifier)

    def enqueue(self, credential, *, binding_id, target, kind, body, idempotency_key,
                task_id, contract_hash, correlation_id=None, expires_at=None, protocol=1,
                generation=None, canonical_id=None, authority_source=None):
        with self._transaction() as con:
            return self._enqueue(con, credential, binding_id=binding_id, target=target, kind=kind,
                                 body=body, idempotency_key=idempotency_key, task_id=task_id,
                                 contract_hash=contract_hash, correlation_id=correlation_id, expires_at=expires_at,
                                 protocol=protocol, generation=generation, canonical_id=canonical_id,
                                 authority_source=authority_source)

    def _state(self, con, message, state, *, busy=None, **data):
        con.execute('update messages set state=?,updated_at=?,busy=? where id=?',
                    (state, self.clock(), message['busy'] if busy is None else int(busy), message['id']))
        self._event(con, state, message=message['id'], **data)

    def _recover(self, con):
        now = self.clock()
        rows = con.execute('select * from messages where state not in ('+
                           ','.join('?' for _ in TERMINAL)+')', tuple(TERMINAL)).fetchall()
        for row in rows:
            message = dict(row)
            if not self._valid(con, message):
                self._state(con, message, 'cancelled', busy=message['busy'] and message['external_possible'], reason='binding_or_endpoint_inactive')
            elif message['expires_at'] is not None and message['expires_at'] <= now:
                self._state(con, message, 'expired', busy=message['busy'] and message['external_possible'], reason='deadline')
            elif message['state'] in ('claimed', 'dispatching'):
                ep = self._endpoint(con, message['target'])
                if (message['claim_until'] <= now or not ep['owner_token']
                        or ep['owner_token'] != message['claim_owner'] or ep['owner_until'] <= now):
                    if message['state'] == 'claimed':
                        self._state(con, message, 'queued', busy=False, reason='unsent_lease_lost')
                    else:
                        self._state(con, message, 'delivery_unknown', reason='dispatch_lease_lost')

    def recover(self):
        with self._transaction() as con:
            before = con.execute('select coalesce(max(seq),0) from events').fetchone()[0]
            self._recover(con)
            return con.execute('select count(*) from events where seq>?', (before,)).fetchone()[0]

    def claim(self, credential, owner_token, *, lease_seconds=30, message_id=None):
        self._ttl(lease_seconds)
        with self._transaction() as con:
            self._recover(con)
            ep = self._owner(con, credential, owner_token)
            if con.execute('select 1 from messages where target=? and busy=1', (ep['id'],)).fetchone():
                return None
            row = con.execute("select id from messages where target=? and state='queued' order by seq limit 1",
                              (ep['id'],)).fetchone()
            if row is None:
                return None
            if message_id is not None and row['id'] != message_id:
                return None  # A specific caller cannot bypass endpoint FIFO.
            token = secrets.token_urlsafe(24)
            con.execute("""update messages set state='claimed',busy=1,claim_token=?,claim_until=?,
                claim_owner=?,updated_at=? where id=?""",
                        (token, min(ep['owner_until'], self.clock()+lease_seconds), owner_token, self.clock(), row['id']))
            self._event(con, 'claimed', message=row['id'], endpoint=ep['id'], owner=ep['owner'])
            return self._message(con, row['id'])

    def begin_dispatch(self, credential, owner_token, message_id, claim_token):
        """Commit the possible-external-effect barrier BEFORE invoking an adapter."""
        with self._transaction() as con:
            ep = self._owner(con, credential, owner_token)
            message = self._message(con, message_id)
            if (message['target'] != ep['id'] or message['state'] != 'claimed'
                    or message['claim_owner'] != owner_token or message['claim_token'] != claim_token
                    or message['claim_until'] <= self.clock() or not self._valid(con, message)
                    or (message['expires_at'] is not None and message['expires_at'] <= self.clock())):
                raise ConflictError('claim lost, expired or invalidated before send')
            attempt = 'attempt-'+uuid.uuid4().hex
            con.execute('''update messages set attempt_id=?,external_possible=1,endpoint_generation=?
                where id=?''', (attempt, ep['generation'], message_id))
            self._state(con, message, 'dispatching')
            return self._message(con, message_id)

    def finish_dispatch(self, message_id, attempt_id, *, state, external_id=None):
        """Adapter-only receipt using the durable attempt capability, no raw stdout."""
        if state not in ('delivered', 'delivery_unknown', 'failed'):
            raise MailboxError('invalid delivery outcome')
        if external_id is not None:
            _text(external_id, 'external_id')
        with self._transaction() as con:
            self._recover(con)
            message = self._message(con, message_id)
            if not attempt_id or message['attempt_id'] != attempt_id:
                raise IdentityError('wrong dispatch attempt')
            fingerprint = _hash(_json([message_id, state, external_id]))
            if not self._receipt(con, message, 'dispatch:'+attempt_id, 'delivery', fingerprint):
                return self._message(con, message_id)
            # Keep native receipt identity even when acceptance or cancellation
            # won the race; recording evidence must not regress message state.
            con.execute('update messages set external_id=? where id=?', (external_id, message_id))
            ep = self._endpoint(con, message['target'])
            current = (message['protocol'] == 1 or (ep['generation'] == message['endpoint_generation']
                and ep['owner_token'] == message['claim_owner'] and ep['owner_until'] > self.clock()
                and message['claim_until'] > self.clock()))
            if not current:
                self._event(con, 'fenced_dispatch_receipt', message=message_id, attempt_id=attempt_id)
            if current and message['state'] in ('dispatching', 'delivery_unknown'):
                if state == 'failed':
                    con.execute('update messages set external_possible=0 where id=?', (message_id,))
                self._state(con, message, state, busy=state != 'failed')
            return self._message(con, message_id)

    def queued_natively(self, credential, message_id):
        """A code-owned adapter capability delegates native turn serialization.

        Release the enqueue slot after the queue subprocess is reaped. An
        unknown receipt still forbids replay of this envelope; distinct enqueue
        operations serialize in the native queue. This never asserts receiver
        acceptance or model completion.
        Stream/RPC adapters retain their slot until a correlated turn finishes.
        """
        with self._transaction() as con:
            ep = self._authenticate(con, credential)
            message = self._message(con, message_id)
            if message['target'] != ep['id']:
                raise IdentityError('native queue release belongs to a different endpoint')
            import agents
            import agent_registry
            try:
                agent_registry.get(ep['kind'], 'native_queue_serialization')
            except ValueError:
                raise ConflictError('adapter has no verified native queue serialization') from None
            if message['state'] not in ('delivered', 'delivery_unknown'):
                raise ConflictError('finished native enqueue attempt required')
            if message['protocol'] == 2 and (message['endpoint_generation'] != ep['generation']
                    or message['claim_owner'] != ep['owner_token'] or ep['owner_until'] <= self.clock()):
                raise ConflictError('native queue release ownership fenced')
            if not message['busy']:
                return  # Repeated release is not a second native receipt.
            con.execute('update messages set busy=0 where id=?', (message_id,))
            self._event(con, 'native_queue_slot_released', message=message_id, endpoint=ep['id'],
                        receipt_known=message['state'] == 'delivered')

    def project_reply(self, credential, message_id):
        """A trusted workflow host received a reply for its controller projection.

        It remains readable/correlated without scheduling a second model turn.
        This records host receipt, not model acceptance or workflow completion.
        """
        with self._transaction() as con:
            message = self._receiver(con, credential, message_id)
            if not message['reply_to']:
                raise ConflictError('only correlated replies can be projected')
            if message['state'] == 'queued':
                self._state(con, message, 'completed', busy=False, reason='workflow_host_projection')
            return self._message(con, message_id)

    def _receipt(self, con, message, receipt_id, kind, fingerprint, *, body=None):
        _text(receipt_id, 'receipt_id')
        ep = message['target']
        old = con.execute('select fingerprint from receipts where endpoint_id=? and receipt_id=?',
                          (ep, receipt_id)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise ConflictError('conflicting duplicate receipt')
            return False
        late = message['state'] in TERMINAL
        con.execute('insert into receipts values(?,?,?,?,?,?,?)',
                    (ep, receipt_id, fingerprint, message['id'], kind, int(late), self.clock()))
        if body is not None:
            if not isinstance(body, str) or len(body.encode()) > 1024*1024:
                raise MailboxError('receipt body must be text of at most 1 MiB')
            con.execute('insert into receipt_bodies values(?,?,?)', (ep, receipt_id, body))
        self._event(con, 'late_receipt' if late else 'receipt', message=message['id'], endpoint=ep,
                    receipt_id=receipt_id, receipt_kind=kind)
        return True

    def _receiver(self, con, credential, identifier):
        ep = self._authenticate(con, credential)
        message = self._message(con, identifier)
        if message['target'] != ep['id']:
            raise IdentityError('receipt belongs to a different endpoint')
        if message['protocol'] != 1:
            raise IdentityError('v2 message requires a fully correlated receiver event')
        return message

    def receive_correlated(self, credential, *, message_id, receipt_id, kind, source,
                           project, session_id, correlation_id, generation, route_generation,
                           endpoint_generation, attempt_id, turn_id, text=''):
        """Apply one authenticated native event atomically with every fence.

        Receiver events are transport evidence only. They cannot approve a user
        decision or settle workflow criteria. Old owner evidence remains in the
        audit log and cannot free a quarantined execution slot.
        """
        if kind not in ('accepted', 'responded', 'reply', 'completed', 'turn_completed'):
            raise MailboxError('unsupported correlated receiver event')
        _text(source, 'source')
        _text(turn_id, 'turn_id')
        with self._transaction() as con:
            self._recover(con)
            ep = self._authenticate(con, credential)
            message = self._message(con, message_id)
            if (message['protocol'] != 2 or message['target'] != ep['id']
                    or ep['session_id'] != session_id or ep['project'] != project
                    or message['correlation_id'] != correlation_id
                    or message['generation'] != _json(generation)
                    or type(route_generation) is not int or message['route_generation'] != route_generation
                    or type(endpoint_generation) is not int or message['endpoint_generation'] != endpoint_generation
                    or not attempt_id or message['attempt_id'] != attempt_id
                    or message['turn_id'] is not None and message['turn_id'] != turn_id):
                raise IdentityError('receiver event identity, turn or generation mismatch')
            fingerprint = _hash(_json([message_id, kind, source, project, session_id,
                correlation_id, generation, route_generation, endpoint_generation, attempt_id, turn_id, text]))
            # Fenced receipts still have stable deduplication and safe audit bodies.
            fresh = self._receipt(con, message, receipt_id, kind, fingerprint, body=text)
            if not fresh:
                return self._message(con, message_id)
            self._event(con, 'native_event', message=message_id, endpoint=ep['id'],
                source=source, session_id=session_id, project=project, correlation_id=correlation_id,
                generation=generation, route_generation=route_generation,
                endpoint_generation=endpoint_generation, attempt_id=attempt_id, turn_id=turn_id,
                event_kind=kind, receipt_id=receipt_id)
            if (message['state'] in TERMINAL or not self._valid(con, message)
                    or ep['generation'] != endpoint_generation or not ep['owner_token']
                    or ep['owner_token'] != message['claim_owner'] or ep['owner_until'] <= self.clock()):
                self._event(con, 'fenced_receiver_event', message=message_id, receipt_id=receipt_id)
                con.execute('update receipts set late=1 where endpoint_id=? and receipt_id=?',
                            (ep['id'], receipt_id))
                return self._message(con, message_id)
            if kind == 'accepted':
                if message['state'] not in ('dispatching', 'delivered', 'delivery_unknown', 'accepted', 'responded'):
                    raise ConflictError('acceptance requires dispatched message')
                if message['turn_id'] is None:
                    con.execute('update messages set turn_id=?,accepted_at=? where id=?',
                                (turn_id, self.clock(), message_id))
                if message['state'] != 'responded':
                    self._state(con, message, 'accepted')
            else:
                if message['state'] not in ('accepted', 'responded') or message['turn_id'] is None:
                    raise ConflictError('event requires acceptance of the exact native turn')
                done = kind in ('completed', 'turn_completed')
                self._state(con, message, 'turn_completed' if done else 'responded', busy=not done)
            return self._message(con, message_id)

    def execution_lease(self, resource):
        with self._transaction() as con:
            row = con.execute('select * from execution_leases where resource=?', (resource,)).fetchone()
            return {k: v for k, v in dict(row).items() if k != 'token_hash'} if row else None

    def acquire_execution(self, resource, *, project, task_id, contract_hash, generation,
                          role, owner, source, lease_seconds=30):
        """Host-only lease; caller must hold the existing repository flock.

        An expired lease alone never permits running past a held repository
        lock. Tokens and increasing fences exclude old renew/release requests.
        A session host owns transport lifecycle only, never workflow approval.
        """
        for value, label in ((resource, 'resource'), (owner, 'owner'), (source, 'source'),
                             (task_id, 'task_id'), (contract_hash, 'contract_hash')):
            _text(value, label)
        if role not in ('origin', 'worker', 'session_host') or generation is None:
            raise MailboxError('explicit execution role and generation required')
        self._ttl(lease_seconds)
        project = str(Path(project).resolve(strict=True))
        token = secrets.token_urlsafe(32)
        with self._transaction() as con:
            old = con.execute('select * from execution_leases where resource=?', (resource,)).fetchone()
            if old and old['state'] == 'active' and old['lease_until'] > self.clock():
                raise ConflictError('execution resource is already owned')
            if old and (old['project'] != project or (old['task_id'] == task_id
                    and (old['contract_hash'] != contract_hash or old['generation'] != _json(generation)
                         and not _newer_generation(generation, json.loads(old['generation']))))):
                raise ConflictError('execution project, contract or generation cannot be replaced by stale work')
            fence = old['fence']+1 if old else 1
            con.execute('''insert into execution_leases values(?,?,?,?,?,?,?,?,?,?,?,?)
                on conflict(resource) do update set project=excluded.project,task_id=excluded.task_id,
                contract_hash=excluded.contract_hash,generation=excluded.generation,role=excluded.role,
                owner=excluded.owner,token_hash=excluded.token_hash,fence=excluded.fence,
                lease_until=excluded.lease_until,state=excluded.state,source=excluded.source''',
                (resource, project, task_id, contract_hash, _json(generation), role, owner,
                 _hash(token), fence, self.clock()+lease_seconds, 'active', source))
            self._event(con, 'execution_acquired', resource=resource, role=role, owner=owner, fence=fence)
        return token, fence

    def renew_execution(self, resource, token, fence, *, generation, lease_seconds=30):
        self._ttl(lease_seconds)
        with self._transaction() as con:
            self._execution_owner(con, resource, token, fence, generation)
            con.execute('update execution_leases set lease_until=? where resource=?',
                        (self.clock()+lease_seconds, resource))

    def advance_execution(self, resource, token, fence, *, generation, next_generation):
        """Fence a foreground owner's previous generation under the same flock.

        This changes no task counters or grants. Old renew/release calls cannot
        settle the newly started execution, even when they hold the old token.
        """
        if not _newer_generation(next_generation, generation):
            raise ConflictError('execution generation must advance monotonically')
        with self._transaction() as con:
            self._execution_owner(con, resource, token, fence, generation)
            con.execute('update execution_leases set generation=?,fence=? where resource=?',
                        (_json(next_generation), fence+1, resource))
            self._event(con, 'execution_advanced', resource=resource, fence=fence+1,
                        generation=next_generation)
        return fence+1

    def _execution_owner(self, con, resource, token, fence, generation):
        row = con.execute('select * from execution_leases where resource=?', (resource,)).fetchone()
        if (not row or row['state'] != 'active' or row['fence'] != fence
                or row['generation'] != _json(generation) or row['lease_until'] <= self.clock()
                or not isinstance(token, str) or not hmac.compare_digest(row['token_hash'], _hash(token))):
            raise ConflictError('execution lease lost or fenced')

    def release_execution(self, resource, token, fence, *, generation):
        with self._transaction() as con:
            self._execution_owner(con, resource, token, fence, generation)
            con.execute("update execution_leases set state='released',token_hash=null where resource=?", (resource,))
            self._event(con, 'execution_released', resource=resource, fence=fence)

    def accept(self, credential, message_id, *, receipt_id):
        with self._transaction() as con:
            self._recover(con)
            message = self._receiver(con, credential, message_id)
            # Native acceptance is independent of transport-delivery confirmation.
            if message['state'] not in TERMINAL and message['state'] not in ('dispatching', 'delivered', 'delivery_unknown', 'accepted'):
                raise ConflictError('message has not been dispatched')
            if self._receipt(con, message, receipt_id, 'accepted', _hash(_json([message_id, 'accepted']))):
                if message['state'] not in TERMINAL and message['state'] != 'accepted':
                    con.execute('update messages set accepted_at=? where id=?', (self.clock(), message_id))
                    self._state(con, message, 'accepted')
            return self._message(con, message_id)

    def reply(self, credential, message_id, *, receipt_id, body, kind='reply', expires_at=None):
        """Atomically finish the received turn and enqueue its correlated reply."""
        with self._transaction() as con:
            self._recover(con)
            message = self._receiver(con, credential, message_id)
            fingerprint = _hash(_json([message_id, kind, body, expires_at]))
            fresh = self._receipt(con, message, receipt_id, 'reply', fingerprint, body=body)
            if not fresh:
                return self._message(con, message['reply_id']) if message['reply_id'] else None
            if message['state'] in TERMINAL:
                return None  # Late reply is auditable but cannot revive cancelled/stale work.
            if message['state'] != 'accepted':
                raise ConflictError('reply requires explicit receiver acceptance')
            response = self._enqueue(con, credential, binding_id=message['binding_id'], target=message['sender'],
                kind=kind, body=body, idempotency_key='reply:'+message_id, task_id=message['task_id'],
                contract_hash=message['contract_hash'], correlation_id=message['correlation_id'],
                reply_to=message_id, expires_at=expires_at)
            con.execute('update messages set reply_id=? where id=?', (response['id'], message_id))
            self._state(con, message, 'replied', busy=False, reply_id=response['id'])
            return response

    def complete(self, credential, message_id, *, receipt_id):
        """Receiver finished this message without a reply; not workflow completion."""
        with self._transaction() as con:
            self._recover(con)
            message = self._receiver(con, credential, message_id)
            if message['state'] not in TERMINAL and message['state'] != 'accepted':
                raise ConflictError('completion requires receiver acceptance')
            if self._receipt(con, message, receipt_id, 'completed', _hash(_json([message_id, 'completed']))):
                if message['state'] not in TERMINAL:
                    self._state(con, message, 'completed', busy=False)
            return self._message(con, message_id)

    def cancel(self, credential, message_id, *, source):
        _text(source, 'source')
        with self._transaction() as con:
            ep = self._authenticate(con, credential)
            message = self._message(con, message_id)
            if ep['id'] != message['sender']:
                raise IdentityError('only the sender can cancel its message')
            if message['state'] not in TERMINAL:
                self._state(con, message, 'cancelled', busy=message['busy'] and message['external_possible'], source=source)
            return self._message(con, message_id)

    def reconcile(self, credential, message_id, *, source, outcome):
        """Explicit native evidence releases a quarantined slot; NEVER resends.

        The receiving adapter/host must verify the native turn is no longer
        running. Unknown delivery alone is not such evidence.
        """
        _text(source, 'source')
        if outcome not in ('not_running', 'finished'):
            raise MailboxError('reconciliation requires native evidence of no running turn')
        with self._transaction() as con:
            message = self._receiver(con, credential, message_id)
            if message['state'] not in TERMINAL | {'delivery_unknown'}:
                raise ConflictError('message is not awaiting reconciliation')
            if message['state'] == 'delivery_unknown':
                self._state(con, message, 'completed' if outcome == 'finished' else 'failed', busy=False,
                            source=source, outcome=outcome)
            else:
                con.execute('update messages set busy=0,updated_at=? where id=?', (self.clock(), message_id))
            self._event(con, 'reconciled', message=message_id, source=source, outcome=outcome)
            return self._message(con, message_id)

    def reconcile_correlated(self, credential, owner_token, message_id, *, source, outcome,
                             generation, endpoint_generation):
        """Current trusted host records verified native absence, without replay.

        This is not a model event. The host must independently check its owned
        native lifecycle, STOP and workflow authority before calling this API.
        Even after reconciliation the original envelope is never queued again.
        """
        _text(source, 'source')
        if outcome not in ('not_running', 'finished'):
            raise MailboxError('native no-running-turn evidence required')
        with self._transaction() as con:
            self._recover(con)
            ep = self._owner(con, credential, owner_token)
            message = self._message(con, message_id)
            binding = self._binding(con, message['binding_id'])
            if (message['protocol'] != 2 or message['target'] != ep['id']
                    or ep['generation'] != endpoint_generation or type(endpoint_generation) is not int
                    or binding['generation'] != _json(generation)):
                raise IdentityError('reconciliation endpoint or execution generation mismatch')
            if message['state'] not in TERMINAL | {'delivery_unknown'}:
                if (message['state'] not in ('delivered', 'accepted', 'responded')
                        or message['claim_owner'] == owner_token):
                    raise ConflictError('message is not quarantined from a lost owner')
            if message['state'] not in TERMINAL:
                self._state(con, message, 'turn_completed' if outcome == 'finished' else 'failed',
                            busy=False, source=source, outcome=outcome)
            else:
                con.execute('update messages set busy=0 where id=?', (message_id,))
            self._event(con, 'native_reconciled', message=message_id, endpoint=ep['id'], source=source, outcome=outcome)
            return self._message(con, message_id)

    def events(self, *, after=0, message_id=None):
        with self._transaction() as con:
            rows = con.execute('''select * from events where seq>? and (? is null or message_id=?)
                order by seq''', (after, message_id, message_id)).fetchall()
            return [dict(row, data=json.loads(row['data'])) for row in rows]

    def receipts(self, message_id):
        """Read acknowledgement evidence, including quarantined late reply text."""
        with self._transaction() as con:
            rows = con.execute('''select r.*, b.body from receipts r left join receipt_bodies b
                on r.endpoint_id=b.endpoint_id and r.receipt_id=b.receipt_id
                where r.message_id=? order by r.at,r.receipt_id''', (message_id,)).fetchall()
            return [dict(row) for row in rows]
