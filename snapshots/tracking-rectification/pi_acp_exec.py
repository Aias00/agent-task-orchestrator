#!/usr/bin/env python3
"""Fixed Pi launcher called only by the isolated ACP profile."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
repo = os.environ['CTASK_AGENT_REPO']
runtime = os.environ['CTASK_PI_RUNTIME']
mode = os.environ['CTASK_AGENT_SANDBOX']
tools = 'read,grep,find,ls' if mode == 'read-only' else 'read,grep,find,ls,edit,write'
command = ['OPERATOR_HOME/.local/bin/pi', '--offline', '--no-extensions', '--no-skills',
           '--no-mcp', '--no-context-files', '--no-prompt-templates', '--no-themes',
           '--no-approve', '--tools', tools, '--extension', str(ROOT/'pi-path-guard.mjs'),
           '--session-dir', str(Path(runtime)/'sessions')]
if os.environ.get('CTASK_PI_MODEL'):
    command += ['--model', os.environ['CTASK_PI_MODEL']]
os.execv(command[0], command+sys.argv[1:])
