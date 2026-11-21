"""Opt-in ACP worker bridge. Profiles are registered by trusted Python code.

The bridge imports the ACP SDK only in its child invocation. A profile owns its
command and its native-receipt protocol; plans cannot select paths or commands.
"""
import asyncio
from contextlib import asynccontextmanager, contextmanager
import io
import json
import logging
import os
import re
import stat
import sys
import uuid
from pathlib import Path

import agent_registry

STDERR_MAX_BYTES = 1024 * 1024


@contextmanager
def safe_protocol_logging():
    """SDK validation stacks can embed protocol values, even with stderr piped."""
    import acp
    source = str(Path(acp.__file__).resolve().parent) + os.sep

    class ProtocolFilter(logging.Filter):
        def filter(self, record):
            if record.pathname.startswith(source) or record.name.startswith('asyncio'):
                kind = type(record.exc_info[1]).__name__ if record.exc_info else 'ProtocolError'
                record.msg, record.args = 'ACP SDK diagnostic: %s', (kind,)
                record.exc_info = record.exc_text = record.stack_info = None
            return True

    filter_ = ProtocolFilter()
    loggers = (logging.getLogger(), logging.getLogger('asyncio'))
    for logger in loggers:
        logger.addFilter(filter_)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(filter_)


@contextmanager
def private_stderr(path, directory):
    if path is None:
        yield None
        return
    path = Path(path).absolute()
    parent = path.parent.resolve(strict=True)
    info = path.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid() or not parent.is_relative_to(directory/'checkpoints')):
        raise ValueError('stderr requires a private runtime directory inside run checkpoints')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('stderr requires an owned regular file without hard links')
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        handle = os.fdopen(fd, 'wb')
        fd = None
        with handle:
            yield handle
    finally:
        if fd is not None:
            os.close(fd)


async def drain_stderr(stream, log):
    written, writable = 0, True
    while chunk := await stream.read(65536):
        # Keep draining after the disk cap (or a disk error) so the child cannot
        # deadlock on a full pipe. No raw diagnostic enters parent stderr/trace.
        if writable and written < STDERR_MAX_BYTES:
            try:
                part = chunk[:STDERR_MAX_BYTES-written]
                log.write(part)
                written += len(part)
            except OSError:
                writable = False


@asynccontextmanager
async def agent_process(client, command, directory, repo, environment, stderr_path):
    import acp
    draining = None
    with private_stderr(stderr_path, directory) as log, safe_protocol_logging():
        try:
            async with acp.spawn_agent_process(client, *command, cwd=repo, env=environment,
                transport_kwargs={'stderr': asyncio.subprocess.PIPE if log else asyncio.subprocess.DEVNULL}) as pair:
                if log:
                    draining = asyncio.create_task(drain_stderr(pair[1].stderr, log))
                yield pair
        finally:
            # The SDK closes/terminates the process before this final drain.
            # A descendant retaining the pipe cannot hold bridge cleanup forever.
            if draining is not None:
                try:
                    await asyncio.wait_for(draining, timeout=2)
                except TimeoutError:
                    pass


class ACPAdapter:
    capabilities = frozenset({'worker', 'acp_process'})

    def __init__(self, kind, bootstrap):
        self.kind = kind
        self.bootstrap = str(Path(bootstrap).resolve())

    def command(self, request):
        return [sys.executable, self.bootstrap, '_acp_bridge', str(request.directory),
                request.label, str(request.schema), str(request.output)]


