"""ATO-owned one-attempt worker using a replaceable in-process acpx bridge."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import acpx_profile

MAX_OUTPUT = 1024 * 1024


class AcpxCodexAdapter:
    kind = 'acpx_codex'
    capabilities = frozenset({'worker', 'acp_process'})

    @property
    def session_capabilities_v2(self):
        import agent_registry
        return {name: {'state': 'unsupported', 'evidence':
            'bounded fresh ACP worker; no coordinator identity, planner or native continuation'}
            for name in agent_registry.SESSION_CAPABILITY_NAMES}

    def command(self, request):
        acpx_profile.validate_request(request)
        argv = [sys.executable, str(Path(__file__).resolve())]
        for name in ('directory', 'label', 'schema', 'output', 'sandbox'):
            argv += ['--'+name, str(getattr(request, name))]
        return argv


def validate_transport(payload, correlation):
    if not isinstance(payload, dict) or payload.get('version') != 1 or payload.get('correlation') != correlation:
        raise ValueError('invalid bridge correlation/envelope')
    if (payload.get('protocol') != 'completed' or payload.get('stop_reason') != 'end_turn'
            or payload.get('delivery') != 'finished' or payload.get('transport') != 'response'
            or payload.get('backend_outcome') != 'unknown' or payload.get('business_validation') != 'unknown'):
        raise ValueError('bridge lacks unambiguous completed transport')


def decode(payload, correlation, schema):
    """No prose extraction, fences, duplicate keys, EOF or transport guessing."""
    import agents
    validate_transport(payload, correlation)
    raw = payload.get('text')
    if not isinstance(raw, str) or len(raw.encode()) > MAX_OUTPUT:
        raise ValueError('missing/oversized final result')
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('ambiguous duplicate result key')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    return agents.validate_result(value, schema)


def invoke(args, prompt, *, profile_factory=acpx_profile.codex_profile):
    """Trusted fixture code may inject a profile; production CLI cannot."""
    import agents
    import agent_registry
    import runner
    import task_trace
    directory = Path(args.directory).resolve()
    match = re.fullmatch(r'([A-Za-z0-9_-]+)-attempt-([1-9][0-9]*)-([0-9a-f]{6,32})', args.label)
    if not match:
        raise ValueError('invalid ATO attempt label')
    task, attempt = match.group(1), int(match.group(2))
    plan = runner.read(directory/'plan.json')
    state = runner.read(directory/'state.json')
    if (runner.digest(plan) != state['plan_hash'] or (directory/'STOP').exists()
            or state['tasks'][task].get('attempt_sequence') != attempt
            or state['tasks'][task].get('status') != 'running'):
        raise ValueError('inactive or changed ATO attempt')
    if (Path(args.schema).resolve() != directory/'result-schema.json'
            or Path(args.output).resolve() != directory/'checkpoints'/(args.label+'.json')
            or Path(args.output).exists() or Path(args.output).is_symlink()):
        raise ValueError('result/schema must belong to this fresh ATO attempt')
    request = agent_registry.ExecutionRequest(plan, directory, args.label, args.schema,
                                              args.output, args.sandbox, False)
    acpx_profile.validate_request(request)
    if args.sandbox != plan.get('sandbox', 'workspace-write'):
        raise ValueError('worker sandbox differs from frozen plan')
    repo = Path(state['repo']).resolve()
    schema = runner.read(args.schema)
    runtime = directory/'checkpoints'/(args.label+'.acpx-runtime')
    runtime.mkdir(mode=0o700)
    # Native scratch is outside authority so the child cannot write bridge input,
    # schema, output, plan, trace or any sibling attempt/checkpoint.
    import tempfile
    # A workflow stage is the canonical Runner run, not another transport task.
    correlation = dict(run=directory.name, stage=directory.name, task=task,
                       attempt=attempt, turn=uuid.uuid4().hex)
    if (directory/'parent.json').is_file():
        parent = runner.read(directory/'parent.json')
        workflow_dir = Path(parent['workflow_dir']).resolve(strict=True)
        if workflow_dir.parent != repo/'.codex/tasks/workflows':
            raise ValueError('invalid canonical workflow correlation')
        correlation['workflow'] = workflow_dir.name
        correlation['workflow_dir'] = str(workflow_dir)
    trace_context = dict(task=task, attempt=attempt, turn=correlation['turn'], source='acp')
    code = 1
    transport_validated = False
    try:
        with tempfile.TemporaryDirectory(prefix='ato-acpx-agent-') as scratch:
            import ato_config
            # Git metadata/instructions remain readable. Native :workspace
            # protects their writes; only ATO authority requires read denial.
            authority = [directory, repo/'.codex', acpx_profile.ROOT]
            # Resolve paths only, without opening any native/global config.
            # Protect the compatibility fallback as well as explicit ATO_HOME.
            authority.extend([ato_config.ato_home(), ato_config.codex_home(), Path.home()/'.codex'])
            cfg = profile_factory(repo, Path(scratch), authority, args.sandbox)
            cfg.update(version=1, correlation=correlation, cwd=str(repo),
                       stop_file=str(directory/'STOP'), model=plan.get('agent', {}).get('model'),
                       timeout_ms=max(1, min(int(plan['limits']['attempt_seconds']*1000)-250, 300000)),
                       text=prompt+'\nReturn exactly one JSON object matching this schema:\n'+json.dumps(schema))
            # No credentials, native config or protocol stderr inherited/logged.
            env = dict(PATH='/usr/bin:/bin', HOME=scratch, CODEX_HOME=str(Path(scratch)/'native'),
                       TMPDIR=scratch)
            process = subprocess.Popen([cfg.get('node', str(acpx_profile.NODE)), str(Path(__file__).with_name('acpx_bridge.mjs'))],
                cwd=repo, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                raw, _ = process.communicate(json.dumps(cfg).encode(),
                                            timeout=cfg['timeout_ms']/1000+8)
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
                raise
            code = process.returncode
        if len(raw) > MAX_OUTPUT*2:
            raise ValueError('bridge output bound exceeded')
        payload = json.loads(raw)
        if code or (directory/'STOP').exists():
            diagnostic = payload.get('diagnostic')
            if (isinstance(diagnostic, dict) and set(diagnostic) == {'phase', 'code', 'dispatch_state'}
                    and diagnostic['phase'] in {'setup','ensure_session','mode','turn','final','cleanup','configuration'}
                    and diagnostic['code'] in {'unknown','ACP_BACKEND_MISSING','ACP_BACKEND_UNAVAILABLE',
                        'ACP_BACKEND_UNSUPPORTED_CONTROL','ACP_DISPATCH_DISABLED','ACP_INVALID_RUNTIME_OPTION',
                        'ACP_SESSION_INIT_FAILED','ACP_TURN_FAILED'}
                    and diagnostic['dispatch_state'] in {'not_started','unknown'}):
                # Diagnostic is non-authoritative: unknown delivery still stays
                # blocked, and this never supplies a completed transport receipt.
                runner.save(runtime/'failure.json', dict(version=1, correlation=correlation,
                    protocol='failed', delivery='unknown', diagnostic=diagnostic))
            raise ValueError('acpx turn failed or authority revoked')
        validate_transport(payload, correlation)
        transport_validated = True
        receipt = {key: payload[key] for key in
            ('version', 'correlation', 'protocol', 'stop_reason', 'delivery', 'transport',
             'backend_outcome', 'business_validation', 'session', 'observations', 'capabilities')}
        receipt['result_schema_valid'] = False
        receipt['process_group'] = os.getpgrp()
        runner.save(runtime/'receipt.json', receipt)
        for event in payload.get('metadata', [])[:64]:
            if event.get('type') == 'tool':
                task_trace.append(directory, 'tool', event_id=args.label+':tool:'+str(event.get('sequence')),
                    state=event.get('state', 'unknown'), tool=event.get('tool'), **trace_context)
        task_trace.append(directory, 'transport_completed', event_id=args.label+':transport',
                          state='completed', **trace_context)
        task_trace.append(directory, 'native_unverified', event_id=args.label+':native',
                          state='unknown', **trace_context)
        value = decode(payload, correlation, schema)
        receipt['result_schema_valid'] = True
        runner.save(runtime/'receipt.json', receipt)
        # The model never receives the checkpoint path and cannot write it under
        # the child sandbox. Only this trusted parent publishes validated data.
        runner.save(args.output, value)
    except Exception as error:
        if isinstance(error, acpx_profile.ProfileUnavailable):
            # This fixed preflight class is raised before Node/prompt dispatch.
            runner.save(runtime/'receipt.json', dict(version=1, correlation=correlation,
                protocol='failed', delivery='unsent', transport='not_dispatched',
                backend_outcome='unknown', business_validation='unknown', result_schema_valid=False))
        if not transport_validated:
            task_trace.append(directory, 'transport_error', event_id=args.label+':error',
                              state='unknown', **trace_context)
        raise


def main():
    parser = argparse.ArgumentParser()
    for name in ('directory', 'label', 'schema', 'output', 'sandbox'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    # Bound input before any process can be admitted.
    prompt = sys.stdin.read(MAX_OUTPUT+1)
    if len(prompt.encode()) > MAX_OUTPUT:
        raise ValueError('prompt exceeds worker bound')
    invoke(args, prompt)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, acpx_profile.ProfileUnavailable) else type(exc).__name__
        print('acpx worker refused/failed: '+detail+'; inspect ATO attempt; no automatic replay', file=sys.stderr)
        sys.exit(1)
