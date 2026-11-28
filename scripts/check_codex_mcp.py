#!/usr/bin/env python3
"""Opt-in native Codex qualification. Never imported by the ordinary test suite.

--run authorizes bounded native model calls (four controller turns, one worker).
--verify-evidence DIR is offline: no model calls or server startup. Evidence is
private operator material, not a signed attestation against a malicious host.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ato import config, executor, process
from ato.store import Store

BASELINE = 'a6cd1ec9eaedd11a88c73464976b4e8f6465c843'
READS = {'ato_repositories', 'ato_list', 'ato_status', 'ato_diff', 'ato_trace', 'ato_inbox'}
REQUIRED = READS | {'ato_validate', 'ato_submit'}
MARKER = 'native-codex-mcp-ok\n'


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('w', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def read(path):
    return json.loads(path.read_text())


def command(argv, **kwargs):
    return subprocess.check_output(argv, timeout=30, stderr=subprocess.PIPE, **kwargs).decode().strip()


def source_identity():
    # Documentation edits do not invalidate execution; all executable sources,
    # dependency declarations and distributed templates do, including this file.
    paths = [*ROOT.glob('ato/**/*.py'), *ROOT.glob('scripts/*.py'),
             *ROOT.glob('skills/ato/templates/*'), ROOT / 'pyproject.toml']
    return {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sorted(paths)}


def client_identity(exe):
    return {'version': command([exe, '--version']), 'sha256': digest(Path(exe).read_bytes())}


def toml_fields(values, prefix=''):
    lines = []
    for key, value in values.items():
        name = prefix + json.dumps(key)
        if isinstance(value, dict):
            lines.extend(toml_fields(value, name + '.'))
        elif value is not None:
            lines.append(name + ' = ' + json.dumps(value))
    return lines


def native_calls(path):
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    require(any(e.get('type') == 'turn.completed' for e in events), 'native turn did not complete: ' + path.name)
    calls = []
    for event in events:
        item = event.get('item', {})
        if event.get('type') == 'item.completed' and item.get('type') == 'mcp_tool_call':
            require(item.get('status') == 'completed' and not item.get('error'), 'native MCP call failed')
            result = item.get('result') or {}
            envelope = result.get('structured_content') or result.get('structuredContent')
            if envelope is None:
                texts = [c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text']
                envelope = next((json.loads(t) for t in texts if t.startswith('{')), None)
            require(envelope and envelope.get('ok') is True, 'native MCP business call failed')
            calls.append({**item, 'envelope': envelope})
    return calls


def synthetic_server(argv):
    # Only this explicitly selected fixture intercepts the native dispatch edge.
    # The real stdio/HTTP server and real worker never load this patch.
    from ato.cli import main
    def queued(store, task_id, run_id, **kwargs):
        store.append_event(task_id, 'synthetic_native_boundary', {'run_id': run_id}, run_id=run_id)
    with patch('ato.executor._spawn_background_run', queued):
        return main(argv)


class Qualification:
    def __init__(self, out):
        self.out = out.resolve()
        require(not (self.out / 'manifest.json').exists(), 'Use a new evidence directory; never overwrite a campaign')
        self.out.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.out, 0o700)
        self.temp = Path(tempfile.mkdtemp(prefix='ato-native-codex-')).resolve()
        self.children = []
        self.credentials = []
        self.ports = []
        self.sessions = []
        self.exe = str(Path(shutil.which('codex')).resolve())
        self.live_home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).resolve()
        self.live_hashes = {name: digest((self.live_home / name).read_bytes())
                            for name in ('config.toml', 'auth.json') if (self.live_home / name).is_file()}
        self.env = {k: os.environ[k] for k in ('PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if k in os.environ}
        self.env.update(HOME=str(self.temp / 'home'), TMPDIR=str(self.temp / 'tmp'),
                        PYTHONPATH=str(ROOT), ATO_CODEX_BIN=self.exe)
        for name in ('home', 'tmp', 'client', 'worker', 'repo', 'controller'):
            (self.temp / name).mkdir(mode=0o700)
        self.tokens = {name: uuid.uuid4().hex + uuid.uuid4().hex for name in ('BOT', 'READER')}
        self.env.update({'ATO_SERVER_TOKEN_' + k: v for k, v in self.tokens.items()})
        self.env.update(ATO_HOME=str(self.temp / 'state'), CODEX_HOME=str(self.temp / 'worker'))
        original = tomllib.loads((self.live_home / 'config.toml').read_text())
        # Preserve the installed default provider/model using native config only;
        # never load other MCPs, hooks, plugins, user instructions or project trust.
        self.provider = {k: original[k] for k in ('model', 'model_provider') if k in original}
        provider = original.get('model_provider')
        if provider:
            selected = dict(original.get('model_providers', {}).get(provider, {}))
            selected.update(request_max_retries=0, stream_max_retries=0)
            self.provider['model_providers'] = {provider: selected}
        self.auth_source = self.live_home / 'auth.json'
        require(self.auth_source.is_file(), 'Native file authentication unavailable; no alternative login attempted')
        def strings(value):
            if isinstance(value, dict):
                return [s for v in value.values() for s in strings(v)]
            if isinstance(value, list):
                return [s for v in value for s in strings(v)]
            return [value.encode()] if isinstance(value, str) and len(value) >= 24 else []
        sensitive_provider = original.get('model_providers', {}).get(provider, {})
        self.secret_bytes = strings(read(self.auth_source)) + strings({
            k: v for k, v in sensitive_provider.items() if 'token' in k or 'header' in k})
        self.secret_bytes += [v.encode() for v in self.tokens.values()]
        self.base = {'project_doc_max_bytes': 0, 'cli_auth_credentials_store': 'file',
                     'features': {'apps': False}, **self.provider}
        self.metadata = {'baseline': BASELINE, 'source': source_identity(), 'client': client_identity(self.exe),
                         'versions': {p: importlib.metadata.version(p) for p in ('mcp', 'mcp-types', 'uvicorn', 'starlette')},
                         'model': original.get('model'), 'auth': 'private native auth.json and selected native provider config',
                         'max_controller_turns': 4, 'controller_deadline_seconds': 240,
                         'approval': 'native --approve-for-me; workspace-write; no bypass', 'worker_timeout_seconds': 120, 'sessions': self.sessions}

    def private_config(self, home, mcp):
        content = '\n'.join(toml_fields(self.base)) + '\n' + mcp
        path = home / 'config.toml'
        path.write_text(content)
        os.chmod(path, 0o600)
        auth = home / 'auth.json'
        shutil.copyfile(self.auth_source, auth)
        os.chmod(auth, 0o600)
        self.credentials.extend([path, auth])

    def init_repo(self, name):
        root = self.temp / name
        for args in (['init', '-q'], ['config', 'user.email', 'fixture@invalid'], ['config', 'user.name', 'Fixture']):
            command(['git', '-C', str(root), *args], env=self.env)
        (root / 'README.md').write_text('Disposable native MCP qualification fixture.\n')
        command(['git', '-C', str(root), 'add', 'README.md'], env=self.env)
        command(['git', '-C', str(root), 'commit', '-qm', 'fixture'], env=self.env)

    def spawn(self, argv, env, name, prompt=None, timeout=None):
        out, err = self.out / (name + '.jsonl'), self.out / (name + '.stderr')
        with out.open('wb') as stdout, err.open('wb') as stderr:
            p = subprocess.Popen(argv, cwd=self.temp / 'controller', env=env,
                                 stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
                                 stdout=stdout, stderr=stderr, start_new_session=True)
        birth = process.group_birth(p.pid)
        require(birth, 'Cannot establish child ownership')
        record = {'pid': p.pid, 'birth': birth, 'argv': argv, 'name': name}
        self.children.append((p, record))
        save(self.out / (name + '.process.json'), record)
        if prompt is not None:
            try:
                p.communicate(prompt.encode(), timeout=timeout)
            except subprocess.TimeoutExpired:
                process.stop_owned_group(p.pid, birth)
                p.wait(timeout=5)
                raise RuntimeError('Native deadline exceeded: ' + name) from None
            require(p.returncode == 0, 'Native client failed; inspect private ' + name + '.stderr')
        return p

    def host_config(self, transport, state='state'):
        template = ROOT / 'skills/ato/templates' / ('server-' + transport + '.example.json')
        cfg = read(template)
        cfg['repositories'] = {'demo': str(self.temp / 'repo')}
        # No human credential exists anywhere in a native controller environment.
        cfg['principals'].pop('operator', None)
        if transport == 'http':
            cfg['principals']['reader'] = {'token_env': 'ATO_SERVER_TOKEN_READER',
                                          'repositories': ['demo'], 'scopes': ['read']}
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            cfg['allowed_hosts'] = ['127.0.0.1:' + str(port)]
            self.ports.append(port)
        else:
            port = None
        path = self.temp / (state + '-' + transport + '.json')
        save(path, cfg)
        save(self.out / path.name, cfg)
        return path, port

    def native(self, name, template, prompt, expected, *, state='state', port=None, host=None):
        home = self.temp / 'client'
        text = (ROOT / 'skills/ato/templates' / ('codex-' + template + '.toml')).read_text()
        if template == 'stdio':
            text = text.replace('/srv/ato/venv/bin/ato', str(ROOT / '.venv/bin/ato'))
            text = text.replace('/srv/ato/server-stdio.json', str(host))
            text = text.replace('/srv/ato/state', str(self.temp / state))
            text = text.replace('/srv/ato/codex', str(self.temp / 'worker'))
        else:
            text = text.replace('https://ato.example.com/mcp', f'http://127.0.0.1:{port}/mcp')
        self.private_config(home, text)
        save(self.out / (name + '.config.json'), {'mcp_servers': tomllib.loads(text)['mcp_servers'],
                                                'isolated': True, 'model': self.provider.get('model')})
        env = {**self.env, 'CODEX_HOME': str(home)}
        env.pop('ATO_SERVER_TOKEN_READER', None)
        argv = [self.exe, 'exec', '--strict-config', '--json', '--ephemeral', '--approve-for-me',
                '--trust', '-C', str(self.temp / 'controller'), '-']
        instruction = ('Authorized isolated MCP qualification. Use only the ato MCP tools. '
                       'Do not run shell commands, read files, inspect credentials, dispatch extra tasks, '
                       'or use other connectors. Perform exactly the following calls in order. '
                       'Every mutation key below is already persisted by the host. '
                       'Stop on any tool failure; do not replace a request key. End with one short sentence.\n')
        save(self.out / (name + '.prompt.json'), {'prompt': instruction + prompt})
        started = time.time()
        self.sessions.append({'name': name, 'transport': template, 'expected': sorted(expected), 'started': started})
        save(self.out / 'progress.json', self.metadata)
        self.spawn(argv, env, name, instruction + prompt, 240)
        calls = native_calls(self.out / (name + '.jsonl'))
        require(expected <= {c['tool'] for c in calls}, 'Missing native tools: ' + name)
        return calls

    def server(self, state, synthetic=False):
        host, port = self.host_config('http', state)
        env = {**self.env, 'ATO_HOME': str(self.temp / state)}
        argv = ([sys.executable, str(Path(__file__).resolve()), '--synthetic-server'] if synthetic
                else [str(ROOT / '.venv/bin/ato')])
        argv += ['serve', '--config', str(host), '--host', '127.0.0.1', '--port', str(port)]
        p = self.spawn(argv, env, state + '-server')
        until = time.monotonic() + 15
        while time.monotonic() < until:
            with socket.socket() as sock:
                if sock.connect_ex(('127.0.0.1', port)) == 0:
                    return port
            require(p.poll() is None, 'HTTP server startup failed')
            time.sleep(.1)
        raise RuntimeError('HTTP server startup deadline')

    def intent(self, name, operation, args, state):
        doc = {'operation': operation, 'arguments': args, 'principal': 'coordinator',
               'state': state, 'prepared_at': time.time()}
        save(self.out / (name + '.intent.json'), doc)
        return 'Call ato_' + operation + ' with ' + json.dumps(args) + '.\n'

    def spec(self, real):
        contract = {'objective': ('Write marker.txt containing exactly native-codex-mcp-ok followed by newline. '
                                  'Do not dispatch any task or use external tools/services; only write this file.'),
                    'scope': 'Only marker.txt in this disposable worktree',
                    'authorization': 'One bounded native Codex worker writes the harmless marker; no delegation.',
                    'agent': {'kind': 'codex'}, 'timeout_seconds': 120, 'max_attempts': 1,
                    'acceptance': [{'id': 'marker', 'kind': 'command', 'description': 'Host verifies exact marker',
                                    'checks': [[sys.executable, '-c',
                                                "from pathlib import Path; assert Path('marker.txt').read_text() == 'native-codex-mcp-ok\\n'"]]}]}
        if not real:
            contract['objective'] = 'Synthetic lifecycle only; dispatch is intercepted, no worker invocation.'
        return {'schema_version': 1, 'repo': 'demo', 'base_ref': 'HEAD', 'worker_contract': contract}

    def inspect_prompt(self, task_id):
        return ''.join('Call ' + tool + ' with ' + json.dumps(args) + '.\n' for tool, args in (
            ('ato_list', {'repo': 'demo'}), ('ato_status', {'task_id': task_id, 'check_history': True}),
            ('ato_diff', {'task_id': task_id}), ('ato_trace', {'task_id': task_id}),
            ('ato_inbox', {'task_id': task_id})))

    def run(self):
        self.private_config(self.temp / 'worker', '')
        for name in ('repo', 'controller'):
            self.init_repo(name)
        spec = self.spec(True)
        args = {'repo': 'demo', 'spec': spec, 'request_id': 'native-' + uuid.uuid4().hex}
        submit = self.intent('real-submit', 'submit', args, 'state')
        host, _ = self.host_config('stdio')
        prompt = ('Call ato_repositories with {}.\nCall ato_validate with ' + json.dumps({'repo': 'demo', 'spec': spec})
                  + '.\n' + submit)
        calls = self.native('stdio-submit', 'stdio', prompt, {'ato_repositories', 'ato_validate', 'ato_submit'}, host=host)
        task_id = next(c['envelope']['data']['task_id'] for c in calls if c['tool'] == 'ato_submit')
        self.metadata['real_task_id'] = task_id
        store = Store(self.temp / 'state/v2/ato.db')
        until = time.monotonic() + 150
        while time.monotonic() < until:
            task = store.get_task(task_id)
            if task['status'] in ('completed', 'blocked', 'stopped', 'waiting_input'):
                break
            time.sleep(.5)
        require(task['status'] == 'completed', 'Real worker did not pass independent checks: ' + str(task.get('reason')))
        require(Path(task['worktree_path'], 'marker.txt').read_text() == MARKER, 'Host marker mismatch')
        (self.out / 'marker.txt').write_text(MARKER)
        self.native('stdio-final', 'stdio', self.inspect_prompt(task_id), READS - {'ato_repositories'}, host=host)
        port = self.server('state')
        # A completely new native process replays the saved exact intent over HTTP.
        replay = read(self.out / 'real-submit.intent.json')['arguments']
        prompt = ('Call ato_repositories with {}.\nCall ato_validate with ' + json.dumps({'repo': 'demo', 'spec': spec})
                  + '.\nCall ato_submit with ' + json.dumps(replay) + '.\n' + self.inspect_prompt(task_id))
        self.native('http-replay', 'http', prompt, REQUIRED, port=port)
        asyncio.run(self.authority(port, args))
        port = self.server('synthetic', synthetic=True)
        fake = {'repo': 'demo', 'spec': self.spec(False), 'request_id': 'synthetic-' + uuid.uuid4().hex}
        text = self.intent('synthetic-submit', 'submit', fake, 'synthetic')
        # Keys are durable before the native call; task_id is learned from receipt.
        keys = {op: 'synthetic-' + op + '-' + uuid.uuid4().hex for op in ('stop', 'resume', 'stop-final')}
        save(self.out / 'lifecycle-keys.json', {'keys': keys, 'prepared_at': time.time()})
        text += ('Using the returned task_id, call ato_stop with request_id ' + json.dumps(keys['stop'])
                 + ', then ato_resume with timeout_seconds 15 and request_id ' + json.dumps(keys['resume'])
                 + ', then ato_stop with request_id ' + json.dumps(keys['stop-final']) + '.\n'
                 'Then call ato_status with that task_id and check_history true, ato_trace and ato_inbox.\n')
        calls = self.native('http-lifecycle', 'http', text,
                            {'ato_submit', 'ato_stop', 'ato_resume', 'ato_status', 'ato_trace', 'ato_inbox'},
                            port=port, state='synthetic')
        self.metadata['synthetic_task_id'] = next(c['envelope']['data']['task_id'] for c in calls if c['tool'] == 'ato_submit')

    async def authority(self, port, args):
        # Separate synthetic principal checks via official SDK, not native claims.
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        url = f'http://127.0.0.1:{port}'
        facts = {}
        async with httpx2.AsyncClient(trust_env=False, timeout=10) as http:
            facts['missing_bearer'] = (await http.get(url + '/health')).status_code
            facts['wrong_bearer'] = (await http.get(url + '/health', headers={'Authorization': 'Bearer invalid'})).status_code
        for name in ('READER', 'BOT'):
            async with httpx2.AsyncClient(headers={'Authorization': 'Bearer ' + self.tokens[name]}, trust_env=False, timeout=10) as http:
                async with streamable_http_client(url + '/mcp', http_client=http) as streams:
                    async with ClientSession(*streams, read_timeout_seconds=10) as session:
                        await session.initialize()
                        names = [t.name for t in (await session.list_tools()).tools]
                        denial = await session.call_tool('ato_answer', {'task_id': self.metadata['real_task_id'],
                            'question_id': 'request-' + '0' * 32, 'answer': 'Synthetic forbidden attempt', 'request_id': 'deny-answer'})
                        facts[name] = {'tools': names, 'answer_denial': denial.structured_content}
                        if name == 'READER':
                            result = await session.call_tool('ato_submit', args)
                            facts[name]['submit_denial'] = result.structured_content
        save(self.out / 'authority.json', facts)

    def cleanup(self):
        facts = {'groups': [], 'ports': [], 'credential_copies_removed': False, 'temp_removed': False}
        # Include detached executors/children that are not in the server group.
        for state in ('state', 'synthetic'):
            db = self.temp / state / 'v2/ato.db'
            if db.exists():
                store = Store(db)
                for task in store.list_tasks():
                    for run in store.list_runs(task['id']):
                        for prefix in ('child_', ''):
                            pid, birth = run.get(prefix + 'pid'), run.get(prefix + 'pid_identity')
                            if pid and birth:
                                okay = process.stop_owned_group(pid, birth)
                                facts['groups'].append({'pid': pid, 'birth': birth, 'exited': okay})
        for child, row in reversed(self.children):
            okay = process.stop_owned_group(row['pid'], row['birth'])
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                okay = False
            facts['groups'].append({**row, 'exited': okay})
        for port in self.ports:
            with socket.socket() as sock:
                facts['ports'].append({'port': port, 'closed': sock.connect_ex(('127.0.0.1', port)) != 0})
        # Snapshot raw databases plus native worker events only, never Codex homes.
        for state in ('state', 'synthetic'):
            source = self.temp / state / 'v2'
            if source.exists():
                dest = self.out / state
                dest.mkdir(exist_ok=True, mode=0o700)
                for db in source.glob('*.db'):
                    with sqlite3.connect(db) as src, sqlite3.connect(dest / db.name) as dst:
                        src.backup(dst)
                for file in [*source.glob('runtime/**/stdout.jsonl'), *source.glob('runtime/**/stderr.log'), *source.glob('runtime/**/runner.log')]:
                    target = dest / file.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file, target)
                for file in source.glob('runtime/**/worker-result.json'):
                    target = dest / file.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file, target)
        for file in self.credentials:
            file.unlink(missing_ok=True)
        facts['credential_copies_removed'] = all(not p.exists() for p in self.credentials)
        facts['live_config_auth_unchanged'] = all(digest((self.live_home / name).read_bytes()) == sha
                                                for name, sha in self.live_hashes.items())
        shutil.rmtree(self.temp)
        facts['temp_removed'] = not self.temp.exists()
        leaks = []
        for file in self.out.rglob('*'):
            if file.is_file() and any(secret in file.read_bytes() for secret in self.secret_bytes):
                leaks.append(str(file.relative_to(self.out)))
                file.unlink()  # Never retain credential-bearing artifacts, even on failure.
        facts['credential_scan_clean'] = not leaks
        facts['removed_sensitive_artifacts'] = leaks
        save(self.out / 'cleanup.json', facts)
        return facts


class EvidenceStore(Store):
    def _init_schema(self):
        require(self.db_file.is_file(), 'Missing evidence database')

    def _connect(self, *, readonly=False):
        return super()._connect(readonly=True)


def verify(out):
    out = out.resolve()
    manifest = read(out / 'manifest.json')
    require(manifest['baseline'] == BASELINE, 'Unexpected baseline')
    require(manifest['source'] == source_identity(), 'Stale source identity')
    exe = str(Path(shutil.which('codex')).resolve())
    require(manifest['client'] == client_identity(exe), 'Stale native client identity')
    for name, sha in manifest['artifacts'].items():
        path = out / name
        require(path.resolve().is_relative_to(out) and path.is_file() and digest(path.read_bytes()) == sha,
                'Missing/changed artifact: ' + name)
    require(not manifest.get('failure') and not manifest.get('cleanup_failure'), 'Qualification recorded a failure')
    expected_sessions = {'stdio-submit', 'stdio-final', 'http-replay', 'http-lifecycle'}
    require({s['name'] for s in manifest['sessions']} == expected_sessions, 'Missing/unknown native session')
    mandatory = {'real-submit.intent.json', 'synthetic-submit.intent.json', 'lifecycle-keys.json',
                 'marker.txt', 'authority.json', 'cleanup.json', 'state/ato.db',
                 'state/transport-requests.db', 'synthetic/ato.db', 'synthetic/transport-requests.db'}
    for name in expected_sessions:
        mandatory.update(name + suffix for suffix in ('.jsonl', '.stderr', '.config.json', '.prompt.json', '.process.json'))
    require(mandatory <= manifest['artifacts'].keys(), 'Unhashed/missing required evidence')
    by_transport = {'stdio': [], 'http': []}
    by_name = {}
    for session in manifest['sessions']:
        name = session['name']
        expected_transport = 'stdio' if name.startswith('stdio-') else 'http'
        require(session['transport'] == expected_transport, 'Mislabelled native transport')
        client_cfg = read(out / (name + '.config.json'))['mcp_servers']['ato']
        if expected_transport == 'http':
            require(client_cfg['url'].startswith('http://127.0.0.1:') and
                    client_cfg.get('bearer_token_env_var') == 'ATO_SERVER_TOKEN_BOT' and
                    'http_headers' not in client_cfg, 'HTTP env bearer config missing')
        else:
            require(client_cfg['args'][:2] == ['mcp', '--stdio'], 'Not the stdio CLI')
        calls = native_calls(out / (name + '.jsonl'))
        require(set(session['expected']) <= {c['tool'] for c in calls}, 'Missing native operation')
        by_transport[session['transport']].extend(calls)
        by_name[name] = calls
    for transport, calls in by_transport.items():
        require(REQUIRED <= {c['tool'] for c in calls}, 'Missing successful native tools on ' + transport)
    real_id = manifest['real_task_id']
    intent = read(out / 'real-submit.intent.json')
    require(intent['prepared_at'] < min(s['started'] for s in manifest['sessions']), 'Intent persisted too late')
    submissions = [c for name in ('stdio-submit', 'http-replay') for c in by_name[name] if c['tool'] == 'ato_submit']
    require(len(submissions) == 2 and all(c['arguments'] == intent['arguments'] for c in submissions), 'Replay arguments differ')
    require(all(c['envelope']['data']['task_id'] == real_id for c in submissions), 'Replay task differs')
    for state, task_id, run_count in (('state', real_id, 1), ('synthetic', manifest['synthetic_task_id'], 2)):
        store = EvidenceStore(out / state / 'ato.db')
        tasks = store.list_tasks()
        require(len(tasks) == 1 and tasks[0]['id'] == task_id, 'Duplicate/missing task')
        task = tasks[0]
        require(task['origin_kind'] == 'mcp' and task['origin_session_id'] is None, 'Forged native origin')
        require(len(store.list_runs(task_id)) == run_count, 'Duplicate/missing run')
        events = store.list_events(task_id)
        require(sum(e['kind'] == 'task_submitted' for e in events) == 1, 'Duplicate admission')
        with sqlite3.connect(out / state / 'transport-requests.db') as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT * FROM requests')]
        require(all(r['state'] == 'complete' and r['task_id'] == task_id for r in rows), 'Unresolved journal')
        require(sum(r['operation'] == 'submit' for r in rows) == 1, 'Duplicate journal submit')
        from ato.spec import canonical_json
        require(digest(canonical_json(task['spec'])) == task['spec_sha256'], 'Frozen spec digest mismatch')
        relevant = submissions if state == 'state' else by_name['http-lifecycle']
        for call in relevant:
            data = call['envelope']['data']
            if 'spec_sha256' in data:
                require(data['task_id'] == task_id and data['spec_sha256'] == task['spec_sha256'], 'Task/spec changed during recovery')
        saved = read(out / ('real-submit.intent.json' if state == 'state' else 'synthetic-submit.intent.json'))
        original_args = {k: v for k, v in saved['arguments'].items() if k != 'request_id'}
        original_args['repository_binding'] = {'root': task['repo_root'], 'key': task['repo_key']}
        row = next(r for r in rows if r['operation'] == 'submit')
        require(row['key_hash'] == digest(saved['arguments']['request_id'].encode()) and
                row['args_hash'] == digest(canonical_json({'operation': 'submit', 'arguments': original_args})),
                'Journal does not corroborate the native intent')
        if state == 'state':
            require(task['status'] == 'completed', 'Worker not completed')
            checks = store.latest_checks(task_id)
            require(len(checks) == 1 and checks[0]['criterion_id'] == 'marker' and checks[0]['state'] == 'passed', 'Worker check failed')
            require((out / 'marker.txt').read_text() == MARKER, 'Marker evidence differs')
            logs = list((out / state).glob('runtime/**/stdout.jsonl'))
            require(len(logs) == 1, 'Expected exactly one real worker')
            worker_events = [json.loads(line) for line in logs[0].read_text().splitlines() if line.strip()]
            require(any(e.get('type') == 'turn.completed' for e in worker_events), 'No successful native worker turn')
            for name in ('stdio-final', 'http-replay'):
                status = next(c['envelope']['data'] for c in by_name[name] if c['tool'] == 'ato_status')
                require(status['completion_notification']['state'] == 'unsupported', 'Unexpected native push binding')
                require(status['status'] == 'completed' and all(c['state'] == 'passed' for c in status['checks']), 'Controller did not read final checks')
                diff = next(c['envelope']['data'] for c in by_name[name] if c['tool'] == 'ato_diff')
                require('marker.txt' in json.dumps(diff), 'Controller did not observe marker diff')
        else:
            require(task['status'] == 'stopped', 'Synthetic STOP failed')
            require(sum(e['kind'] == 'synthetic_native_boundary' for e in events) == 2, 'Synthetic dispatch mismatch')
            require(sum(e['kind'] == 'task_resumed' for e in events) == 1, 'Missing resume')
            resume = next(r for r in store.list_runs(task_id) if r['kind'] == 'resume')
            require(resume['timeout_seconds'] == 15, 'Resume override missing')
    authority = read(out / 'authority.json')
    require(authority['missing_bearer'] == authority['wrong_bearer'] == 401, 'HTTP auth not enforced')
    require(set(authority['READER']['tools']) == READS, 'Read principal tools mismatch')
    for name in ('READER', 'BOT'):
        require(authority[name]['answer_denial']['error']['code'] == 'FORBIDDEN', 'Bot can answer')
    require(authority['READER']['submit_denial']['error']['code'] == 'FORBIDDEN', 'Reader can submit')
    cleanup = read(out / 'cleanup.json')
    require(all(cleanup[k] is True for k in ('credential_copies_removed', 'temp_removed', 'live_config_auth_unchanged', 'credential_scan_clean')), 'Cleanup failed')
    require(cleanup['groups'] and all(r['exited'] and process.owned_group_exited(r['pid'], r['birth']) for r in cleanup['groups']), 'Owned process remains')
    require(all(p['closed'] for p in cleanup['ports']) and len(cleanup['ports']) == 2, 'Listener cleanup failed')
    print('Native Codex evidence verified (stdio, HTTP, exact replay, real worker, synthetic lifecycle, cleanup).')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--synthetic-server':
        return synthetic_server(sys.argv[2:])
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run', action='store_true')
    group.add_argument('--verify-evidence', type=Path)
    parser.add_argument('--evidence', type=Path, default=ROOT / '.verification/codex-mcp/native')
    args = parser.parse_args()
    if args.verify_evidence:
        verify(args.verify_evidence)
        return 0
    os.umask(0o077)
    q = Qualification(args.evidence)
    try:
        q.run()
    except Exception as exc:
        q.metadata['failure'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        try:
            q.cleanup()
        except Exception as exc:
            q.metadata['cleanup_failure'] = type(exc).__name__
        q.metadata['artifacts'] = {str(p.relative_to(q.out)): digest(p.read_bytes()) for p in q.out.rglob('*')
                                   if p.is_file() and p.name != 'manifest.json'}
        save(q.out / 'manifest.json', q.metadata)
    verify(q.out)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('Qualification failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
