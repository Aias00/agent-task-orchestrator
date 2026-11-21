"""Native mailbox adapters. These are hosts, never arbitrary TTY injectors.

Construction is explicit and starts no process. Use start()/close() in a bounded
context, dispatch_one(), then events() and receive_event(). No scheduler lives here.
Native auth is supplied by the caller's isolated environment, never copied.
"""
from dataclasses import asdict
from functools import wraps
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid

from transport import DeliveryResult, TransportEvent


class AdapterError(ValueError):
    pass


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)
    return call


def _save(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as handle:
        os.chmod(tmp, 0o600)
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


class CodexQueueAdapter:
    """Exact queue receipt verification; receipt means queued, not executed.

    The caller must verify the session metadata before constructing a binding.
    An injected verifier can revalidate that identity immediately before send.
    CTASK_QUEUE_BIN remains supported for legacy process fixtures.
    """
    def __init__(self, *, home, verifier, executable=None, timeout=25, cwd=None):
        self.home = str(Path(home).resolve())
        self.verifier = verifier
        self.executable = executable or os.environ.get('CTASK_QUEUE_BIN', 'codex')
        self.timeout = timeout
        self.cwd = str(Path(cwd).resolve()) if cwd is not None else None

    def send(self, endpoint, message):
        if endpoint['kind'] != 'codex' or message['target'] != endpoint['id']:
            raise AdapterError('Codex endpoint mismatch')
        thread = str(uuid.UUID(endpoint['session_id']))
        self.verifier(self.home, thread, endpoint['project'])
        try:
            result = subprocess.run([self.executable, 'queue', '--thread', thread,
                '--message', message['body']], cwd=self.cwd or endpoint['project'],
                env=dict(os.environ, CODEX_HOME=self.home), capture_output=True,
                text=True, timeout=self.timeout)
        except OSError:
            return DeliveryResult('failed')
        except subprocess.TimeoutExpired:
            return DeliveryResult('delivery_unknown')
        match = re.search(r'Queued message ([0-9a-f-]+) for thread ([0-9a-f-]+)', result.stdout)
        if result.returncode == 0 and match and match[2] == thread:
            return DeliveryResult('delivered', match[1])
        return DeliveryResult('delivery_unknown')


class NativeAdapter:
    """One owned, persisted native session with one outstanding correlated turn.

    A process lifetime lock and persisted pending barrier complement the bus
    lease. A crashed pending session requires explicit reconciliation, never a
    fresh session or automatic resend. Idle sessions restart by exact native ID.
    Default tools are disabled; file tools require the existing macOS sandbox
    boundary and path guards. User/project config, hooks, MCP and agents are off.
    """
    kind = None

    def __init__(self, *, endpoint_id, session_id, project, runtime, executable=None,
                 provider_env=None, model=None, tools='none', timeout=30):
        self.endpoint_id = endpoint_id
        self.session_id = str(uuid.UUID(session_id))
        self.project = str(Path(project).resolve(strict=True))
        self.runtime = Path(runtime).resolve()
        if not Path(self.project).is_dir() or not endpoint_id:
            raise AdapterError('explicit endpoint and project directory required')
        if tools not in ('none', 'read-only', 'workspace-write'):
            raise AdapterError('invalid file tool policy')
        self.tools, self.timeout, self.model = tools, timeout, model
        self.executable = executable or shutil.which(self.kind)
        if not self.executable:
            raise AdapterError('native executable unavailable')
        # Environment is deliberately not inherited: no ambient tokens, config,
        # telemetry, cloud routing, NODE_OPTIONS or arbitrary plugin loading.
        self.env = {k: os.environ[k] for k in ('PATH', 'LANG') if k in os.environ}
        if any(key in (provider_env or {}) for key in ('HOME', 'CODEX_HOME')):
            raise AdapterError('provider settings cannot replace system or Codex home directories')
        self.env.update(provider_env or {})
        self.process = None
        self._mutex = threading.RLock()
        self._lock = None
        self._reader = None
        self._records = queue.Queue(maxsize=1024)
        self._events = []
        self.pending = None
        self._assistant = ''
        self._prompt_ack = False
        self.native_session_file = None

    def _identity(self):
        return dict(endpoint_id=self.endpoint_id, session_id=self.session_id,
                    project=self.project, kind=self.kind, tools=self.tools)

    def _state(self, state):
        _save(self.runtime/'session.json', dict(**self._identity(), state=state,
              pending=self.pending, native_session_file=self.native_session_file,
              pid=self.process.pid if self.process else None))

    def _argv(self, resume):
        raise NotImplementedError

    @_serialized
    def start(self):
        if self.process is not None:
            raise AdapterError('adapter is already started')
        self.runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.runtime, 0o700)
        self._lock = (self.runtime/'owner.lock').open('a+')
        os.chmod(self.runtime/'owner.lock', 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            self._lock = None
            raise AdapterError('managed endpoint already owned') from None
        try:
            state_path = self.runtime/'session.json'
            resume = state_path.exists()
            if resume:
                old = json.loads(state_path.read_text())
                if any(old.get(k) != v for k, v in self._identity().items()):
                    raise AdapterError('persisted native identity or permission mismatch')
                if old.get('pending'):
                    raise AdapterError('unresolved native send; reconcile before restart, do not replay')
                native = old.get('native_session_file')
                if not native or not Path(native).is_file() or not Path(native).resolve().is_relative_to(self.runtime):
                    raise AdapterError('native transcript missing; refuse empty-session replacement')
                self.native_session_file = native
            for name in ('tmp', 'claude', 'pi', 'sessions', 'jiti'):
                (self.runtime/name).mkdir(mode=0o700, exist_ok=True)
            self.env.update(TMPDIR=str(self.runtime/'tmp')+'/',
                CLAUDE_CONFIG_DIR=str(self.runtime/'claude'), CLAUDE_CODE_TMPDIR=str(self.runtime/'tmp'),
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1',
                PI_CODING_AGENT_DIR=str(self.runtime/'pi'), PI_OFFLINE='1', PI_TELEMETRY='0',
                JITI_CACHE=str(self.runtime/'jiti'), CTASK_AGENT_REPO=self.project,
                CTASK_AGENT_SANDBOX=self.tools)
            argv = self._argv(resume)
            if self.tools != 'none':
                if not Path('/usr/bin/sandbox-exec').is_file():
                    raise AdapterError('file tools require macOS sandbox-exec; no fallback')
                quote = lambda p: json.dumps(str(p))
                profile = ['(version 1)', '(allow default)', '(deny file-write*)',
                    '(allow file-write* (literal "/dev/null") (subpath '+quote(self.runtime)+'))']
                if self.tools == 'workspace-write':
                    profile.append('(allow file-write* (require-all (subpath '+quote(self.project)+') '
                        '(require-not (subpath '+quote(Path(self.project)/'.git')+')) '
                        '(require-not (subpath '+quote(Path(self.project)/'.codex')+'))))')
                (self.runtime/'sandbox.sb').write_text('\n'.join(profile)+'\n')
                argv = ['/usr/bin/sandbox-exec', '-f', str(self.runtime/'sandbox.sb'), *argv]
            # No detached jobs/process groups: lifecycle is owned by this host.
            self._stderr = (self.runtime/'stderr.log').open('ab', buffering=0)
            os.chmod(self.runtime/'stderr.log', 0o600)
            self._records = queue.Queue(maxsize=1024)
            self.process = subprocess.Popen(argv, cwd=self.project, env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr)
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()
            self._state('starting')
            if self.kind == 'pi':
                self._write(dict(id='identity', type='get_state'))
                row = self._until(lambda r: r.get('type') == 'response' and r.get('id') == 'identity')
                if not row.get('success') or row.get('data', {}).get('sessionId') != self.session_id:
                    raise AdapterError('Pi native session identity mismatch')
                native_file = Path(row['data']['sessionFile']).resolve()
                if not native_file.is_relative_to(self.runtime/'sessions'):
                    raise AdapterError('Pi session file outside managed runtime')
                if resume and str(native_file) != self.native_session_file:
                    raise AdapterError('Pi resumed a different session file')
                self.native_session_file = str(native_file)
            self._state('idle')
            return self
        except BaseException:
            self.close()
            if hasattr(self, '_stderr'):
                self._stderr.close()
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(8*1024*1024+1)
                if not line:
                    break
                if len(line) > 8*1024*1024:
                    raise AdapterError('oversized native record')
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise AdapterError('native record must be an object')
                self._records.put(row, timeout=1)
        except Exception:
            self._records.put({'_protocol_error': True}, timeout=1)
        finally:
            try:
                self._records.put({'_eof': True}, timeout=1)
            except queue.Full:
                pass

    def _write(self, record):
        self.process.stdin.write(json.dumps(record, ensure_ascii=False).encode()+b'\n')
        self.process.stdin.flush()

    def _until(self, predicate, timeout=None):
        deadline = time.monotonic()+(self.timeout if timeout is None else timeout)
        while True:
            try:
                row = self._records.get(timeout=max(0, deadline-time.monotonic()))
            except queue.Empty:
                raise AdapterError('native response deadline exceeded') from None
            if row.get('_eof') or row.get('_protocol_error'):
                raise AdapterError('native process exited or invalid protocol')
            self._consume(row)
            if predicate(row):
                return row
            if time.monotonic() >= deadline:
                raise AdapterError('native response deadline exceeded')

    def _emit(self, kind, text=''):
        event = TransportEvent(kind, self.pending, self.endpoint_id, self.session_id,
            self.pending+':'+kind, self.kind+' native managed session', text)
        # Durable normalized events can be redelivered through idempotent receipts
        # after a host crash. Never persist full provider error payloads or env.
        path = self.runtime/'events.jsonl'
        with path.open('a') as handle:
            os.chmod(path, 0o600)
            handle.write(json.dumps(asdict(event), ensure_ascii=False)+'\n')
            handle.flush()
            os.fsync(handle.fileno())
        self._events.append(event)

    def _consume(self, row):
        if self.kind == 'claude':
            if row.get('session_id') and row['session_id'] != self.session_id:
                raise AdapterError('Claude native session identity mismatch')
            if not self.pending:
                if row.get('type') in ('result', 'assistant', 'user'):
                    raise AdapterError('unsolicited native turn')
                return
            if row.get('type') == 'user':
                if row.get('uuid') != self._user_uuid:
                    raise AdapterError('Claude user acknowledgement mismatch')
                self._prompt_ack = True
                self._emit('accepted')
            if row.get('type') == 'result':
                if not self._prompt_ack or row.get('session_id') != self.session_id:
                    raise AdapterError('uncorrelated Claude result')
                if row.get('is_error') or row.get('subtype') != 'success':
                    self._emit('blocked', 'Native turn failed; inspect isolated CLI diagnostics')
                    raise AdapterError('Claude native turn failed')
                self._finish(str(row.get('result', '')))
        elif self.pending:
            if row.get('type') == 'response' and row.get('command') == 'prompt':
                if row.get('id') != self.pending or not row.get('success'):
                    raise AdapterError('Pi prompt acknowledgement mismatch or rejection')
                if row.get('data', {}).get('disposition') != 'started':
                    raise AdapterError('Pi did not start the managed turn')
                self._prompt_ack = True
                self._emit('accepted')
            if row.get('type') == 'message_end' and row.get('message', {}).get('role') == 'assistant':
                message = row['message']
                if message.get('stopReason') != 'stop':
                    self._emit('blocked', 'Native turn failed; inspect isolated CLI diagnostics')
                    raise AdapterError('Pi assistant failed or truncated')
                self._assistant = ''.join(c.get('text', '') for c in message.get('content', [])
                                          if c.get('type') == 'text')
            if row.get('type') == 'agent_settled':
                if not self._prompt_ack or row.get('aborted') or not self._assistant:
                    raise AdapterError('Pi turn not successfully settled')
                self._finish(self._assistant)

    def _finish(self, text):
        if self.kind == 'claude':
            candidates = list((self.runtime/'claude/projects').glob('*/'+self.session_id+'.jsonl'))
            if len(candidates) != 1:
                raise AdapterError('Claude native transcript not persisted at the exact session ID')
            self.native_session_file = str(candidates[0].resolve())
        if not self.native_session_file or not Path(self.native_session_file).is_file():
            raise AdapterError('native transcript not persisted')
        self._emit('reply', text)
        self._emit('completed')
        self.pending = None
        self._state('idle')

    @_serialized
    def send(self, endpoint, message):
        if (any(endpoint.get(k) != v for k, v in dict(id=self.endpoint_id, kind=self.kind,
                session_id=self.session_id, project=self.project).items())
                or message.get('target') != self.endpoint_id):
            raise AdapterError('native endpoint/project/session mismatch')
        if self.pending:
            raise AdapterError('endpoint busy; queue in the durable mailbox')
        if not self.process or self.process.poll() is not None:
            return DeliveryResult('failed')
        self.pending = message['id']
        self._assistant, self._prompt_ack = '', False
        self._state('pending')  # Before the first byte can reach a CLI.
        envelope = {k: message.get(k) for k in ('id', 'kind', 'correlation_id',
                'reply_to', 'task_id', 'contract_hash', 'body')}
        if message.get('kind') in ('question_delivery', 'answer_delivery', 'recovery', 'delegation', 'control'):
            envelope['host_protocol'] = {
                'instructions': 'Tools are disabled. Return plain result text, or one JSON object with a ctask event. '
                    'The host validates the exact current message and original authority. Never invent a user answer. '
                    'An acceptance event acknowledges receipt only; it does not resume or complete a workflow.',
                'events': [
                    {'ctask': {'kind': 'question', 'question': 'Concrete question', 'recommendation': 'Reasoned option'}},
                    {'ctask': {'kind': 'needs_input', 'question': 'Missing user input', 'recommendation': 'Option'}},
                    {'ctask': {'kind': 'blocked', 'note': 'Concrete recovery blocker'}},
                    {'ctask': {'kind': 'accept_answer', 'question_id': 'Exact question ID in this sourced user answer'}},
                    {'ctask': {'kind': 'accept_delegation'}},
                ],
            }
        prompt = json.dumps(envelope, ensure_ascii=False)
        if self.kind == 'claude':
            self._user_uuid = str(uuid.uuid4())
            record = dict(type='user', session_id=self.session_id, uuid=self._user_uuid,
                message=dict(role='user', content=prompt), parent_tool_use_id=None)
        else:
            record = dict(id=self.pending, type='prompt', message=prompt)
        try:
            self._write(record)
            self._until(lambda r: self._prompt_ack)
        except (OSError, AdapterError):
            self._state('delivery_unknown')
            return DeliveryResult('delivery_unknown')
        return DeliveryResult('delivered', message['id'])

    @_serialized
    def events(self, *, timeout=None):
        """Wait for the active turn; normalize only after native completion.

        On timeout the pending barrier remains and close() reaps the child. No
        retries or invented completion. Caller feeds events to the bus/controller.
        """
        if self.pending:
            try:
                self._until(lambda r: self.pending is None, timeout)
            except (OSError, AdapterError):
                self._state('delivery_unknown')
                raise
        events, self._events = self._events, []
        return events

    def recorded_events(self):
        """Replay durable normalized receipts; the bus deduplicates receipt IDs.

        This does not clear unknown delivery or grant workflow permission. An
        incomplete final JSONL line (crash during append) is left for inspection.
        """
        path = self.runtime/'events.jsonl'
        if not path.exists():
            return []
        result = []
        with path.open('rb') as handle:
            for line in handle:
                if not line.endswith(b'\n'):
                    break
                event = TransportEvent(**json.loads(line))
                if event.endpoint_id != self.endpoint_id or event.session_id != self.session_id:
                    raise AdapterError('stored event identity mismatch')
                result.append(event)
        return result

    @_serialized
    def close(self):
        proc = self.process
        if proc is not None:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            if self._reader:
                self._reader.join(timeout=3)
            proc.stdout.close()
            self._stderr.close()
            self._state('delivery_unknown' if self.pending else 'closed')
            self.process = None
        if self._lock:
            self._lock.close()
            self._lock = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


class ClaudeAdapter(NativeAdapter):
    kind = 'claude'

    def _argv(self, resume):
        names = {'none': '', 'read-only': 'Read,Glob,Grep',
                 'workspace-write': 'Read,Glob,Grep,Edit,Write'}[self.tools]
        argv = [self.executable, '--safe-mode', '--restricted', '--disable-slash-commands',
            '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--permission-mode', 'dontAsk', '--tools', names, '--allowedTools', names,
            '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--verbose', '--replay-user-messages', '--resume' if resume else '--session-id', self.session_id]
        return argv+(['--model', self.model] if self.model else [])


class PiAdapter(NativeAdapter):
    kind = 'pi'

    def _argv(self, resume):
        argv = [self.executable, '--mode', 'rpc', '--session-id', self.session_id,
            '--session-dir', str(self.runtime/'sessions'), '--no-extensions', '--no-skills',
            '--no-prompt-templates', '--no-themes', '--no-mcp', '--no-context-files',
            '--no-approve', '--offline', '--thinking', 'off']
        if self.tools == 'none':
            argv += ['--no-tools']
        else:
            names = 'read,grep,find,ls'+(',edit,write' if self.tools == 'workspace-write' else '')
            argv += ['--tools', names, '--extension', str(Path(__file__).with_name('pi-path-guard.mjs'))]
        return argv+(['--model', self.model] if self.model else [])
