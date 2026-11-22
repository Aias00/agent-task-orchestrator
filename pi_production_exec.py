#!/usr/bin/env python3
"""Pinned production native exec; no credential in argv, disk or diagnostics."""
import json
import os
from pathlib import Path
import resource
import sys

from pi_toolchain import ROOT, load_launch, native_arguments, native_environment


def environment():
    env = native_environment()
    names = ('ATO_PI_PROVIDER_KEY', 'ATO_PI_PROXY_PORT', 'ATO_PI_CONTROL_SOCKET', 'ATO_PI_PROFILE_HASH')
    if any(not os.environ.get(name) for name in names):
        raise ValueError('production environment incomplete')
    for name in names:
        env[name] = os.environ[name]
    env['PI_OFFLINE'] = '1'
    return env


def main():
    runtime = Path(os.environ['CTASK_PI_RUNTIME']).resolve(strict=True)
    project = Path(os.environ['CTASK_AGENT_REPO']).resolve(strict=True)
    args = native_arguments(runtime, sys.argv[1:])
    # No native load: ACP may not supply --session even if upstream advertises it.
    if len(args) != 3:
        raise ValueError('production native load unsupported')
    tools = load_launch(runtime, project, 'coordinator')
    record = json.loads((runtime/'trusted/toolchain.json').read_text())
    if record.get('profile_id') != 'production-pi-v1':
        raise ValueError('production identity required')
    mode = os.environ['CTASK_AGENT_SANDBOX']
    if mode not in ('read-only', 'workspace-write'):
        raise ValueError('production tool mode rejected')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    command = [str(tools.node), str(tools.pi), '--offline', '--no-extensions', '--no-skills',
        '--no-mcp', '--no-context-files', '--no-prompt-templates', '--no-themes', '--no-approve',
        '--tools', 'read' if mode == 'read-only' else 'read,edit,write',
        '--extension', str(ROOT/'pi-coordinator-guard.mjs'),
        '--extension', str(ROOT/'pi_production_transport.mjs'),
        '--session-dir', str(runtime/'sessions'), '--model', os.environ['CTASK_PI_MODEL']]
    os.execve(command[0], command+args, environment())


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Production Pi launch rejected', file=sys.stderr)
        raise SystemExit(1)
