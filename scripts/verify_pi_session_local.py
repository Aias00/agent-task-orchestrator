#!/usr/bin/env python3
"""Origin-only, <=240s real Pi CLI + scripted loopback model qualification.

No cloud inference, native auth, real task/hub, installation or runtime switch.
Every question/user answer below is a labelled synthetic local fixture. Detailed
receipts are allowlisted and retained; private runtime/native history is deleted.
Do not run this script as a scheduler worker.
"""
import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ARTIFACT = 'ato-local-artifact\n'
SEED = 'ato-local-seed\n'
MODEL = 'ato-scripted'
MAX_REQUEST = 2 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def save(path, value):
    from ato_workflow.transport import _private_save
    _private_save(path, value)


@contextmanager
def disabled_codex():
    """Fail probes cover legacy static aliases, native SQLite and app-server.

    The shared SQLite module also serves ATO's actual MessageBus, so only the
    explicitly isolated Codex namespace is forbidden. No database is opened to
    decide this boundary. Restore all probes for offline in-process tests.
    """
    from ato_adapters import appserver, codex_session_adapter
    import codex_legacy
    import sqlite3
    from urllib.parse import unquote, urlsplit
    native_root = Path(os.environ['CODEX_HOME']).resolve()
    original_connect = sqlite3.connect
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append('codex_native_access')
        raise AssertionError('Codex native access forbidden in local-only Pi smoke')
    def connect(database, *args, **kwargs):
        value = os.fsdecode(database)
        path = Path(unquote(urlsplit(value).path) if value.startswith('file:') else value).resolve()
        if path == native_root or path.is_relative_to(native_root):
            forbidden()
        return original_connect(database, *args, **kwargs)
    with ExitStack() as stack:
        for name in ('session_path', 'queue', 'create_session', 'current', 'observe_recovery', 'observe_reply'):
            stack.enter_context(patch.object(codex_session_adapter, name, side_effect=forbidden))
            if hasattr(codex_legacy, name):
                stack.enter_context(patch.object(codex_legacy, name, side_effect=forbidden))
        for name in ('session_path', 'create_session', 'current_locator', 'observe_recovery', 'observe_reply'):
            stack.enter_context(patch.object(codex_session_adapter.CodexSessionAdapter, name, staticmethod(forbidden)))
        stack.enter_context(patch.object(appserver, 'Client', side_effect=forbidden))
        stack.enter_context(patch.object(sqlite3, 'connect', side_effect=connect))
        yield attempts


def codex_probes(work):
    """Fail closed in every candidate Python CLI child as well as the host."""
    folder = work/'probes'
    folder.mkdir(mode=0o700)
    (folder/'sitecustomize.py').write_text('''from scripts.verify_pi_session_local import disabled_codex
_ato_codex_probes = disabled_codex()
_ato_codex_probes.__enter__()
''')
    return str(folder)+os.pathsep+str(ROOT)


