"""CLI worker adapters. Codex remains the default; external CLIs are bounded workers."""
import ato_core as core
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import agent_registry as registry
from codex_session_adapter import CodexSessionAdapter

KINDS = ('codex', 'claude', 'pi')
SENSITIVE_PATHS = ('.git', '.codex', '.ato', '.pi', '.aws', '.ssh', '.env')
MANAGED = Path.home()/'.local/share/ctask-agent-clis/node_modules/.bin'


def validate(agent):
    if agent is None:
        return
    if (not isinstance(agent, dict) or set(agent)-{'kind', 'model'}
            or agent.get('kind') not in registry.kinds()):
        raise ValueError('agent requires a registered kind and optional model')
    if 'model' in agent and (not isinstance(agent['model'], str) or not agent['model'].strip()
                             or agent['model'].startswith('-')):
        raise ValueError('agent model must be a nonempty model identifier')


def executable(kind):
    override = os.environ.get('CTASK_'+kind.upper()+'_BIN')
    candidate = override or shutil.which(kind) or str(MANAGED/kind)
    found = shutil.which(candidate)
    if not found:
        raise ValueError(kind+' CLI not installed; install it and authenticate separately')
    return str(Path(found).absolute())


def doctor():
    rows = []
    for kind in KINDS:
        row = dict(kind=kind, installed=False, authentication='not checked; version is not proof of authentication')
        capabilities = registry.get(kind).capabilities
        row['capabilities'] = sorted(capabilities)
        row['native_resume'] = 'supported' if 'native_resume' in capabilities else 'unsupported'
        row['session_capabilities'] = {entry.name: entry.state.value
            for entry in registry.session_capabilities(kind).entries}
        row['readiness'] = 'unknown; installation and authentication are separate checks'
        try:
            binary = executable(kind)
            result = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=15)
            row.update(installed=result.returncode == 0, executable=binary,
                       version=(result.stdout.strip().split('\n')[0])[:120])
            if kind == 'claude' and row['installed']:
                auth = subprocess.run([binary, 'auth', 'status'], capture_output=True, text=True, timeout=15)
                try:
                    data = json.loads(auth.stdout)
                    row['authentication'] = {k: data.get(k) for k in ('loggedIn', 'authMethod', 'apiProvider')}
                except ValueError:
                    row['authentication'] = 'status unavailable; no credentials printed'
            elif kind == 'pi':
                folder = Path(os.environ.get('PI_CODING_AGENT_DIR', str(Path.home()/'.pi/agent')))
                row['authentication'] = dict(provider_required=True,
                    native_auth_file_present=(folder/'auth.json').is_file(),
                    common_api_environment_present=any(os.environ.get(k) for k in (
                        'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY', 'OPENROUTER_API_KEY')),
                    validity='not verified; configure a provider/model then run an isolated task')
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            row['error'] = str(exc)
        if kind == 'codex':
            row['worker_guard'] = 'native'
        elif Path('/usr/bin/sandbox-exec').exists():
            native = '~/.claude and ~/.claude.json' if kind == 'claude' else 'the configured Pi native agent directory'
            row['worker_guard'] = ('macOS sandbox-exec; file tools only; legacy CLI metadata/auth directory '
                +native+' remains OS-writable for login/cache compatibility; acp_pi keeps native credentials read-only')
        else:
            row['worker_guard'] = 'unavailable; external execution refused'
        rows.append(row)
    return rows


def command(plan, directory, label, schema, output, sandbox=None):
    """Same entry for stage worker and read-only workflow planner."""
    cfg = plan.get('agent', {'kind': 'codex'})
    validate(cfg)
    kind = cfg['kind']
    import permissions
    permissions.validate(plan)
    mode = sandbox or plan.get('sandbox', 'workspace-write')
    request = registry.ExecutionRequest(plan, directory, label, schema, output, mode,
                                        sandbox is not None)
    return registry.get(kind, 'planner' if request.planner else 'worker').command(request)


