"""Code-owned ACP Pi profile. Selected only by agent.kind=acp_pi.

The profile never accepts a command, environment, or import path from a plan.
It fails closed when the pinned source, optional SDK, or OS sandbox is absent.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))

from ato_common import ato_core as runner
from ato_pi.pi_toolchain import (ExecutableLocator, clean_environment, prepare_toolchain,
                          write_launch)

PI = ExecutableLocator('pi')
NODE = ExecutableLocator('node')
SANDBOX = Path('/usr/bin/sandbox-exec')

def main():
    parser = argparse.ArgumentParser()
    for name in ('directory', 'label', 'schema', 'output', 'sandbox'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--model')
    args = parser.parse_args()
    try:
        invoke(args)
    except Exception as exc:
        hint = ''
        try:
            diagnostic = Path(args.directory).resolve()/'checkpoints'/(args.label+'.acp-runtime')/'stderr.log'
            if diagnostic.is_file():
                hint = f' (inspect private runtime stderr at {diagnostic})'
        except (OSError, ValueError, RuntimeError):
            pass  # A failed diagnostic lookup must not expose the original exception chain.
        print('ACP Pi worker failed: '+type(exc).__name__+hint, file=sys.stderr)
        return 1
    return 0


def invoke(args):
    from ato_adapters import agents
    from ato_adapters import acp_bridge
    directory = Path(args.directory).resolve()
    repo = Path(runner.read(directory/'state.json')['repo']).resolve()
    if args.sandbox not in ('read-only', 'workspace-write'):
        raise ValueError('unsupported sandbox')
    if not SANDBOX.is_file():
        raise ValueError('OS sandbox unavailable')
    if Path(args.output).resolve().parent != directory/'checkpoints':
        raise ValueError('output must belong to scheduler checkpoints')
    # Use a new, private, per-attempt directory; never reuse old session state.
    runtime = directory/'checkpoints'/(args.label+'.acp-runtime')
    if runtime.is_relative_to(repo) and not runtime.is_relative_to(repo/'.codex/tasks'):
        raise ValueError('worker runtime requires protected scheduler namespace')
    runtime.mkdir(mode=0o700)
    (runtime/'tmp').mkdir(mode=0o700)
    tools = prepare_toolchain(repo, runtime, node=NODE, pi=PI)
    derived = runtime/'adapter'
    subprocess.run([sys.executable, str(ROOT/'scripts/derive_pi_acp.py'), str(derived)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=clean_environment(home=runtime/'tmp'))
    launcher = write_launch(runtime, repo, 'worker', tools)
    profile = runtime/'sandbox.sb'
    # Unlike the legacy Pi path, the ACP profile grants no write access to the
    # user's native agent directory. Native auth configuration is read-only.
    profile.write_text(agents.sandbox_profile(repo, runtime, args.sandbox))
    env = dict(clean_environment(home=Path.home()), CTASK_PI_ACP_STATE_DIR=str(runtime/'state'),
               PI_ACP_PI_COMMAND=str(launcher),
               CTASK_AGENT_REPO=str(repo), CTASK_AGENT_SANDBOX=args.sandbox,
               CTASK_PI_RUNTIME=str(runtime), TMPDIR=str(runtime/'tmp'),
               JITI_CACHE=str(runtime/'jiti'), PI_TELEMETRY='0')
    native = Path(os.environ.get('PI_CODING_AGENT_DIR', str(Path.home()/'.pi/agent'))).resolve()
    if native.is_relative_to(repo) or native.is_relative_to(runtime):
        raise ValueError('native auth configuration may not come from workspace or runtime')
    env['PI_CODING_AGENT_DIR'] = str(native)
    if args.model:
        env['CTASK_PI_MODEL'] = args.model
    command = [str(SANDBOX), '-f', str(profile), str(tools.node), str(derived/'pi-acp.mjs')]
    acp_bridge.main(command, [str(directory), args.label, args.schema, args.output],
                    environment=env, native_result_only=True, stderr_path=runtime/'stderr.log')

if __name__ == '__main__':
    raise SystemExit(main())