class ScriptedModel:
    """Six deterministic OpenAI stream responses; binds numeric loopback only.

    Requests are consumed in order, never replayed after an error. No proxy,
    outbound client, raw request logging, provider credential or model exists.
    """
    steps = (
        ('read', {'path': 'seed.txt'}), (None, 'local-first-receipt'),
        ('write', {'path': 'result.txt', 'content': ARTIFACT}), (None, 'local-answer-receipt'),
        ('read', {'path': 'result.txt'}), (None, 'local-restart-receipt'))

    def __init__(self):
        self.receipts, self.errors = [], []
        self.server = None
        self.handlers = set()
        self.index = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0, limit=MAX_REQUEST+1)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def validate_tool_result(self, body):
        """Require the native result of our exact preceding call on the wire.

        A completed ACP tool event also covers failed tools. Neither that event
        nor an unrelated old tool message proves this sample's file operation.
        Keep response() as the small stream formatter used by offline fixtures.
        """
        if self.index % 2 != 1:
            return
        messages = body.get('messages', [])
        call_id = 'local-tool-'+str(self.index-1)
        require(messages and messages[-1].get('role') == 'tool'
            and messages[-1].get('tool_call_id') == call_id,
            'missing exact native tool result; no replay')
        previous_tool, arguments = self.steps[self.index-1]
        calls = [call for message in messages if message.get('role') == 'assistant'
                 for call in message.get('tool_calls', []) if call.get('id') == call_id]
        require(len(calls) == 1 and calls[0].get('function', {}).get('name') == previous_tool
            and json.loads(calls[0]['function']['arguments']) == arguments,
            'native tool result has a different call identity or arguments')
        content = messages[-1].get('content')
        if isinstance(content, list):
            content = '\n'.join(part.get('text', '') for part in content if part.get('type') == 'text')
        expected = (SEED.strip(), 'Successfully wrote to result.txt', ARTIFACT.strip())[self.index//2]
        require(isinstance(content, str) and expected in content, 'native file tool did not return expected result')

    def response(self, body):
        require(self.index < len(self.steps), 'unexpected extra model request; no retry allowance')
        require(body.get('model') == MODEL and body.get('stream') is True, 'unexpected model or protocol')
        messages = body.get('messages')
        require(isinstance(messages, list) and messages, 'model request missing messages')
        tool, content = self.steps[self.index]
        if tool:
            names = {entry.get('function', {}).get('name') for entry in body.get('tools', [])}
            require(tool in names, 'real CLI did not expose required bounded file tool')
            delta = dict(role='assistant', tool_calls=[dict(index=0, id='local-tool-'+str(self.index),
                type='function', function=dict(name=tool, arguments=json.dumps(content)))])
        else:
            require(any(m.get('role') == 'tool' for m in messages), 'no real native tool response before text')
            delta = dict(role='assistant', content=content)
        ordinal = self.index+1
        self.index += 1  # Admission barrier: a malformed subsequent stream is not retried.
        record = dict(ordinal=ordinal, model=MODEL, tool=tool, tool_response_seen=tool is None)
        self.receipts.append(record)
        def chunk(delta, finish=None):
            return dict(id='local-completion-'+str(ordinal), object='chat.completion.chunk',
                created=1, model=MODEL, choices=[dict(index=0, delta=delta, finish_reason=finish)])
        chunks = [chunk(delta), chunk({}, 'tool_calls' if tool else 'stop')]
        return ''.join('data: '+json.dumps(value)+'\n\n' for value in chunks)+'data: [DONE]\n\n'

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            peer = writer.get_extra_info('peername')
            require(peer and peer[0] == '127.0.0.1', 'non-loopback peer')
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            require(len(header) <= 16384, 'oversized HTTP headers')
            lines = header.decode('ascii').split('\r\n')
            require(lines[0] == 'POST /v1/chat/completions HTTP/1.1', 'unexpected HTTP route')
            fields = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
            fields = {k.lower(): v.strip() for k, v in fields.items()}
            require(fields.get('authorization') == 'Bearer ato-local-dummy', 'dummy local credential required')
            require(fields.get('host') == '127.0.0.1:'+str(self.port), 'unexpected host')
            size = int(fields.get('content-length', '0'))
            require(0 < size <= MAX_REQUEST and 'transfer-encoding' not in fields, 'invalid bounded request')
            raw = await asyncio.wait_for(reader.readexactly(size), 5)
            body = json.loads(raw)
            self.validate_tool_result(body)
            stream = self.response(body).encode()
            self.receipts[-1]['request_sha256'] = sha(raw)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: close\r\nContent-Length: '+str(len(stream)).encode()+b'\r\n\r\n'+stream)
            await writer.drain()
        except Exception as exc:
            self.errors.append(type(exc).__name__)  # Never retain requests or headers.
            writer.write(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.handlers):
            task.cancel()
        if self.handlers:
            await asyncio.gather(*list(self.handlers), return_exceptions=True)


def _dispatch_api(repo, *args):
    """Invoke the canonical kernel/interaction Python APIs directly."""
    from ato_adapters import local_session_adapter
    from ato_workflow import interaction, workflow
    import runner

    command, *rest = args
    def flag(name, default=None):
        if name in rest:
            return rest[rest.index(name) + 1]
        return default

    if command == 'local-session-create':
        return local_session_adapter.create(
            flag('--project'), flag('--role'), flag('--source'),
            flag('--credential-file'), flag('--user-credential-file'))
    if command == 'local-session-grant':
        return local_session_adapter.grant(
            flag('--credential-file'), flag('--workflow'), flag('--output'), flag('--source'))
    if command == 'workflow-create':
        return str(workflow.create(flag('--repo'), flag('--plan'), origin_ref=flag('--origin-ref')))
    actor_file = flag('--actor-file')
    proof, reference, bound_workflow = (
        local_session_adapter.load_actor_file(actor_file) if actor_file else (None, None, None))
    if command == 'hub':
        return interaction.configure_session(flag('--session-ref'), flag('--source'), actor_proof=proof)
    if command == 'ask':
        return interaction.ask(rest[0], flag('--question'), '', '', None, actor_proof=proof)
    if command == 'inbox':
        return interaction.inbox(actor_proof=proof, reference=reference, workflow=bound_workflow)
    if command == 'read':
        return interaction.read_question(rest[0], actor_proof=proof, presented_source=flag('--presented-source'))
    if command == 'respond':
        return interaction.answer(rest[0], flag('--answer'), flag('--source'), actor_proof=proof)
    if command == 'accept-answer':
        return interaction.accept(rest[0], actor_proof=proof)
    if command == 'continue-task':
        workflow.Workflow(rest[0]).execute(continuation=(flag('--accepted-answer'), proof))
        return runner.status(rest[0])
    raise ValueError('unsupported local smoke command: ' + str(command))


async def cli(repo, *args, data=True):
    """Actual candidate API dispatch, with only temporary identities/configuration."""
    result = await asyncio.to_thread(_dispatch_api, repo, *args)
    return result if data else str(result)


def safe_events(host):
    """Native evidence, excluding prompts, response text, tool arguments and keys."""
    path = host.runtime/'events.jsonl'
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            row = json.loads(line)
            event = row['event']
            record = dict(sequence=row['seq'], event_id=event['event_id'], kind=event['kind'],
                correlation_id=event['correlation_id'], generation=event['generation'],
                turn_id=event['turn_id'], message_id=event['message_id'],
                native_session_id=event['session']['native_id'],
                project=event['session'].get('project'), adapter=event['session'].get('adapter'))
            if event['kind'] in ('tool_started', 'tool_completed'):
                try:
                    detail = json.loads(event['text'])
                    tool_id, status = detail['tool_id'], detail['status']
                    require(isinstance(tool_id, str) and len(tool_id) <= 256
                        and status in ('pending', 'in_progress', 'completed', 'failed'), 'invalid tool receipt')
                    record['tool'] = dict(tool_id=tool_id, status=status)
                except (ValueError, KeyError, TypeError):
                    record['tool'] = dict(tool_id=None, status='unknown')
            rows.append(record)
    return rows


def verified_turns(host, reference, expected_messages):
    """Cross-check retained native evidence against exact host admissions."""
    turns = list(host.state['turns'].values())
    require({turn['message_id'] for turn in turns} == set(expected_messages)
        and len(turns) == len(expected_messages)
        and len({turn['turn_id'] for turn in turns}) == len(turns)
        and all(turn['state'] == 'turn_completed'
            and turn['correlation_id'] == expected_messages[turn['message_id']] for turn in turns),
        'missing, duplicate or uncompleted exact native turn')
    events = safe_events(host)
    for turn in turns:
        correlated = [event for event in events if event['message_id'] == turn['message_id']]
        require(correlated and all(event['native_session_id'] == reference.identity.native_id
            and event['project'] == reference.identity.project and event['adapter'] == reference.identity.adapter
            and event['correlation_id'] == turn['correlation_id']
            and event['generation'] == turn['revision'] and event['turn_id'] == turn['turn_id']
            for event in correlated), 'native event session/message/turn/generation correlation mismatch')
        require(sum(event['kind'] == 'turn_completed' for event in correlated) == 1,
            'missing or duplicate native completion')
        tools = [event['tool'] for event in correlated if event['kind'] == 'tool_completed']
        require(tools and all(tool['status'] == 'completed' for tool in tools),
            'native file tool failed or has no successful completion')
    return turns, events


async def scenario(work, evidence):
    import agent_registry
    from ato_adapters import agents
    from ato_workflow import interaction
    from ato_adapters import local_session_adapter
    import recovery
    import runner
    from ato_workflow import session_router
    from ato_workflow import transport
    from ato_pi.pi_session_profile import LocalPiProfile

    repo = work/'repo'; repo.mkdir()
    subprocess.run(['/usr/bin/git', 'init', '-q', str(repo)], check=True, timeout=5)
    (repo/'seed.txt').write_text(SEED)
    model = await ScriptedModel().start()
    hosts = []
    receipts = []
    def receipt(step, **fields):
        receipts.append(dict(step=step, **fields))
        save(evidence/'steps.json', receipts)
    adapter = agent_registry.get('pi_acp')
    configuration = dict(providers={'ato-local': dict(baseUrl=f'http://127.0.0.1:{model.port}/v1',
        apiKey='ato-local-dummy', api='openai-completions', models=[dict(id=MODEL, name=MODEL,
            reasoning=False, input=['text'], cost=dict(input=0, output=0, cacheRead=0, cacheWrite=0),
            contextWindow=32768, maxTokens=1024)])})
    profile = LocalPiProfile(configuration, 'ato-local/'+MODEL)
    async def settle_native(projection, reference, correlation_id, cursor):
        events, _ = adapter.observe_session_v2(reference.identity, correlation_id, binding['generation'], cursor)
        settled = await asyncio.to_thread(transport.observe_managed, projection, events)
        require(settled['mailbox_state'] == 'turn_completed', 'exact native receipt did not settle canonical mailbox')
        receipt('canonical_turn_completed', mailbox_id=projection['mailbox_id'],
            correlation_id=correlation_id, native_session_id=reference.identity.native_id,
            generation=binding['generation'], mailbox_state=settled['mailbox_state'])
    try:
        host, reference = await adapter.create_managed_session(profile, repo, sandbox='workspace-write', timeout=25)
        hosts.append(host)
        await host.listen()
        require(adapter.verify_session(reference.identity.native_id, str(repo)), 'native identity not verified')
        receipt('native_created', native_session_id=reference.identity.native_id,
            reference=reference.reference, provenance=host.state['provenance_data'])
        hub_root, user_root = work/'hub-root.json', work/'user-root.json'
        hub = await cli(repo, 'local-session-create', '--project', repo, '--role', 'hub',
            '--credential-file', hub_root, '--user-credential-file', user_root,
            '--source', 'synthetic local smoke hub; no live user/session binding')
        await cli(repo, 'hub', '--session-ref', hub['reference'], '--source', 'synthetic local smoke hub',
            '--actor-file', hub_root)
        plan = work/'plan.json'
        runner.save(plan, dict(objective='Synthetic local-only Pi tool and neutral human roundtrip with independent checks',
            scope='temporary fixture files only', authorization='synthetic smoke; no business or production authority',
            memory=False, acceptance=[dict(id='artifact', description='independent exact artifact check',
                checks=[[sys.executable, '-c', "from pathlib import Path; assert Path('result.txt').read_text() == 'ato-local-artifact\\n'"]])],
            limits=dict(runtime_seconds=30, attempt_seconds=10, check_seconds=5, max_attempts=1,
                max_stages=1, max_planning_attempts=1, max_wait_seconds=1)))
        directory = Path(await cli(repo, 'workflow-create', '--repo', repo, '--plan', plan,
            '--origin-ref', reference.reference, data=False))
        binding = host.bind_workflow(directory)
        state_before = (directory/'state.json').read_bytes()
        async def origin_grant(current, ref, name):
            proof = adapter.origin_actor_proof(ref.identity, source='synthetic existing local smoke authority', **binding)
            file = work/name
            local_session_adapter.private_create(file, dict(version=2, reference=ref.reference,
                workflow_id=str(directory), proof=asdict(proof)))
            return file
        origin_file = await origin_grant(host, reference, 'origin.json')
        hub_file, user_file = work/'hub.json', work/'user.json'
        for root, target in ((hub_root, hub_file), (user_root, user_file)):
            await cli(repo, 'local-session-grant', '--credential-file', root, '--workflow', directory,
                '--output', target, '--source', 'synthetic local-only workflow grant')
        first_canonical = 'local-first-'+uuid.uuid4().hex
        first_cursor = adapter.session_cursor_v2(reference.identity)
        projection = transport.prepare_managed(reference.reference, 'Read seed.txt for the first explicit local turn.',
            canonical_id=first_canonical, phase='local_smoke', task_id=str(directory),
            contract_hash=binding['contract_hash'], generation=recovery.generation(directory, runner.read(directory/'state.json')),
            project=repo, source='synthetic local-only foreground smoke')
        dispatched = await asyncio.to_thread(transport.dispatch_managed, projection)
        require(dispatched['mailbox_state'] == 'delivered', 'first turn admission not confirmed; no replay')
        await host.drain()
        require(host.state['lifecycle'] == 'idle', 'first native turn did not complete')
        await settle_native(projection, reference, first_canonical, first_cursor)
        async def question_roundtrip(origin_key, question, answer):
            q = await cli(repo, 'ask', directory, '--question', question, '--actor-file', origin_key)
            inbox = await cli(repo, 'inbox', '--actor-file', hub_file)
            require(any(row['id'] == q['id'] for row in inbox), 'canonical inbox mismatch')
            shown = await cli(repo, 'read', q['id'], '--actor-file', hub_file,
                '--presented-source', 'synthetic test display, not real user presentation')
            require(shown['question'] == question, 'actual question display mismatch')
            origin_ref, _ = session_router.resolve_v2(q['origin_thread'], project=str(repo))
            cursor = adapter.session_cursor_v2(origin_ref.identity)
            answered = await cli(repo, 'respond', q['id'], '--answer', answer,
                '--source', 'scripted synthetic user fixture; no real authorization', '--actor-file', user_file)
            require(answered['answer_actor']['role'] == 'user', 'model output was used as user authority')
            await hosts[-1].drain()
            require(hosts[-1].state['lifecycle'] == 'idle', 'answer native turn did not complete')
            await settle_native(answered['answer_delivery'], origin_ref, q['id'], cursor)
            accepted = await cli(repo, 'accept-answer', q['id'], '--actor-file', origin_key)
            require(accepted['state'] == 'accepted' and accepted['accepted_actor']['role'] == 'origin', 'origin acceptance mismatch')
            receipt('question_roundtrip', question_id=q['id'], origin=accepted['accepted_actor']['session']['reference'],
                answer_source='synthetic_user_fixture', workflow_accepted=False)
            return accepted
        q1 = await question_roundtrip(origin_file, 'Synthetic fixture: write the exact local artifact?',
            'Write result.txt using the bounded file tool; synthetic fixture only.')
        require((repo/'result.txt').read_text() == ARTIFACT, 'real file tool artifact mismatch')
        turns, events = verified_turns(host, reference,
            {projection['mailbox_id']: first_canonical, q1['answer_delivery']['mailbox_id']: q1['id']})
        require((directory/'state.json').read_bytes() == state_before, 'agent completion mutated workflow acceptance')
        save(evidence/'native-events-before-restart.json', events)
        receipt('two_native_turns', native_session_id=reference.identity.native_id,
            turns=[dict(message_id=t['message_id'], turn_id=t['turn_id']) for t in turns],
            artifact_sha256=sha((repo/'result.txt').read_bytes()), workflow_accepted=False)
        proof = agent_registry.ActorProof('pi-host-token', host.host_id, host.token)
        actor = session_router.authenticate_actor(reference.reference, proof, role='session_host',
            project=str(repo), **binding)
        checkpoint = dict(full_goal=runner.read(directory/'contract.json')['objective'],
            current_files=[dict(path='result.txt', sha256=sha((repo/'result.txt').read_bytes()))],
            previous_results=[dict(message_id=t['message_id'], turn_id=t['turn_id'], state=t['state']) for t in turns],
            cumulative_history=['Synthetic native turns 1 and 2 completed; original attempts and pending criteria retained'])
        old_question = interaction.question_path(q1['id']).read_bytes()
        old_pid = host.proc.pid
        await host.close()
        newhost, newref = await adapter.reconstruct_session(reference, checkpoint, profile, actor=actor, **binding)
        hosts.append(newhost)
        await newhost.listen()
        require(newref.identity.native_id != reference.identity.native_id and newhost.proc.pid != old_pid,
            'reconstruction did not create a new exact native process/session')
        require(newhost.state['recovery']['checkpoint'] == checkpoint, 'reconstruction lost checkpoint')
        require(interaction.question_path(q1['id']).read_bytes() == old_question, 'historical question rebound')
        new_origin = await origin_grant(newhost, newref, 'successor-origin.json')
        q2 = await question_roundtrip(new_origin, 'Synthetic fixture: verify the artifact after reconstruction?',
            'Read result.txt and verify the retained artifact; no replay of previous write.')
        _, restart_events = verified_turns(newhost, newref, {q2['answer_delivery']['mailbox_id']: q2['id']})
        require(not newhost.state['workflow_completed_by_end_turn'], 'native completion authorized workflow')
        require(runner.read(directory/'state.json')['criteria']['artifact']['status'] == 'pending', 'criterion auto-completed')
        result = await cli(repo, 'continue-task', directory, '--accepted-answer', q2['id'], '--actor-file', new_origin)
        require(result['status'] == 'completed' and result['criteria']['artifact']['status'] == 'passed'
            and result['criteria']['artifact']['evidence'] and result['planning_attempts'] == 0,
            'independent foreground checks did not complete the synthetic workflow')
        subprocess.run([sys.executable, '-c',
            "from pathlib import Path; assert Path('result.txt').read_text() == 'ato-local-artifact\\n'"],
            cwd=repo, env=dict(os.environ), check=True, timeout=5)
        require(model.index == 6 and not model.errors, 'unexpected model traffic/errors')
        save(evidence/'native-events-after-restart.json', restart_events)
        save(evidence/'artifact.json', dict(path='result.txt', sha256=sha((repo/'result.txt').read_bytes()),
            exact_content_verified=True, independent_subprocess_check=True, configuration_ephemeral=True))
        receipt('independent_acceptance', criterion='artifact', status='passed',
            previous_native_session_id=reference.identity.native_id, native_session_id=newref.identity.native_id,
            restart_mode='checkpoint_reconstruction', original_question_retained=True)
        provenance = host.state['provenance_data']
        return dict(kind='actual_pi_cli_with_scripted_loopback_model', codex_disabled=True,
            isolated_configuration=True, identity_verified=True, session_created=True, two_turns_correlated=True,
            question_answer_roundtrip=True, tool_observed=True, artifact_verified=True, independent_checks_passed=True,
            restart_verified=True, no_external_calls=True, restart_mode='checkpoint_reconstruction',
            workflow_completed_by_end_turn=False, native_session_id=reference.identity.native_id,
            successor_native_session_id=newref.identity.native_id, evidence=str(evidence),
            pi_version=provenance['pi_version'], pi_binary_sha256=provenance['pi_binary_sha256'],
            adapter_sha256=provenance['adapter_sha256'], cloud_model_qualified=False,
            actual_user_authorization=False, ephemeral_paths=True, model_request_count=model.index)
    finally:
        # Retain safe traffic receipts even when a later assertion fails. Never
        # retain raw HTTP/model/native configuration or retry an admitted turn.
        save(evidence/'model-receipts.json', dict(requests=model.receipts, errors=model.errors))
        try:
            for host in reversed(hosts):
                await host.close()
        finally:
            await model.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--local-only', required=True, action='store_true')
    args = parser.parse_args()
    evidence = ROOT/'build/session-decoupling/real-pi-local'/uuid.uuid4().hex
    evidence.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='ato-real-pi-local-') as folder:
            work = Path(folder).resolve()
            env = dict(HOME=str(work/'home'), ATO_HOME=str(work/'ato'), CODEX_HOME=str(work/'unavailable-codex'),
                PATH='/usr/bin:/bin', CTASK_CODEX_BIN='/codex-disabled', CTASK_QUEUE_BIN='/codex-disabled',
                PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=codex_probes(work))
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, env, clear=True))
                attempts = stack.enter_context(disabled_codex())
                # All candidate CLI subprocesses inherit an empty inaccessible Codex
                # namespace and failing binaries; model/native environments are fresh.
                proof = asyncio.run(asyncio.wait_for(scenario(work, evidence), timeout=210))
                require(not attempts, 'Codex fail probe was reached; smoke is not vendor independent')
        proof['elapsed_seconds'] = round(time.monotonic()-started, 3)
        save(evidence/'proof.json', proof)
        save(args.output.resolve(), proof)
        print(json.dumps(dict(kind=proof['kind'], evidence=str(evidence), cloud_model_qualified=False)))
        return 0
    except Exception as exc:
        save(evidence/'failure.json', dict(status='failed', error_type=type(exc).__name__,
            reason=str(exc)[:400],
            elapsed_seconds=round(time.monotonic()-started, 3), cloud_model_qualified=False))
        print('local-only Pi qualification failed: '+type(exc).__name__+'; safe evidence: '+str(evidence), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
