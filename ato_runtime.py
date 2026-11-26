"""Private subprocess bootstrap for a source-selected unified ATO runtime."""
import hmac
import json
import os
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ato_common import ato_config
from ato_task import ato_process
from ato_task.ato_execution import run_child
from ato_task.ato_host import Host, host_record
from ato_task.ato_private import read_handoff, render_notice
from ato_release.seal import runtime_seal
from ato_task.ato_task_store import TaskError, TaskStore, _fields, _identifier, _require, _sha, _strict_json


def runtime_hooks(store):
    return Host(store=store).compose(render_notice=render_notice).execution.hooks


def pipe_payload(variable):
    raw = os.environ.pop(variable, "")
    _require(raw.isascii() and raw.isdecimal(), "FORBIDDEN")
    pipe = int(raw)
    _require(pipe >= 3 and stat.S_ISFIFO(os.fstat(pipe).st_mode), "FORBIDDEN")
    with os.fdopen(pipe, "rb") as stream:
        message = stream.read(65537)
    _require(len(message) <= 65536, "FORBIDDEN")
    return _strict_json(message, "FORBIDDEN")


def host_intent(verb, *, fields=()):
    capability = pipe_payload("ATO_OBSERVER_PIPE" if verb == "observe" else "ATO_PRIVATE_HOST_PIPE")
    _fields(capability, ("parent", "birth", "seal", *fields), code="FORBIDDEN")
    _require(capability["parent"] == os.getppid()
             and ato_process.disposition(capability["parent"], capability["birth"]) == "live", "FORBIDDEN")
    seal = runtime_seal(required=True, expected=capability["seal"])
    return seal, capability


def main(argv):
    try:
        match argv:
            case ["run-child", task_id, run_id]:
                capability = pipe_payload("ATO_PRIVATE_PIPE_FD")
                _fields(capability, ("task_id", "run_id", "nonce", "gate", "seal"), code="FORBIDDEN")
                roots = _strict_json(os.environ.pop("ATO_PRIVATE_ROOTS", "").encode(), "FORBIDDEN")
                _fields(roots, ("home", "native_home", "native_homes"), code="FORBIDDEN")
                store = TaskStore(**roots, runtime_seal=capability["seal"])
                run_child(store, task_id, run_id, capability, runtime_hooks(store))
            case ["read-handoff", ref]:
                try:
                    host = Host(seal=runtime_seal(required=True), read_only=True).compose(render_notice=render_notice)
                    try:
                        result = {"valid": True, "data": read_handoff(host, host.actor_file(), ref)}
                    finally:
                        host.close_mailbox_snapshot()
                except Exception:
                    result = dict(valid=False, action="end_silently", reason="stale_or_unavailable")
                print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            case ["observe"]:
                from ato_task.ato_observer import observe
                seal, launch = host_intent("observe", fields=("journal", "nonce"))
                path = Path(launch["journal"])
                journal = host_record(path, path.parent, code="FORBIDDEN")
                key = journal.get("observer_starting")
                _require(key in {"new_observer", "rollback_observer"} and hmac.compare_digest(
                             journal.get("observer_nonce_sha256", ""), _sha(launch["nonce"].encode())),
                         "FORBIDDEN")
                selected = journal["target"] if key == "new_observer" else journal["before"]["observer_source"]
                recorded = journal[key]
                _require(recorded["pid"] == os.getpid()
                         and ato_process.disposition(recorded["pid"], recorded["pid_identity"]) == "live"
                         and selected["seal"] == seal
                         and journal["paths"]["ato_home"] == str(ato_config.unified_home())
                         and journal["paths"]["codex_home"] == str(ato_config.codex_home()), "FORBIDDEN")
                observe(seal)
            case ["supervise", task_id, run_id]:
                _identifier(task_id, "task")
                _identifier(run_id, "run")
                seal, proof = host_intent("supervise", fields=("action", "task_id", "run_id"))
                _require((proof["action"], proof["task_id"], proof["run_id"]) == ("supervise", task_id, run_id),
                         "FORBIDDEN")
                host = Host(seal=seal).compose(render_notice=render_notice)
                host.execution.reconcile(host.actor_file(), task_id, expected_run=run_id)
            case ["resume-run", task_id]:
                _identifier(task_id, "task")
                seal, proof = host_intent("resume-run", fields=("action", "task_id", "run_id"))
                _require((proof["action"], proof["task_id"]) == ("resume-run", task_id), "FORBIDDEN")
                expected_run = _identifier(proof["run_id"], "run")
                host = Host(seal=seal).compose(render_notice=render_notice)
                actor = host.actor_file()
                host.execution.resume(actor, task_id, expected_run=expected_run)
            case _:
                return 2
        return 0
    except TaskError as exc:
        return exc.exit_code
    except Exception:
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
