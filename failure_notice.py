"""Compact failure wakeups. Reading evidence never grants recovery authority."""
import hashlib
import json
import os
from pathlib import Path
import re

import interaction
import recovery
import ato_core as core
import control_store
from ato_config import reader_command as default_reader_command

MAX_BYTES = 2 * 1024 * 1024


class StaleFailure(ValueError):
    pass


def action_for(identifier):
    if not re.fullmatch(r'failure-[0-9a-f]{20}', identifier):
        raise ValueError('invalid failure identifier')
    with control_store.database() as con:
        found = con.execute('select body from actions where id=?', (identifier,)).fetchone()
    if not found:
        raise ValueError('unknown failure notice')
    return json.loads(found[0])


def validate(action):
    import failure_attention
    if (action.get('state') in control_store.TERMINAL or action.get('presented_at')
            or not failure_attention.current(action)):
        raise StaleFailure('failure notice is no longer current')


def payload_directory():
    path = control_store.directory()/'failure-material'
    if path.is_symlink():
        raise ValueError('failure material cannot use a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('failure material must be private')
    return path


def prepare(key, target, workflow, material, *, reader_command=None):
    match = re.fullmatch(r'followup:(failure-[0-9a-f]{20}):0', key)
    if not match:
        raise ValueError('failure notices are once-only')
    identifier = match[1]
    action = action_for(identifier)
    validate(action)
    if target != action['hub'] or str(workflow) != action['workflow']:
        raise ValueError('failure notice route mismatch')
    if not isinstance(material, str) or len(material.encode()) > MAX_BYTES:
        raise ValueError('failure material bound exceeded')
    record = dict(version=1, id=identifier, target=target, workflow=action['workflow'],
                  generation=action['baseline_generation'], failure_evidence=action['failure_evidence'],
                  material=material, material_sha256=hashlib.sha256(material.encode()).hexdigest())
    path = payload_directory()/(identifier+'.json')
    with recovery.locked(path.with_suffix('.lock')):
        if path.exists():
            if path.is_symlink() or path.stat().st_mode & 0o077 or core.read(path) != record:
                raise ValueError('immutable failure material changed')
        else:
            core.private_save(path, record)
    # The controller supplies coordination.reader_command; direct callers use
    # that same configuration primitive, re-exported by coordination.
    command = (default_reader_command if reader_command is None else reader_command)()
    notice = (f'[ctask-followup id={identifier} reminder=0]\n'
              f'{command} {identifier}\n'
              'Stale/handled:silent; current:one brief notice; no new authority.')
    if len(notice.encode()) >= 300:
        raise ValueError('failure reader path exceeds compact notice bound')
    return notice


def read(identifier):
    action = action_for(identifier)
    interaction.actor(action['hub'])
    validate(action)
    path = payload_directory()/(identifier+'.json')
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES + 16384
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise ValueError('invalid private failure material')
    record = core.read(path)
    if (record.get('version') != 1 or record.get('id') != identifier
            or record.get('target') != action['hub'] or record.get('workflow') != action['workflow']
            or record.get('generation') != action['baseline_generation']
            or record.get('failure_evidence') != action['failure_evidence']
            or not isinstance(record.get('material'), str)
            or hashlib.sha256(record['material'].encode()).hexdigest() != record.get('material_sha256')):
        raise ValueError('failure material integrity or binding mismatch')
    return dict(valid=True, authority='read-only evidence; no approval, recovery, or presentation',
                workflow=record['workflow'], material=record['material'])


def validate_envelope(message):
    key = str(message.get('canonical_id', ''))
    if not key.startswith('followup:failure-'):
        return
    match = re.fullmatch(r'followup:(failure-[0-9a-f]{20}):0', key)
    if not match:
        raise ValueError('invalid failure notice envelope')
    action = action_for(match[1])
    validate(action)
    if message.get('task_id') != action['workflow']:
        raise ValueError('failure envelope workflow mismatch')
