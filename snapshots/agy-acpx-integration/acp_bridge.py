"""Opt-in ACP worker bridge. Profiles are registered by trusted Python code.

The bridge imports the ACP SDK only in its child invocation. A profile owns its
command and its native-receipt protocol; plans cannot select paths or commands.
"""
import asyncio
import io
import json
import re
import sys
import uuid
from pathlib import Path

import agent_registry


class ACPAdapter:
    capabilities = frozenset({'worker', 'acp_process'})

    def __init__(self, kind, bootstrap):
        self.kind = kind
        self.bootstrap = str(Path(bootstrap).resolve())

    def command(self, request):
        return [sys.executable, self.bootstrap, '_acp_bridge', str(request.directory),
                request.label, str(request.schema), str(request.output)]


async def invoke(command, directory, label, schema_path, output, prompt, *, environment=None, native_result_only=False):
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
    async with acp.spawn_agent_process(client, *command, cwd=repo, env=environment,
                                       transport_kwargs={'stderr': asyncio.subprocess.DEVNULL}) as (conn, proc):
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


def main(command, args, *, environment=None, native_result_only=False):
    if len(args) != 4:
        raise ValueError('bridge requires run, label, schema, and output')
    asyncio.run(invoke(command, *args, sys.stdin.read(), environment=environment,
                       native_result_only=native_result_only))