class _CodexAdapter(CodexSessionAdapter):
    kind = 'codex'
    capabilities = (frozenset({'worker', 'planner', 'legacy_model', 'explicit_permissions', 'progress'})
                    | CodexSessionAdapter.session_capabilities)

    def progress_path(self, directory, label, log):
        return log

    def normalize_progress(self, row):
        import agent_native_events
        return agent_native_events.codex(row)

    def command(self, request):
        import permissions
        plan, mode = request.plan, request.sandbox
        schema, output = request.schema, request.output
        model = plan.get('agent', {}).get('model') or plan.get('model')
        argv = [os.environ.get('CTASK_CODEX_BIN', 'codex'), 'exec', '--sandbox', mode,
                '-c', 'approval_policy="never"', '--json', '--output-schema', str(schema), '-o', str(output)]
        if model:
            argv += ['--model', model]
        argv += permissions.codex_args(plan, mode if request.planner else None)
        return argv+['-']


class _FileWorkerAdapter:
    capabilities = frozenset({'worker', 'planner', 'progress'})

    def progress_path(self, directory, label, log):
        return Path(directory)/'checkpoints'/(label+'.agent-runtime')/'stdout.jsonl'

    def normalize_progress(self, row):
        import agent_native_events
        return {'claude': agent_native_events.claude, 'pi': agent_native_events.pi}[self.kind](row)

    def __init__(self, kind):
        self.kind = kind
        reason = f'{kind} adapter implements bounded worker/planner execution only'
        self.session_capabilities_v2 = {
            name: {'state': 'unsupported', 'evidence': reason}
            for name in registry.SESSION_CAPABILITY_NAMES
        }

    def command(self, request):
        argv = [sys.executable, str(Path(__file__).resolve()), '_invoke', '--kind', self.kind,
                '--directory', str(request.directory), '--label', request.label,
                '--schema', str(request.schema), '--output', str(request.output),
                '--sandbox', request.sandbox]
        model = request.plan.get('agent', {}).get('model')
        if model:
            argv += ['--model', model]
        return argv


registry.register(_CodexAdapter())
registry.register(_FileWorkerAdapter('claude'))
registry.register(_FileWorkerAdapter('pi'))


class _PiACPAdapter:
    """Explicit opt-in profile; no plan-controlled executable or environment."""
    kind = 'acp_pi'
    capabilities = frozenset({'worker', 'planner', 'acp_process'})
    session_capabilities_v2 = {
        name: {'state': 'unsupported', 'evidence': 'ACP profile is a bounded worker, not a managed coordinator session'}
        for name in registry.SESSION_CAPABILITY_NAMES
    }

    def command(self, request):
        argv = [sys.executable, str(Path(__file__).with_name('pi_acp_worker.py')),
                '--directory', str(request.directory), '--label', request.label,
                '--schema', str(request.schema), '--output', str(request.output),
                '--sandbox', request.sandbox]
        model = request.plan.get('agent', {}).get('model')
        if model:
            argv += ['--model', model]
        return argv


registry.register(_PiACPAdapter())

# The ACP worker is deliberately distinct from this foreground coordinator.
# Importing/registering it starts no CLI, socket, provider or background job.
from pi_session_coordinator import PiSessionAdapter
registry.register(PiSessionAdapter())

# Passive human/origin CLI endpoints use the same router and durable mailbox.
# Registration performs no I/O and provisions no identities or credentials.
from local_session_adapter import LocalCLIAdapter
registry.register(LocalCLIAdapter())


def validate_result(result, schema):
    if not isinstance(result, dict) or set(result) != set(schema['required']):
        raise ValueError('result must match all required fields exactly')
    for key, spec in schema['properties'].items():
        value = result[key]
        types = {'string': str, 'integer': int, 'array': list}
        if type(value) is not types[spec['type']]:
            raise ValueError('wrong result type: '+key)
        if 'enum' in spec and value not in spec['enum']:
            raise ValueError('invalid result enum: '+key)
    return result


