#!/usr/bin/env python3
"""Trusted local operator setup; no services, native sessions or model calls."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_registry import SessionIdentity
import ato_config
from ato_host import ACCEPTANCE_NOTIFICATIONS, Host
from ato_execution_host import HostProfile
from ato_seal import installed_boundary, runtime_seal
from ato_task_store import (
    TaskError, _fields, _identifier, _repo_boundaries, _require, _save, _sha, _strict_json, _text,
    normalize_repository, read_input_file,
)
from codex_session_adapter import CodexSessionAdapter
from message_bus import EndpointCredential, IdentityError, MessageBus

OPERATIONS = {
    "controller": ("observe", "initial", "stop", "resume", "recheck", "reconcile",
                   "trace", "cleanup", "binding", "ask", "read_answer", "handoff", "reviewer"),
    "interaction": ("observe", "binding", "read", "present"),
    "user": ("observe", "read", "answer", "accept", "handoff", "reviewer", "stop"),
}


def record(host, name, value):
    with host.store._private(host.folder, create=True) as fd:
        _save(fd, name + ".json", value, exclusive=True)


class _EndpointRegistration(MessageBus):
    """Register the pair with the existing mailbox API in one transaction."""

    def __init__(self, connection, clock):
        self.connection = connection
        self.clock = clock

    @contextmanager
    def _transaction(self):
        yield self.connection


def _registered_acceptance_endpoints(bus, connection, row):
    if row is None:
        return False
    credentials = [EndpointCredential(**row[key]) for key in ("sender", "target")]
    found = []
    for credential in credentials:
        try:
            bus._endpoint(connection, credential.endpoint_id)
        except IdentityError:
            continue
        found.append(bus._authenticate(connection, credential))
    if not found:
        # A crash can leave the private record ahead of SQLite's commit.
        return False
    _require([(ep["kind"], ep["state"]) for ep in found] == [("local_cli", "active"), ("codex", "active")])
    return True


def bind_acceptance(host, args):
    repository = normalize_repository(args.repo)
    _repo_boundaries(repository, host.store.home, host.store.native_homes, selected=args.repo)
    project = Path(args.session_project).resolve(strict=True)
    _require(project.is_dir(), "INVALID_REPO")
    _require(host.record("observation", {}).get("enabled") is True and host.acceptance_queue_binary(),
             "UNSUPPORTED_CAPABILITY")
    with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
        _require(CodexSessionAdapter().verify_session(args.session_id, str(project)), "FORBIDDEN")
    bus = host.mailbox()
    _require(bus is not None, "UNSUPPORTED_CAPABILITY")
    with host.store._private(host.folder, create=True) as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TaskError("BUSY") from None
        row = host.acceptance_notifications()
        if row is not None:
            _require((row["native_home"], row["target"]["session_id"], row["target"]["project"])
                     == (str(host.store.native_home), args.session_id, str(project)), "DECISION_CONFLICT")
        with bus._transaction() as connection:
            if not _registered_acceptance_endpoints(bus, connection, row):
                registrar = _EndpointRegistration(connection, bus.clock)
                source_ref = row["source_ref"] if row else "ato-acceptance-" + secrets.token_hex(16)
                sender = registrar.register_endpoint(source_ref + "-sender", kind="local_cli",
                    session_id=source_ref, project=project, source=args.source)
                target = registrar.register_endpoint(source_ref + "-target", kind="codex",
                    session_id=args.session_id, project=project, source=args.source)
                row = dict(version=1, native_home=str(host.store.native_home), source_ref=source_ref,
                           sender=asdict(sender), target=asdict(target),
                           repositories=row["repositories"] if row else {})
            row["repositories"].setdefault(repository.key, bus.clock())
            _save(fd, ACCEPTANCE_NOTIFICATIONS + ".json", row)
    return dict(configured=True, repo=repository.root, notify="manual_acceptance")


def proof_directory(host, requested):
    roots = host.execution.profile.proof_directories(host.store)
    _require(bool(roots), "UNSUPPORTED_CAPABILITY")
    root = roots[0] if requested is None else Path(requested)
    _require(root in roots, "FORBIDDEN")
    return root


def issue(host, directory, project, role, ref, source, **claims):
    identifier = "credential-" + secrets.token_hex(16)
    challenge = secrets.token_urlsafe(32)
    identity = SessionIdentity("local_cli", ref, project, "operator enrolled local CLI").to_mapping()
    with host.store._private(directory) as fd:
        _save(fd, identifier + ".json", dict(version=2, reference=ref, proof=dict(
            method="local-cli-token", credential_ref=identifier, challenge=challenge)), exclusive=True)
        record(host, identifier, dict(enabled=True, reference=ref, role=role, identity=identity,
            token_hash=_sha(challenge.encode()), source=source, **claims))
    return str(directory / (identifier + ".json"))


def submission(host, args):
    spec = host.store.normalize_spec(args.repo, args.spec)
    directory = proof_directory(host, args.proof_root)
    refs = {role: "agentref-" + secrets.token_hex(16) for role in ("controller", "interaction")}
    for role, ref in refs.items():
        record(host, ref, dict(enabled=True, reference=ref, repo_key=spec.repository.key,
                              role=role, revisions=[1], source=args.source))
    binding = dict(controller_ref=refs["controller"], interaction_ref=refs["interaction"], revision=1)
    grant = dict(reference="submission-" + secrets.token_hex(16), repo_key=spec.repository.key,
                 spec_sha256=spec.spec_sha256, binding=binding)
    proof = issue(host, directory, spec.repository.root, "controller", refs["controller"],
                  args.source, submissions=[grant])
    return dict(controller_proof=proof, spec_sha256=spec.spec_sha256, binding=binding)


def scope_proof(host, args):
    task_id = _identifier(args.task_id, "task")
    task, _, binding = host.interaction._task(task_id)
    _require(task["creation"]["runtime_seal"] == host.store.runtime_seal
             and task["creation"]["phase"] == "ready", "RUNTIME_MISMATCH")
    role = args.role
    endpoint_role = "controller" if role == "controller" else "interaction"
    ref = binding[endpoint_role + "_ref"]
    with host.store._private(host.store._task_dir(task_id)) as fd:
        scope = host.store._scope(fd, task_id)
    _require(host.endpoint_grant(ref, scope, endpoint_role, binding["revision"]), "FORBIDDEN")
    generation, _ = host.interaction._kernel(task)
    if task["worktree_removed"]:
        generation = host.interaction._removed_generation(task)
    _require(host.store.load_task(task_id)["revision"] == task["revision"], "BUSY")
    directory = proof_directory(host, args.proof_root)
    proof = issue(host, directory, task["repo"]["root"], role, ref, args.source,
        scope=asdict(scope), binding_revision=binding["revision"], run_id=task["run_id"],
        generation=generation, operations=list(OPERATIONS[role]))
    return dict(proof=proof, task_id=task_id, run_id=task["run_id"], role=role)


def attest(host, args):
    locator = host.actor_file()
    principal, scope, task, binding, row, item = host.interaction._addressed(locator, args.request_id, "answer")
    _require(row and row["answer"] is None and item["kind"] in ("permission", "host_handoff")
             and args.performed == (item["kind"] == "host_handoff")
             and task["creation"]["runtime_seal"] == host.store.runtime_seal, "FORBIDDEN")
    _text(args.answer, maximum=8192)
    ref = host.event_ref(principal, scope, binding, "answer", item, args.answer)
    record(host, ref, dict(grant_ref=item["purpose_ref"], performed=args.performed, source=args.source))
    return dict(event=ref, request_id=args.request_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="operation", required=True)
    configure = commands.add_parser("configure", allow_abbrev=False)
    configure.add_argument("--profile", required=True)
    bind = commands.add_parser("bind-acceptance", allow_abbrev=False)
    for name in ("repo", "session-id", "session-project"):
        bind.add_argument("--" + name, required=True)
    submit = commands.add_parser("submission", allow_abbrev=False)
    for name in ("repo", "spec"):
        submit.add_argument("--" + name, required=True)
    scoped = commands.add_parser("scope", allow_abbrev=False)
    scoped.add_argument("task_id")
    scoped.add_argument("--role", choices=tuple(OPERATIONS), required=True)
    for command in (submit, scoped):
        command.add_argument("--proof-root")
    evidence = commands.add_parser("attest", allow_abbrev=False)
    evidence.add_argument("request_id")
    evidence.add_argument("--answer", required=True)
    evidence.add_argument("--performed", action="store_true")
    for command in (configure, bind, submit, scoped, evidence):
        command.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        _text(args.source)
        seal = runtime_seal(required=True)
        host = Host(seal=seal)
        with installed_boundary(seal):
            if args.operation == "configure":
                _, data = read_input_file(args.profile)
                row = _strict_json(data, "INVALID_SPEC")
                _fields(row, ("worker_executable", "worker_home", "review_tools", "proof_roots"))
                HostProfile.from_document(dict(row, fixture=False)).proof_directories(host.store)
                record(host, "profile", row)
                result = {"configured": True}
            elif args.operation == "bind-acceptance":
                result = bind_acceptance(host, args)
            else:
                host.compose()
                _require(host.execution.profile is not None, "UNSUPPORTED_CAPABILITY")
                result = dict(submission=submission, scope=scope_proof, attest=attest)[args.operation](host, args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except TaskError as exc:
        print(json.dumps({"error": exc.code}))
        return exc.exit_code
    except (OSError, TypeError, KeyError, ValueError):
        print(json.dumps({"error": "HOST_SETUP_REFUSED"}))
        return 5


if __name__ == "__main__":
    sys.exit(main())
