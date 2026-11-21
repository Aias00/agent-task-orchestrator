#!/usr/bin/env python3
"""Private native coordinator launcher; no task-supplied argv or tool paths."""
import os
from pathlib import Path
import sys

from pi_toolchain import load_launch, native_arguments, native_environment

ROOT = Path(__file__).resolve().parent


def main():
    runtime = Path(os.environ['CTASK_PI_RUNTIME']).resolve(strict=True)
    project = Path(os.environ['CTASK_AGENT_REPO']).resolve(strict=True)
    mode = os.environ['CTASK_AGENT_SANDBOX']
    if mode not in ('read-only', 'workspace-write'):
        raise ValueError('invalid coordinator policy')
    args = native_arguments(runtime, sys.argv[1:])
    tools = load_launch(runtime, project, 'coordinator')
    names = 'read' if mode == 'read-only' else 'read,edit,write'
    command = [str(tools.node), str(tools.pi), '--offline', '--no-extensions', '--no-skills',
        '--no-mcp', '--no-context-files', '--no-prompt-templates', '--no-themes', '--no-approve',
        '--tools', names, '--extension', str(ROOT/'pi-coordinator-guard.mjs'),
        '--session-dir', str(runtime/'sessions'), '--model', os.environ['CTASK_PI_MODEL']]
    os.execve(command[0], command+args, native_environment())


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Pi native coordinator failed: '+type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