def decode(kind, path):
    records = []
    if Path(path).stat().st_size > 64*1024*1024:
        raise ValueError('protocol output exceeds 64 MiB')
    with Path(path).open() as handle:
        for line in handle:
            if len(line) > 8*1024*1024:
                raise ValueError('oversized protocol record')
            if line.strip():
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError('protocol record must be an object')
                records.append(item)
    if kind == 'claude':
        terminal = [r for r in records if r.get('type') == 'result']
        if not terminal or terminal[-1].get('is_error') or terminal[-1].get('subtype') != 'success':
            raise ValueError('Claude did not return a successful terminal result')
        result = terminal[-1]
        value = result.get('structured_output')
        if not isinstance(value, dict):
            raise ValueError('Claude terminal result lacks structured_output')
        return value, dict(session_id=result.get('session_id'), usage=result.get('usage'),
                           total_cost_usd=result.get('total_cost_usd'))
    if not any(r.get('type') in ('agent_end', 'agent_settled') for r in records):
        raise ValueError('Pi stream has no terminal lifecycle record')
    settled = [r for r in records if r.get('type') == 'agent_settled']
    if settled and settled[-1].get('aborted'):
        raise ValueError('Pi was aborted')
    messages = [r['message'] for r in records if r.get('type') == 'message_end'
                and isinstance(r.get('message'), dict) and r['message'].get('role') == 'assistant']
    if not messages or messages[-1].get('stopReason') not in ('stop', 'length'):
        raise ValueError('Pi final assistant response missing or failed')
    message = messages[-1]
    if message.get('stopReason') != 'stop':
        raise ValueError('Pi result truncated')
    content = ''.join(c.get('text', '') for c in message.get('content', []) if c.get('type') == 'text')
    session = next((r for r in records if r.get('type') == 'session'), {})
    return json.loads(content), dict(session_id=session.get('id'), usage=message.get('usage'))


def sandbox_profile(repo, runtime, mode, kind=None):
    quote = lambda p: json.dumps(str(Path(p).resolve()))
    # Network is needed by the CLI's model client. No shell, web, MCP or extension
    # tool is offered to the model. Host checks run outside this worker policy.
    rules = ['(version 1)', '(allow default)', '(deny file-write*)',
             '(allow file-write* (literal "/dev/null") (subpath '+quote(runtime)+'))']
    # Trusted CLI metadata is writable, but model tools cannot target it:
    # Claude --restricted and the mandatory Pi path guard enforce workspace
    # paths. No credentials are copied into task records or invocation arguments.
    if kind == 'claude':
        rules.append('(allow file-write* (subpath '+quote(Path.home()/'.claude')+') '
                     '(literal '+quote(Path.home()/'.claude.json')+') '
                     '(subpath '+quote(Path('/tmp')/('claude-'+str(os.getuid())))+'))')
    elif kind == 'pi':
        folder = Path(os.environ.get('PI_CODING_AGENT_DIR', str(Path.home()/'.pi/agent')))
        rules.append('(allow file-write* (subpath '+quote(folder)+'))')
    if mode == 'workspace-write':
        exclusions = ' '.join('(require-not (subpath '+quote(repo/name)+'))' for name in SENSITIVE_PATHS)
        # Also exclude nested sensitive segments and their physical symlink targets.
        pattern = '^'+re.escape(str(Path(repo).resolve()))+'/([^/]+/)*('+'|'.join(map(re.escape, SENSITIVE_PATHS))+')(/|$)'
        rules.append('(allow file-write* (require-all (subpath '+quote(repo)+') '+exclusions+
                     ' (require-not (regex '+json.dumps(pattern)+'))))')
    return '\n'.join(rules)+'\n'


