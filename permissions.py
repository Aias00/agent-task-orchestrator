"""Explicit Codex worker capabilities; legacy plans keep their policy."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile


def validate(plan):
    mode = plan.get('sandbox', 'workspace-write')
    if mode not in ('read-only', 'workspace-write', 'danger-full-access'):
        raise ValueError('unsupported sandbox')
    grant = plan.get('permissions', {})
    if not isinstance(grant, dict) or set(grant) - {'source', 'network_access', 'writable_roots', 'requirements'}:
        raise ValueError('invalid permissions fields')
    import agents
    adapter = agents.registry.get(plan.get('agent', {}).get('kind', 'codex'))
    if 'explicit_permissions' not in adapter.capabilities and (grant or mode == 'danger-full-access'):
        raise ValueError('explicit capabilities/full access currently require the Codex adapter')
    if 'network_access' in grant and type(grant['network_access']) is not bool:
        raise ValueError('permissions.network_access must be boolean')
    roots = grant.get('writable_roots', [])
    if not isinstance(roots, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in roots):
        raise ValueError('writable_roots must be absolute directory paths')
    if any(not Path(p).is_dir() for p in roots):
        raise ValueError('writable roots must exist as directories before execution')
    if mode == 'read-only' and (roots or grant.get('network_access')):
        raise ValueError('read-only plans cannot request worker write/network grants')
    if mode == 'danger-full-access' and ('network_access' in grant or roots):
        raise ValueError('full access cannot promise narrower network or filesystem restrictions')
    if grant or mode == 'danger-full-access':
        if not isinstance(grant.get('source'), str) or not grant['source'].strip():
            raise ValueError('explicit worker permissions require their authorization source')
    required = grant.get('requirements', [])
    if not isinstance(required, list) or any(k not in ('loopback', 'unix_socket', 'browser') for k in required):
        raise ValueError('requirements must select loopback, unix_socket, browser')
    handoff = plan.get('host_handoff')
    if handoff is not None and (not isinstance(handoff, dict) or set(handoff) != {'source', 'instructions'}
            or any(not isinstance(v, str) or not v.strip() for v in handoff.values())):
        raise ValueError('host_handoff requires source and bounded instructions')
    import execution_policy
    execution_policy.validate(plan, adapter.capabilities)


def codex_args(plan, sandbox=None):
    validate(plan)
    if sandbox is not None:  # A read-only planner never inherits worker write grants.
        return []
    grant = plan.get('permissions', {})
    args = []
    if 'network_access' in grant:
        args += ['-c', 'sandbox_workspace_write.network_access=' + str(grant['network_access']).lower()]
    if 'writable_roots' in grant:
        roots = [str(Path(p).resolve(strict=True)) for p in grant['writable_roots']]
        if any(not Path(p).is_dir() for p in roots):
            raise ValueError('writable roots must exist as directories before execution')
        args += ['-c', 'sandbox_workspace_write.writable_roots=' + json.dumps(roots)]
    return args


def describe(plan):
    result = dict(sandbox=plan.get('sandbox', 'workspace-write'), **plan.get('permissions', {}))
    if 'execution_policy' in plan:
        result['execution_policy'] = plan['execution_policy']
    return result


def probe_argv(plan, repo):
    """Probe capabilities through Codex's native sandbox, without a model call.

    The sandbox subcommand uses a named equivalent profile. Its report is NOT
    a claim about a future exec worker or an inherited administrator policy.
    """
    validate(plan)
    grant = plan.get('permissions', {})
    mode = plan.get('sandbox', 'workspace-write')
    profile = {'read-only': ':read-only', 'danger-full-access': ':danger-full-access'}.get(mode, 'ctask-check')
    argv = [os.environ.get('CTASK_CODEX_BIN', 'codex'), 'sandbox', '--permission-profile', profile,
            '--include-managed-config', '-C', str(Path(repo).resolve())]
    if mode == 'workspace-write':
        argv += ['-c', 'permissions.ctask-check.extends=":workspace"', '-c',
                 'permissions.ctask-check.network.enabled=' + str(grant.get('network_access', False)).lower()]
        for root in grant.get('writable_roots', []):
            directory = str(Path(root).resolve(strict=True))
            argv += ['-c', 'permissions.ctask-check.workspace_roots.' + json.dumps(directory) + '=true']
    spec = dict(requirements=grant.get('requirements', []), roots=grant.get('writable_roots', []))
    return argv + ['--', sys.executable, str(Path(__file__).resolve()), json.dumps(spec)]


def probe(spec):
    results = {}
    for name in spec['requirements']:
        try:
            if name == 'loopback':
                with socket.socket() as server:
                    server.settimeout(3)
                    server.bind(('127.0.0.1', 0)); server.listen(1)
                    with socket.create_connection(server.getsockname(), timeout=3) as client:
                        conn, _ = server.accept()
                        with conn:
                            client.sendall(b'ctask'); assert conn.recv(5) == b'ctask'
            elif name == 'unix_socket':
                with tempfile.TemporaryDirectory(prefix='ctask-socket-') as folder:
                    with socket.socket(socket.AF_UNIX) as server:
                        server.bind(str(Path(folder)/'probe.sock')); server.listen(1)
            elif name == 'browser':
                script = """const {chromium}=require('playwright');
                (async()=>{const b=await chromium.launch({headless:true});
                try{const p=await b.newPage();await p.setContent('<title>ctask</title>');
                if(await p.title()!=='ctask')throw Error('browser failed');}finally{await b.close();}
                })().catch(e=>{console.error(e.message);process.exitCode=1});"""
                result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=30)
                if result.returncode:
                    raise RuntimeError(result.stderr[-1500:])
            results[name] = {'passed': True}
        except (OSError, RuntimeError, AssertionError, subprocess.TimeoutExpired) as exc:
            results[name] = {'passed': False, 'error': str(exc)[:2000]}
    for root in spec['roots']:
        try:
            with tempfile.TemporaryFile(dir=root) as handle:
                handle.write(b'ctask'); handle.flush()
            results['write:' + root] = {'passed': True}
        except OSError as exc:
            results['write:' + root] = {'passed': False, 'error': str(exc)}
    passed = all(r['passed'] for r in results.values())
    print(json.dumps(dict(passed=passed, probe_kind='native-sandbox-profile', results=results)))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(probe(json.loads(sys.argv[1])))