async def invoke(command, directory, label, schema_path, output, prompt, *, environment=None,
                 native_result_only=False, stderr_path=None):
    # Import lazily so the original standard-library-only routes still work.
    import acp
    from acp.schema import TextContentBlock, RequestPermissionResponse, DeniedOutcome
    import agents
    import runner
    import task_trace
    from acp_outcome import Outcome

    directory = Path(directory).resolve()
    output = Path(output).resolve()
    if output.parent != directory / 'checkpoints':
        raise ValueError('checkpoint must belong to run')
    match = re.fullmatch(r'(.+)-attempt-(\d+)-[0-9a-f]+', label)
    run_state = runner.read(directory / 'state.json')
    if match:
        task, attempt = match.group(1), int(match.group(2))
    elif re.fullmatch(r'plan-[0-9a-f]+', label):
        task, attempt = '_planner', run_state['planning_attempts']
    else:
        raise ValueError('invalid attempt label')
    repo = Path(run_state['repo']).resolve()
    turn = uuid.uuid4().hex

    class Client:
        def __init__(self):
            self.session = None
            self.outcome = None
            self.chunks = io.StringIO()
            self.size = 0
            self.number = 0
            self.progress_recorded = False
            self.native_result = None
        async def request_permission(self, **kwargs):
            self.number += 1
            task_trace.append(directory, 'approval', event_id=f'{label}:approval:{self.number}',
                              task=task, attempt=attempt, session=self.session, turn=turn,
                              source='acp', state='cancelled')
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome='cancelled'))

        async def session_update(self, session_id, update, **kwargs):
            if session_id != self.session or self.outcome is None:
                return
            self.number += 1
            context = dict(task=task, attempt=attempt, session=session_id, turn=turn)
            kind = update.session_update
            if kind in ('tool_call', 'tool_call_update'):
                locations = getattr(update, 'locations', None) or []
                target = locations[0].path if len(locations) == 1 else None
                task_trace.append(directory, 'tool', event_id=f'{label}:acp:{self.number}',
                    source='acp', tool=update.tool_call_id, state=update.status,
                    target=target, workspace=repo, **context)
            elif kind == 'agent_message_chunk':
                content = update.content
                if content.type == 'text':
                    before = self.size
                    self.size += len(content.text)
                    if self.size <= 1024 * 1024:
                        self.chunks.write(content.text)
                    elif before <= 1024 * 1024:
                        task_trace.append(directory, 'output_truncated', event_id=f'{label}:truncated', source='acp', **context)
                if not self.progress_recorded:
                    self.progress_recorded = True
                    task_trace.append(directory, 'progress', event_id=f'{label}:progress', source='acp', **context)
            elif kind == 'session_info_update':
                # This namespace is only authoritative for an explicitly trusted
                # profile whose adapter emits it directly from its native stream.
                meta = update.field_meta or {}
                receipt = meta.get('ctaskNativeReceipt')
                if isinstance(receipt, dict) and receipt.get('turn') == turn:
                    reason = receipt.get('stopReason')
                    if reason in ('stop', 'error', 'aborted', 'cancelled'):
                        self.outcome.native_receipt(session_id, turn, reason, trusted=True)
                        if reason == 'stop' and isinstance(receipt.get('result'), str):
                            if len(receipt['result']) <= 1024 * 1024:
                                self.native_result = receipt['result']
                        event = 'native_success' if reason == 'stop' else 'native_error'
                        task_trace.append(directory, event, event_id=f'{label}:native:{self.number}',
                                          source='native', **context)

    client = Client()
    async with agent_process(client, command, directory, repo, environment, stderr_path) as (conn, proc):
        await conn.initialize(protocol_version=1)
        session = await conn.new_session(cwd=str(repo))
        client.session = session.session_id
        client.outcome = Outcome(client.session, turn)
        # The turn nonce is conveyed in the protocol request's extension metadata,
        # not inferred from model output or a prior session's transcript.
        schema = runner.read(schema_path)
        response = await conn.prompt(session_id=client.session,
            prompt=[TextContentBlock(type='text', text=prompt + '\nReturn only a JSON object matching this schema: ' + json.dumps(schema))],
            ctaskTurn=turn)
        client.outcome.transport_receipt(client.session, turn, response.stop_reason)
        context = dict(task=task, attempt=attempt, session=client.session, turn=turn)
        task_trace.append(directory,
            'transport_completed' if client.outcome.transport == 'completed' else 'transport_error',
            event_id=label+':transport', source='acp', **context)
        if client.outcome.native == 'unverified':
            task_trace.append(directory, 'native_unverified', event_id=label+':native:missing',
                              source='native', **context)
        if client.outcome.native != 'success' or client.outcome.transport != 'completed':
            raise ValueError('native or transport outcome failed or unverified')
        if not native_result_only and client.size > 1024 * 1024:
            raise ValueError('structured result exceeds limit')
        result_text = client.native_result if native_result_only else client.chunks.getvalue()
        if result_text is None:
            raise ValueError('native structured result is unavailable')
        value = agents.validate_result(json.loads(result_text), schema)
        runner.save(output, value)


def main(command, args, *, environment=None, native_result_only=False, stderr_path=None):
    if len(args) != 4:
        raise ValueError('bridge requires run, label, schema, and output')
    asyncio.run(invoke(command, *args, sys.stdin.read(), environment=environment,
                       native_result_only=native_result_only, stderr_path=stderr_path))