def invoke(args):
    directory = Path(args.directory).resolve()
    repo = Path(core.read(directory/'state.json')['repo']).resolve()
    output = Path(args.output).resolve()
    if output.parent != directory/'checkpoints':
        raise ValueError('adapter output must be in its scheduler checkpoints')
    if args.sandbox not in ('read-only', 'workspace-write'):
        raise ValueError('unsupported sandbox')
    if not Path('/usr/bin/sandbox-exec').is_file():
        raise ValueError('external CLI workers require macOS sandbox-exec; no unsandboxed fallback')
    binary = executable(args.kind)
    runtime = directory/'checkpoints'/(args.label+'.agent-runtime')
    runtime.mkdir(mode=0o700)
    (runtime/'tmp').mkdir()
    profile = runtime/'sandbox.sb'
    profile.write_text(sandbox_profile(repo, runtime, args.sandbox, args.kind))
    schema = core.read(args.schema)
    prompt = sys.stdin.read()+'\nFinal response must be one JSON object matching this schema exactly:\n'+json.dumps(schema)
    readonly = args.sandbox == 'read-only'
    if args.kind == 'claude':
        tools = 'Read,Glob,Grep' if readonly else 'Read,Glob,Grep,Edit,Write'
        argv = [binary, '--safe-mode', '--restricted', '--no-session-persistence',
                '--disable-slash-commands', '-p', '--output-format', 'stream-json', '--verbose',
                '--permission-mode', 'dontAsk', '--tools', tools, '--allowedTools', tools,
                '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--session-id', str(uuid.uuid4()), '--json-schema', json.dumps(schema)]
        # Worker session persistence stays isolated; never copies credentials.
        env = dict(os.environ, TMPDIR=str(runtime/'tmp'),
                   CLAUDE_CODE_TMPDIR=str(runtime/'tmp'), CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
    else:
        tools = 'read,grep,find,ls' if readonly else 'read,grep,find,ls,edit,write'
        argv = [binary, '--mode', 'json', '--print', '--tools', tools,
                '--no-extensions', '--no-skills', '--no-prompt-templates', '--no-themes',
                '--no-mcp', '--no-context-files', '--no-approve', '--offline',
                '--extension', str(Path(__file__).with_name('pi-path-guard.mjs')),
                '--session', str(runtime/'session.jsonl')]
        env = dict(os.environ, TMPDIR=str(runtime/'tmp'),
                   JITI_CACHE=str(runtime/'jiti'), PI_TELEMETRY='0',
                   CTASK_AGENT_REPO=str(repo), CTASK_AGENT_SANDBOX=args.sandbox)
    if args.model:
        argv += ['--model', args.model]
    # A transcript records exact native identity/config/result metadata, not keys.
    core.save(runtime/'invocation.json', dict(kind=args.kind, argv=argv, cwd=str(repo),
              sandbox=args.sandbox, tools=tools.split(','), started_at=core.now(),
              native_resume='recorded only; scheduler retries use fresh sessions with full checkpoints'))
    raw = runtime/'stdout.jsonl'
    with (runtime/'input.txt').open('w') as h:
        h.write(prompt)
    with (runtime/'input.txt').open('rb') as inp, raw.open('wb') as out, (runtime/'stderr.log').open('wb') as err:
        # Keep the same process group as runner.command so STOP/timeout reaps
        # the CLI and every descendant, including when this bridge is killed.
        proc = subprocess.Popen(['/usr/bin/sandbox-exec', '-f', str(profile), *argv],
                                cwd=repo, env=env, stdin=inp, stdout=out, stderr=err)
        print(json.dumps({'event': 'agent_started', 'kind': args.kind, 'pid': proc.pid,
                          'evidence': str(runtime)}), flush=True)
        code = proc.wait()
    # Missing authentication is a prerequisite blocker, not an implementation
    # failure to be retried until the attempt budget is consumed.
    stderr = (runtime/'stderr.log').read_text(errors='replace')[-8000:]
    unauthenticated = ('No API key found' in stderr)
    if args.kind == 'claude':
        with raw.open() as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get('type') == 'result' and item.get('is_error'):
                    unauthenticated |= 'Not logged in' in str(item.get('result', ''))
    if unauthenticated:
        reason = args.kind+' authentication is unavailable; configure native login or a provider API key, then resume this task'
        if 'status' in schema['properties']:
            value = dict(status='blocked', summary=reason, blocker=reason,
                         next_step='Authenticate outside the worker, then resume --retry-failed', retry_after_seconds=1)
        else:
            value = dict(decision='blocked', reason=reason, tasks=[], retry_after_seconds=1)
        core.save(output, validate_result(value, schema))
        core.save(runtime/'result-metadata.json', dict(kind=args.kind, finished_at=core.now(), blocked='authentication'))
        print(json.dumps({'event': 'agent_blocked', 'kind': args.kind, 'reason': reason}), flush=True)
        return
    if code:
        raise ValueError(f'{args.kind} CLI exited {code}; inspect private adapter stderr/stream at {runtime}; authentication or runtime may be unavailable')
    value, metadata = decode(args.kind, raw)
    validate_result(value, schema)
    core.save(output, value)
    core.save(runtime/'result-metadata.json', dict(kind=args.kind, finished_at=core.now(), **metadata))
    print(json.dumps({'event': 'agent_result', 'kind': args.kind, 'result': value}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['_invoke'])
    parser.add_argument('--kind', choices=['claude', 'pi'], required=True)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--schema', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--sandbox', required=True)
    parser.add_argument('--model')
    try:
        invoke(parser.parse_args())
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
