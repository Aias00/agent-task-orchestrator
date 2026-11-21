"""Trusted Codex exec exact-ID worker protocol; never imports a native chat.

CLI processes restart for each turn. Only the task's verified native context
is reused. Readback is confined to the new task-owned rollout and exact index
lookup; no historical transcript search and no authentication reads occur.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat

import codex_session_adapter as native
import profile_evidence

CAPABILITIES = dict(version=1, native_continuation='supported',
    persistent_process='unsupported', checkpoint_request='unsupported',
    qualification='code-owned strict protocol; installed CLI not yet origin-qualified')
MAX_BYTES = 64 * 1024 * 1024


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json_hash(value):
    return _hash(json.dumps(value, sort_keys=True).encode())


def _file(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('worker profile/native file cannot be a symlink')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError('worker profile/native file must be bounded regular data')
        data = stream.read(MAX_BYTES+1)
        if len(data) > MAX_BYTES:
            raise ValueError('worker profile/native file too large')
    return data, info


# The installed queue gate is a code-owned launcher, never a runtime override
# from a plan. Resolve its immutable native payload rather than pinning only
# the Python launcher while its `current` package link changes underneath it.
TRUSTED_QUEUE_GATE = '71540a5026b32539eca6bd66164f7104525cc061e6733d45903aba901f365a56'


def _binary(candidate):
    launcher = Path(candidate).absolute()
    data = launcher.read_bytes()
    provenance = dict(path=str(launcher), sha256=_hash(data))
    if _hash(data) == TRUSTED_QUEUE_GATE:
        tree = ast.parse(data.decode())
        choices = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'NATIVE' for t in node.targets)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
        if len(choices) != 1 or not Path(choices[0]).is_absolute():
            raise ValueError('trusted Codex launcher native payload is unknown')
        launcher = Path(choices[0]).resolve(strict=True)
    else:
        launcher = launcher.resolve(strict=True)
    return str(launcher), _hash(launcher.read_bytes()), provenance


def profile(plan, repo):
    import runtime_selection
    binary = shutil.which(os.environ.get('CTASK_CODEX_BIN', 'codex'))
    if not binary:
        raise ValueError('Codex executable unavailable')
    binary, binary_sha256, launcher = _binary(binary)
    # Hash configuration bytes only; neither content nor auth is recorded. No
    # auth.json/SQLite history/config is copied or modified by this adapter.
    configs = {}
    for folder in (native.home(), Path(repo)/'.codex'):
        for path in [folder/'config.toml', *sorted(folder.glob('*.config.toml'))]:
            configs[str(path)] = _hash(_file(path)[0]) if path.exists() else None
    env = {name: _hash(os.environ[name].encode()) if name in os.environ else None
           for name in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ORG_ID',
                        'OPENAI_PROJECT_ID', 'CODEX_PROFILE', 'CODEX_CONFIG_PROFILE',
                        'CODEX_MANAGED_CONFIG_REQUIREMENTS_FILE', 'CODEX_HOME', 'HOME')}
    # Native config remains native. Unobserved runtime capabilities are not
    # promoted to success; effective model/policy is checked after each turn.
    return dict(adapter='codex', binary=binary, binary_sha256=binary_sha256, launcher=launcher,
        native_home=str(native.home()), configs=configs, environment_hash=_json_hash(env),
        model=plan.get('agent', {}).get('model') or plan.get('model'),
        sandbox=plan.get('sandbox', 'workspace-write'), approval='never',
        permissions=plan.get('permissions', {}),
        source_manifest_sha256=_json_hash(runtime_selection._sources(runtime_selection.ROOT)))


def command(plan, schema, output, value):
    import permissions
    cfg = value['profile']
    argv = [cfg['binary'], 'exec', '--sandbox', cfg['sandbox'],
            '-c', 'approval_policy="never"', *permissions.codex_args(plan)]
    if cfg['sandbox'] == 'workspace-write':
        grant = plan.get('permissions', {})
        if 'network_access' not in grant:
            argv += ['-c', 'sandbox_workspace_write.network_access=false']
        if 'writable_roots' not in grant:
            argv += ['-c', 'sandbox_workspace_write.writable_roots=[]']
    if cfg['model']:
        argv += ['--model', cfg['model']]
    if value['native_id']:
        # Options before resume pin the same sandbox even when the subcommand
        # itself doesn't expose --sandbox. UUID is never supplied by a model.
        argv += ['resume']
    else:
        argv += ['--thread-source', value['source']]
    argv += ['--json', '--output-schema', str(schema), '-o', str(output)]
    if value['native_id']:
        argv += [native.identity(value['native_id'])]
    return argv + ['-']


def _metadata(path, native_id, project, source):
    if not path.resolve(strict=True).is_relative_to(native.home()):
        raise ValueError('task-owned native rollout escaped configured home')
    if not Path(path).is_relative_to(native.home()):
        raise ValueError('native worker rollout outside its configured native home')
    data, info = _file(path)
    if not data.endswith(b'\n'):
        raise ValueError('partial native rollout')
    first = json.loads(data.split(b'\n', 1)[0])
    meta = first.get('payload', {})
    actual_source = meta.get('thread_source', meta.get('source'))
    if isinstance(actual_source, dict) and set(actual_source) == {'custom'}:
        actual_source = actual_source['custom']
    if (first.get('type') != 'session_meta' or meta.get('id') != native_id
            or meta.get('cwd') != str(project) or actual_source != source):
        raise ValueError('native worker source/project/session mismatch')
    return data, dict(device=info.st_dev, inode=info.st_ino)


def verify(value, repo):
    identifier = native.identity(value['native_id'])
    path = Path(native.session_path(native.home(), identifier))
    if str(path) != value['rollout_path']:
        raise ValueError('native worker exact index path changed')
    data, file_id = _metadata(path, identifier, repo, value['source'])
    if (file_id != value['file_identity'] or len(data) != value['offset']
            or _hash(data) != value['rollout_sha256']):
        raise ValueError('native worker changed outside its owned turn')


def _records(data):
    rows = []
    for line in data.splitlines():
        if len(line) > native.MAX_LINE:
            raise ValueError('oversized native worker record')
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError('invalid native worker record')
        rows.append(row)
    return rows


def receipt(value, log, repo, correlation, generation):
    from agent_registry import SessionIdentity, SessionEvent
    from transport import DeliveryResult
    import time
    rows = _records(_file(log)[0])
    starts = [r for r in rows if r.get('type') == 'thread.started']
    if len(starts) != 1:
        raise ValueError('native worker needs one exact thread receipt')
    identifier = native.identity(starts[0].get('thread_id'))
    if identifier != starts[0]['thread_id'] or value['native_id'] not in (None, identifier):
        raise ValueError('native worker resumed wrong ID')
    if (len([r for r in rows if r.get('type') == 'turn.started']) != 1
            or len([r for r in rows if r.get('type') == 'turn.completed']) != 1
            or any(r.get('type') in ('error', 'turn.failed') for r in rows)):
        raise ValueError('native worker lacks unique successful terminal lifecycle')
    path = Path(native.session_path(native.home(), identifier))
    data, file_id = _metadata(path, identifier, repo, value['source'])
    offset = value.get('offset', 0)
    if value['native_id']:
        if (str(path) != value['rollout_path'] or file_id != value['file_identity']
                or _hash(data[:offset]) != value['rollout_sha256']):
            raise ValueError('native worker rollout was replaced or prefix changed')
    events = _records(data[offset:])
    turn = None
    context = None
    seen_message = False
    ended = False
    for event in events:
        payload = event.get('payload', {})
        if not isinstance(payload, dict):
            raise ValueError('invalid native worker event payload')
        kind = payload.get('type')
        if event.get('type') == 'event_msg' and kind == 'task_started':
            if turn is not None:
                raise ValueError('multiple or unrelated native worker turns')
            turn = payload.get('turn_id')
            if not isinstance(turn, str) or not turn or turn in [r['turn_id'] for r in value['turns']]:
                raise ValueError('missing/replayed native worker turn')
        if event.get('type') == 'turn_context':
            if context is not None or payload.get('turn_id') != turn:
                raise ValueError('native worker turn context mismatch')
            context = {key: payload.get(key) for key in ('cwd', 'approval_policy', 'sandbox_policy',
                        'model', 'model_provider', 'permission_profile')}
        if ((event.get('type') == 'event_msg' and kind == 'user_message'
                and native._correlates(correlation, payload.get('message', '')))
                or (event.get('type') == 'response_item' and payload.get('role') == 'user'
                    and native._correlates(correlation, native._text(payload)))):
            if turn is None or ended:
                raise ValueError('native worker message outside owned turn')
            seen_message = True
        if event.get('type') == 'event_msg' and kind == 'task_complete':
            if not turn or payload.get('turn_id') != turn or not seen_message or ended:
                raise ValueError('native worker message/turn completion mismatch')
            ended = True
    if not ended or context is None or not seen_message:
        raise ValueError('native worker correlation/effective context unverified')
    policy = value['profile']
    sandbox = context['sandbox_policy']
    if (context['cwd'] != str(repo) or context['approval_policy'] != 'never'
            or not isinstance(sandbox, dict) or sandbox.get('type') != policy['sandbox']
            or not isinstance(context['model'], str) or not context['model']
            or policy['model'] and context['model'] != policy['model']):
        raise profile_evidence.ProfileDriftError('native worker effective model/permissions mismatch',
            dict(context, cwd=str(repo), approval_policy='never',
                 sandbox_policy=dict(sandbox if isinstance(sandbox, dict) else {}, type=policy['sandbox']),
                 model=policy['model'] or dict(required='nonempty_string')), context)
    if policy['sandbox'] == 'workspace-write':
        grant = policy['permissions']
        if sandbox.get('network_access', False) != grant.get('network_access', False):
            raise profile_evidence.ProfileDriftError('native worker effective network grant mismatch',
                dict(sandbox_policy=dict(network_access=grant.get('network_access', False))),
                dict(sandbox_policy=dict(network_access=sandbox.get('network_access', False))))
        roots = sandbox.get('writable_roots', [])
        if set(roots) != set(grant.get('writable_roots', [])):
            raise profile_evidence.ProfileDriftError('native worker effective writable roots mismatch',
                dict(sandbox_policy=dict(writable_roots=grant.get('writable_roots', []))),
                dict(sandbox_policy=dict(writable_roots=roots)))
    if value.get('effective_context') is not None and context != value['effective_context']:
        raise profile_evidence.ProfileDriftError('native worker effective profile changed on resume',
                                               value['effective_context'], context)
    identity = SessionIdentity('codex', identifier, str(repo), value['source'])
    event = SessionEvent('native-'+correlation, identity, correlation, generation,
        'turn_completed', 'task-owned Codex exec stream and exact native append readback',
        time.time(), turn_id=turn, message_id=correlation)
    delivered = DeliveryResult('delivered', correlation, 'exact task-owned native turn completed')
    return dict(native_id=identifier, turn_id=turn, rollout_path=str(path), offset=len(data),
        rollout_sha256=_hash(data), file_identity=file_id, effective_context=context,
        native_event=event.to_mapping(), delivery=dict(state=delivered.state, message_id=delivered.external_id))
