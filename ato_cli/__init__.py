"""Strict parsing and JSON results for the public ATO interface."""
import argparse
import json
import sys

from ato_task_store import TaskError, _identifier, result_envelope


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def error(self, message):
        raise TaskError("USAGE")


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if hasattr(namespace, self.dest):
            parser.error("repeated option")
        setattr(namespace, self.dest, True if self.nargs == 0 else values)


def option(parser, name, *, required=False, flag=False):
    parser.add_argument("--" + name, action=Once, default=argparse.SUPPRESS,
                        required=required, **({"nargs": 0} if flag else {}))


def build_parser():
    parser = Parser(prog="ato", description="Submit and manage durable agent tasks.")
    daily = parser.add_subparsers(dest="operation", required=True)

    def command(commands, name, description, identity=None):
        selected = commands.add_parser(name, help=description, description=description)
        if identity:
            selected.add_argument(identity)
        return selected

    def spec(selected):
        option(selected, "repo", required=True)
        option(selected, "spec", required=True)

    run = command(daily, "run", "Submit a frozen task specification.")
    spec(run)
    option(run, "foreground", flag=True)
    option(command(daily, "list", "List visible tasks."), "repo")
    command(daily, "status", "Read task and execution status.", "task_id")
    command(daily, "stop", "Request durable STOP.", "task_id")
    command(daily, "resume", "Resume an eligible task.", "task_id")
    command(daily, "inbox", "Read pending human decisions.")
    option(command(daily, "answer", "Record a user's answer.", "request_id"), "answer", required=True)
    option(command(daily, "accept", "Record manual acceptance of a task.", "task_id"), "evidence")

    project = command(daily, "project", "Repository and worktree operations.")
    project = project.add_subparsers(dest="subcommand", required=True)
    spec(command(project, "validate", "Preview a normalized specification."))
    command(project, "trace", "Read a bounded task timeline.", "task_id")
    option(command(project, "recheck", "Recheck the existing product.", "task_id"), "checks-only", flag=True)
    option(command(project, "cleanup", "Retain accepted changes and remove an eligible worktree.", "task_id"),
           "confirm", required=True, flag=True)

    agent = command(daily, "agent", "Capabilities, ownership and technical review.")
    agent = agent.add_subparsers(dest="subcommand", required=True)
    option(command(agent, "doctor", "Inspect configured local adapter capabilities."), "probe-versions", flag=True)
    command(agent, "binding", "Read the verified task binding.", "task_id")
    handoff = command(agent, "handoff", "Record an authorized controller handoff.", "task_id")
    option(handoff, "to", required=True)
    option(handoff, "interaction")
    option(handoff, "evidence", required=True)
    reviewer = command(agent, "reviewer", "Select an authorized technical reviewer.", "task_id")
    option(reviewer, "kind", required=True)
    option(reviewer, "model")
    option(reviewer, "effort")
    option(reviewer, "evidence", required=True)
    ask = command(agent, "ask", "Record a scoped question for the actual user.", "task_id")
    for name in ("kind", "question", "reason"):
        option(ask, name, required=True)
    option(ask, "continuation")
    option(ask, "purpose-ref")

    system = command(daily, "system", "Read-only runtime diagnostics.")
    system = system.add_subparsers(dest="subcommand", required=True)
    command(system, "status", "Read release, observer and mailbox status.")
    option(command(system, "notification-policy", "Read effective notification policy."), "repo")
    return parser


def main(argv=None, *, application=None):
    try:
        args = build_parser().parse_args(argv)
        for field in ("task", "request"):
            if hasattr(args, field + "_id"):
                _identifier(getattr(args, field + "_id"), field)
        if application is None:
            from ato_application import Application
            application = Application()
        data = application.dispatch(args)
        result, code = result_envelope(data=data), 0
        output = json.dumps(result, ensure_ascii=False, allow_nan=False)
        output.encode("utf-8")
    except TaskError as exc:
        result, code = result_envelope(error=exc), exc.exit_code
        output = json.dumps(result)
    except KeyboardInterrupt:
        error = TaskError("STOPPED")
        output, code = json.dumps(result_envelope(error=error)), error.exit_code
    except Exception:
        error = TaskError("INTERNAL_ERROR")
        output, code = json.dumps(result_envelope(error=error)), error.exit_code
    print(output)
    return code
