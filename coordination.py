"""Private host-handoff material; a short wake-up is never new authority."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys

import interaction
import recovery
import ato_core as core
from ato_config import reader_command

MARKER = 'host-handoff:'
MAX_BYTES = 2 * 1024 * 1024


def identifier(directory, state):
    return 'host-handoff-' + core.digest([str(directory), recovery.generation(directory, state), state.get('host_handoff')])[:20]


def validate(directory, state, contract, expected):
    if ((directory/'STOP').exists() or (directory/'user-closure.json').exists()
            or state.get('status') != 'needs_host' or not contract.get('host_handoff')
            or state.get('contract_hash') != core.digest(contract)
            or identifier(directory, state) != expected):
        raise ValueError('host handoff is no longer current')


def is_handoff(message):
    return message.get('kind') == 'host_handoff' or str(message.get('canonical_id', '')).startswith(MARKER)


def validate_envelope(directory, state, contract, message):
    if not is_handoff(message):
        return
    key = message.get('canonical_id', '')
    if not key.startswith(MARKER):
        raise ValueError('host handoff has no canonical request')
    validate(directory, state, contract, key[len(MARKER):])


def payload_directory():
    path = interaction.directory()/'control'/'handoff-material'
    if path.is_symlink():
        raise ValueError('handoff material cannot use a symlink')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('handoff material must be private')
    return path


def prepare(directory, state, contract, target, key, material):
    expected = key[len(MARKER):] if key.startswith(MARKER) else ''
    validate(directory, state, contract, expected)
    if not isinstance(material, str) or len(material.encode()) > MAX_BYTES:
        raise ValueError('handoff material bound exceeded')
    notice = (f'[ctask-host-handoff id={expected}]\n'
        f'{reader_command()} {expected}\n'
        'Stale:silent; else:prior authority; no recap.')
    if len(notice.encode('utf-8')) >= 260:
        raise ValueError('handoff reader path exceeds compact notice byte limit; configure a shorter trusted path')
    path = payload_directory()/(expected+'.json')
    record = dict(version=1, id=expected, workflow=str(directory), target=target,
        contract_hash=state['contract_hash'], generation=recovery.generation(directory, state),
        material=material, material_sha256=hashlib.sha256(material.encode()).hexdigest())
    with recovery.locked(path.with_suffix('.lock')):
        if path.exists():
            if path.is_symlink() or path.stat().st_mode & 0o077 or core.read(path) != record:
                raise ValueError('immutable handoff material changed')
        else:
            from ato_core import private_save
            private_save(path, record)
    # Retain the exact correlation marker, not task/authorization/host prose.
    return notice


def read(identifier_value):
    if re.fullmatch(r'failure-[0-9a-f]{20}', identifier_value):
        import failure_notice
        return failure_notice.read(identifier_value)
    if not re.fullmatch(r'host-handoff-[0-9a-f]{20}', identifier_value):
        raise ValueError('invalid handoff identifier')
    path = payload_directory()/(identifier_value+'.json')
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES + 16384
            or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise ValueError('invalid private handoff material')
    value = core.read(path)
    if (value.get('version') != 1 or value.get('id') != identifier_value
            or not isinstance(value.get('material'), str)
            or hashlib.sha256(value['material'].encode()).hexdigest() != value.get('material_sha256')):
        raise ValueError('handoff material integrity mismatch')
    interaction.actor(value['target'])
    directory, state, contract, origin = interaction.binding(value['workflow'], require_open=False)
    if (origin != value['target'] or state['contract_hash'] != value['contract_hash']
            or recovery.generation(directory, state) != value['generation']):
        raise ValueError('handoff origin or execution changed')
    validate(directory, state, contract, identifier_value)
    return dict(valid=True, authority='existing frozen contract only; read is not acceptance or approval',
        workflow=str(directory), material=value['material'])


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError('one handoff identifier required')
        result = read(sys.argv[1])
    except (ValueError, OSError, KeyError, TypeError):
        # A late notice must not start another user-facing diagnostic turn.
        result = dict(valid=False, action='end_silently', reason='stale_or_unavailable')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
