"""Trusted synthetic profile bootstrap, not a production executable override."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import agents
import agent_registry
import acpx_profile
import acpx_worker


class FixtureAdapter(acpx_worker.AcpxCodexAdapter):
    kind = 'offline_acpx_fixture'

    def command(self, request):
        argv = super().command(request)
        argv[1:2] = [str(Path(__file__).resolve()), '_invoke']
        return argv


def fixture_profile(repo, scratch, authority, mode):
    pins = acpx_profile.dependencies()
    return dict(**pins, agent='synthetic', final_parser='codex-1.1.5',
        argv=[str(acpx_profile.NODE), str(Path(__file__).with_name('acpx_fixture_agent.mjs'))],
        agent_env=dict(HOME=str(scratch), CODEX_HOME=str(scratch/'native'),
                       PATH='/usr/bin:/bin', TMPDIR=str(scratch)),
        required_mode='agent' if mode == 'workspace-write' else 'read-only')


agent_registry.register(FixtureAdapter())
# This test-only bootstrap exercises production Runner policy with a synthetic
# transport. It cannot be selected through production task/env configuration.
agent_registry.get('acpx_codex').command = FixtureAdapter().command
agent_registry.set_runtime_entrypoint(__file__)

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_invoke':
        parser = argparse.ArgumentParser()
        for name in ('directory', 'label', 'schema', 'output', 'sandbox'):
            parser.add_argument('--'+name, required=True)
        try:
            acpx_worker.invoke(parser.parse_args(sys.argv[2:]), sys.stdin.read(), profile_factory=fixture_profile)
        except Exception as exc:
            print('fixture bridge failed: '+type(exc).__name__, file=sys.stderr)
            sys.exit(1)
    else:
        import runner
        sys.exit(runner.main())
