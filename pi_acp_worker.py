"""Code-owned ACP Pi profile. Selected only by agent.kind=acp_pi.

The profile never accepts a command, environment, or import path from a plan.
It fails closed when the pinned source, optional SDK, or OS sandbox is absent.
"""
import ato_core as runner
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PI = Path.home()/'.local/bin/pi'
NODE = Path.home()/'.nvm/versions/node/v24.22.1/bin/node'
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
    import agents
    import acp_bridge
    directory = Path(args.directory).resolve()
    repo = Path(runner.read(directory/'state.json')['repo']).resolve()
    if args.sandbox not in ('read-only', 'workspace-write'):
        raise ValueError('unsupported sandbox')
    if not SANDBOX.is_file() or not PI.is_file() or not NODE.is_file():
        raise ValueError('pinned Pi, Node or OS sandbox unavailable')
    if Path(args.output).resolve().parent != directory/'checkpoints':
        raise ValueError('output must belong to scheduler checkpoints')
    # Use a new, private, per-attempt directory; never reuse old session state.
    runtime = directory/'checkpoints'/(args.label+'.acp-runtime')
    runtime.mkdir(mode=0o700)
    (runtime/'tmp').mkdir(mode=0o700)
    derived = runtime/'adapter'
    subprocess.run([sys.executable, str(ROOT/'scripts/derive_pi_acp.py'), str(derived)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    profile = runtime/'sandbox.sb'
    # Unlike the legacy Pi path, the ACP profile grants no write access to the
    # user's native agent directory. Native auth configuration is read-only.
    profile.write_text(agents.sandbox_profile(repo, runtime, args.sandbox))
    env = dict(CTASK_PI_ACP_STATE_DIR=str(runtime/'state'),
               PI_ACP_PI_COMMAND=str(ROOT/'pi_acp_exec.py'),
               CTASK_AGENT_REPO=str(repo), CTASK_AGENT_SANDBOX=args.sandbox,
               CTASK_PI_RUNTIME=str(runtime), TMPDIR=str(runtime/'tmp'),
               JITI_CACHE=str(runtime/'jiti'), PI_TELEMETRY='0')
    if args.model:
        env['CTASK_PI_MODEL'] = args.model
    command = [str(SANDBOX), '-f', str(profile), str(NODE), str(derived/'pi-acp.mjs')]
    acp_bridge.main(command, [str(directory), args.label, args.schema, args.output],
                    environment=env, native_result_only=True, stderr_path=runtime/'stderr.log')

if __name__ == '__main__':
    raise SystemExit(main())
