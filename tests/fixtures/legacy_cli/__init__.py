"""Domain CLI dispatch. Importing or asking for help starts no runtime."""
import subprocess
import sys

from .parser import build_parser
from . import workflow_cmd
from . import budget_cmd
from . import control_cmd
from . import session_cmd
from . import interaction_cmd
from . import recovery_cmd
from . import run_cmd

_HANDLERS = {
    '_controller': control_cmd.handle,
    '_dispatcher': session_cmd.handle,
    '_supervisor': recovery_cmd.handle,
    '_worker': run_cmd.handle,
    'accept-answer': interaction_cmd.handle,
    'acknowledge': workflow_cmd.handle,
    'agent-doctor': session_cmd.handle,
    'answer': interaction_cmd.handle,
    'ask': interaction_cmd.handle,
    'budget-charge': budget_cmd.handle,
    'budget-configure': budget_cmd.handle,
    'budget-estimate': budget_cmd.handle,
    'budget-policy': budget_cmd.handle,
    'budget-reforecast': budget_cmd.handle,
    'budget-run': budget_cmd.handle,
    'continue-task': interaction_cmd.handle,
    'control': control_cmd.handle,
    'create': run_cmd.handle,
    'delegate': session_cmd.handle,
    'delegation-accept': session_cmd.handle,
    'delegation-status': session_cmd.handle,
    'failure-alerts': control_cmd.handle,
    'followup-link': control_cmd.handle,
    'followups': control_cmd.handle,
    'host-checks': workflow_cmd.handle,
    'host-recheck': workflow_cmd.handle,
    'hub': interaction_cmd.handle,
    'inbox': interaction_cmd.handle,
    'list': run_cmd.handle,
    'local-session-create': session_cmd.handle,
    'local-session-grant': session_cmd.handle,
    'notification-policy': control_cmd.handle,
    'operational-disposition': control_cmd.handle,
    'permission-check': run_cmd.handle,
    'presented': control_cmd.handle,
    'read': interaction_cmd.handle,
    'read-question': interaction_cmd.handle,
    'respond': interaction_cmd.handle,
    'resume': run_cmd.handle,
    'run': run_cmd.handle,
    'session-register': session_cmd.handle,
    'start': run_cmd.handle,
    'status': run_cmd.handle,
    'stop': run_cmd.handle,
    'supervise': recovery_cmd.handle,
    'takeover': recovery_cmd.handle,
    'takeover-status': recovery_cmd.handle,
    'trace': run_cmd.handle,
    'unsupervise': recovery_cmd.handle,
    'validate': run_cmd.handle,
    'watch': control_cmd.handle,
    'workflow-close': workflow_cmd.handle,
    'workflow-create': workflow_cmd.handle,
    'workflow-link': workflow_cmd.handle,
    'workflow-validate': workflow_cmd.handle,
}


def main(core):
    args = build_parser(core.__doc__).parse_args()
    try:
        return _HANDLERS[args.command](args, core)
    except (core.Halt, ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
