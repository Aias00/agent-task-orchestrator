"""Trusted offline bootstrap; never installed or used by production profiles."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ato_adapters import agents
import agent_registry
from ato_adapters.acp_bridge import ACPAdapter

agent_registry.register(ACPAdapter('offline_acp_fixture', __file__))
agent_registry.register(ACPAdapter('offline_pi_acp_fixture', __file__))
agent_registry.set_runtime_entrypoint(__file__)

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_acp_bridge':
        from ato_adapters import acp_bridge
        try:
            import os
            label = sys.argv[3]
            directory = Path(sys.argv[2])
            # Profile choice comes from the scheduler's immutable plan. Both are
            # deliberately fixture-only and never installed as production agents.
            plan = __import__('json').loads((directory/'plan.json').read_text())
            if plan['agent']['kind'] == 'offline_pi_acp_fixture':
                command = [os.environ.get('CTASK_NODE_BIN', 'node'), os.environ['CTASK_FIXTURE_DERIVED_PI_ACP']]
                environment = {key: os.environ[key] for key in ('CTASK_PI_ACP_STATE_DIR', 'PI_ACP_PI_COMMAND')}
            else:
                command = [sys.executable, str(Path(__file__).with_name('acp_fixture_agent.py'))]
                environment = None
            acp_bridge.main(command, sys.argv[2:], environment=environment,
                            native_result_only=plan['agent']['kind'] == 'offline_pi_acp_fixture')
        except Exception as exc:
            print(type(exc).__name__, file=sys.stderr)
            raise SystemExit(1)
    else:
        import runner
        from legacy_cli import main
        raise SystemExit(main(runner))
