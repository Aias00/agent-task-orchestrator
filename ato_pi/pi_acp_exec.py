#!/usr/bin/env python3
"""Fixed native worker launcher; toolchain evidence is not task authority."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))

from ato_pi.pi_toolchain import load_launch, native_arguments, native_environment


def main():
    runtime = Path(os.environ['CTASK_PI_RUNTIME']).resolve(strict=True)
    project = Path(os.environ['CTASK_AGENT_REPO']).resolve(strict=True)
    mode = os.environ['CTASK_AGENT_SANDBOX']
    if mode not in ('read-only', 'workspace-write'):
        raise ValueError('invalid worker policy')
    args = native_arguments(runtime, sys.argv[1:])
    tools = load_launch(runtime, project, 'worker')
    names = 'read,grep,find,ls' if mode == 'read-only' else 'read,grep,find,ls,edit,write'
    command = [str(tools.node), str(tools.pi), '--offline', '--no-extensions', '--no-skills',
        '--no-mcp', '--no-context-files', '--no-prompt-templates', '--no-themes',
        '--no-approve', '--tools', names, '--extension', str(ROOT/'pi-path-guard.mjs'),
        '--session-dir', str(runtime/'sessions')]
    if os.environ.get('CTASK_PI_MODEL'):
        command += ['--model', os.environ['CTASK_PI_MODEL']]
    os.execve(command[0], command+args, native_environment())


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Pi native worker failed: '+type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
