"""Controller ledger storage only; no delivery, policy, actors or business execution."""
import contextlib
import json
import sqlite3

import ato_config
import ato_core as core

TERMINAL = {'resolved', 'stale', 'stopped', 'completed', 'superseded'}


def directory():
    p = ato_config.ensure_interaction_directory()/'control'
    p.mkdir(exist_ok=True)
    return p


@contextlib.contextmanager
def database(root=None):
    con = sqlite3.connect((directory() if root is None else root)/'state.sqlite', timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('pragma journal_mode=WAL')
    con.execute('pragma synchronous=FULL')
    con.executescript('''
      create table if not exists actions(id text primary key, body text not null);
      create table if not exists events(seq integer primary key autoincrement,
        key text unique not null, at real not null, action_id text, kind text not null, body text not null);
      create table if not exists outbox(key text primary key, body text not null);
      create table if not exists workflows(path text primary key, body text not null);
    ''')
    try:
        yield con
    finally:
        con.close()


def event(con, key, identifier, kind, body, *, clock=core.now):
    con.execute('insert or ignore into events(key,at,action_id,kind,body) values(?,?,?,?,?)',
                (key, clock(), identifier, kind, json.dumps(body, ensure_ascii=False)))


def put(con, action, kind=None, *, clock=core.now):
    old = con.execute('select body from actions where id=?', (action['id'],)).fetchone()
    if old and json.loads(old[0]) == action:
        return
    action['updated_at'] = clock()
    con.execute('insert into actions values(?,?) on conflict(id) do update set body=excluded.body',
                (action['id'], json.dumps(action, ensure_ascii=False)))
    event(con, core.digest([action['id'], action]), action['id'], kind or 'action_updated', action, clock=clock)


def settings():
    p = directory()/'config.json'
    return core.read(p) if p.exists() else None
