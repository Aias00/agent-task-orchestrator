#!/usr/bin/env python3
"""Private, fixed native Pi coordinator launcher; no task-supplied argv."""
import os
from pathlib import Path
import sys


def main():
    from pi_session_profile import ROOT, PI, NODE
    runtime = Path(os.environ['CTASK_PI_RUNTIME']).resolve(strict=True)
    mode = os.environ['CTASK_AGENT_SANDBOX']
    if mode not in ('read-only', 'workspace-write'):
        raise ValueError('invalid coordinator policy')
    # Only the pinned ACP subprocess may request rpc or the owned session file.
    args = sys.argv[1:]
    if args[:3] != ['--mode', 'rpc', '--no-themes']:
        raise ValueError('unsupported native invocation')
    if len(args) != 3:
        if len(args) != 5 or args[3] != '--session':
            raise ValueError('unsupported native arguments')
        path = Path(args[4]).resolve(strict=True)
        if not path.is_relative_to(runtime/'sessions'):
            raise ValueError('session file outside owned runtime')
    tools = 'read' if mode == 'read-only' else 'read,edit,write'
    command = [str(NODE), str(PI.resolve()), '--offline', '--no-extensions', '--no-skills',
        '--no-mcp', '--no-context-files', '--no-prompt-templates', '--no-themes', '--no-approve',
        '--tools', tools, '--extension', str(ROOT/'pi-coordinator-guard.mjs'),
        '--session-dir', str(runtime/'sessions'), '--model', os.environ['CTASK_PI_MODEL']]
    os.execve(command[0], command+args, dict(os.environ))


if __name__ == '__main__':
    main()
