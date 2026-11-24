#!/usr/bin/env python3
"""Trusted local operator setup; no services, native sessions or model calls."""
import argparse
from dataclasses import asdict
import fcntl
import json
from pathlib import Path
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_registry
from agent_registry import SessionActivity, SessionIdentity
import ato_config
from ato_execution import _repo_gate, quarantine_task
from ato_host import Host, _CREDENTIAL, _SESSION
from ato_execution_host import HostProfile
from ato_seal import installed_boundary, runtime_seal
from ato_task_store import (
    SubmissionOrigin, TaskError, _directory, _fields, _identifier, _matches, _read_json,
    _repo_boundaries, _require, _save, _sha,
    _strict_json, _text, canonical_json, normalize_repository, read_input_file,
)
from codex_session_adapter import CodexSessionAdapter
from message_bus import EndpointCredential, MailboxError

OPERATIONS = {
    "controller": ("observe", "initial", "stop", "resume", "recheck", "reconcile",
                   "trace", "cleanup", "binding", "ask", "read_answer", "handoff", "reviewer"),
    "interaction": ("observe", "binding", "read", "present"),
    "user": ("observe", "read", "answer", "accept", "handoff", "reviewer", "stop", "binding"),
}


def record(host, name, value, *, update=False):
    with host.store._private(host.folder, create=True) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _save(fd, name + ".json", value, exclusive=not update)


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
    origin = SubmissionOrigin(str(host.store.native_home), args.session_id, str(project))
    host.register_acceptance_route(bus, origin, args.source, repo_key=repository.key)
    return dict(configured=True, repo=repository.root, notify="completion")


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
    with _directory(directory, private_from=directory) as fd:
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
    current_ref = binding[endpoint_role + "_ref"]
    ref = args.reference or current_ref
    revision = args.revision if args.revision is not None else binding["revision"]
    _require((revision, ref) == (binding["revision"], current_ref)
             or (role == "controller" and revision == binding["revision"] + 1), "FORBIDDEN")
    with host.store._private(host.store._task_dir(task_id)) as fd:
        scope = host.store._scope(fd, task_id)
    _require(host.endpoint_grant(ref, scope, endpoint_role, revision), "FORBIDDEN")
    generation, _ = host.interaction._kernel(task)
    if task["worktree_removed"]:
        generation = host.interaction._removed_generation(task)
    _require(host.store.load_task(task_id)["revision"] == task["revision"], "BUSY")
    directory = proof_directory(host, args.proof_root)
    proof = issue(host, directory, task["repo"]["root"], role, ref, args.source,
        scope=asdict(scope), binding_revision=revision, run_id=task["run_id"],
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


def configure_host(host, args):
    _, data = read_input_file(args.profile)
    row = _strict_json(data, "INVALID_SPEC")
    _fields(row, ("worker_executable", "worker_home", "review_tools", "proof_roots"))
    _require(isinstance(row["review_tools"], list) and all(
        isinstance(tool, list) and len(tool) == 3 and all(isinstance(x, str) for x in tool)
        for tool in row["review_tools"])
        and isinstance(row["proof_roots"], list) and all(isinstance(x, str) for x in row["proof_roots"]),
        "INVALID_SPEC")
    HostProfile.from_document(dict(row, fixture=False)).verify_configuration(host.store)
    if args.update:
        _require(host.record("profile") is not None, "NOT_FOUND")
    record(host, "profile", row, update=args.update)
    record(host, "profile-event-" + secrets.token_hex(16), dict(
        action="update" if args.update else "configure", profile_sha256=_sha(canonical_json(row)),
        source=args.source, recorded_at=time.time()))
    return {"configured": True}


def enroll_endpoint(host, args):
    repo = normalize_repository(args.repo)
    ref = args.reference or "agentref-" + secrets.token_hex(16)
    _require(_matches(_SESSION, ref) and args.revision > 0, "INVALID_EVIDENCE")
    _require(bool(args.native_kind) == bool(args.native_session), "INVALID_EVIDENCE")
    with host.store._private(host.folder, create=True) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        row = host.record(ref)
        if row is None:
            row = dict(enabled=True, reference=ref, repo_key=repo.key, role=args.role,
                       revisions=[args.revision], source=args.source)
        else:
            _require(row.get("enabled") is True and row["repo_key"] == repo.key
                     and row["role"] == args.role, "FORBIDDEN")
            row["revisions"] = sorted(set(row["revisions"]) | {args.revision})
        if args.native_kind:
            _require(not row.get("mailbox"), "FORBIDDEN")
            import agents
            import transport
            adapter = agents.registry.get(args.native_kind, "session_verify")
            with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
                _require(adapter.verify_session(args.native_session, repo.root), "FORBIDDEN")
                credential = transport.managed_bus().register_endpoint(
                    ref, kind=args.native_kind, session_id=args.native_session, project=repo.root,
                    source=args.source)
            row["mailbox"] = asdict(credential)
        _save(fd, ref + ".json", row)
        _save(fd, "enrollment-" + secrets.token_hex(16) + ".json", dict(
            reference=ref, revision=args.revision, source=args.source, recorded_at=time.time()), exclusive=True)
    return dict(reference=ref, binding_revision=args.revision)


def approve_binding(host, args):
    task_id = _identifier(args.task_id, "task")
    principal, scope, task, _, binding, _ = host.binding._current(
        host.actor_file(), task_id, observe=host.binding.handoff_observe if args.action == "handoff" else None)
    _require(task["creation"]["runtime_seal"] == host.store.runtime_seal, "RUNTIME_MISMATCH")
    grant = host.actor_grant(principal, scope, binding, args.action)
    _require(grant and grant.role == "user" and args.action in grant.operations, "FORBIDDEN")
    source_ref = "host-" + _sha(args.source.encode())
    evidence = dict(schema_version=1, task_id=task_id, spec_sha256=scope.spec_sha256,
                    binding_revision=binding["revision"], source=source_ref)
    if args.action == "handoff":
        _require(_matches(_SESSION, args.to) and args.kind is None, "INVALID_EVIDENCE")
        target = args.interaction or binding["interaction_ref"]
        after = dict(binding, controller_ref=args.to, interaction_ref=target, revision=binding["revision"] + 1)
        host.binding._authorize_endpoints(scope, after)
        evidence.update({"from": binding["controller_ref"], "to": args.to, "interaction": target})
    else:
        from hub_contract import HubAgentSpec
        _require(args.to is None and args.interaction is None, "INVALID_EVIDENCE")
        evidence["reviewer"] = asdict(HubAgentSpec(args.kind, args.model, args.effort))
    digest = _sha(canonical_json(evidence))
    filename = "binding-evidence-" + digest
    directory = proof_directory(host, args.proof_root)
    with _directory(directory, private_from=directory) as fd:
        _save(fd, filename + ".json", evidence, exclusive=True)
    record(host, "decision-" + digest, dict(actor_id=principal.actor_id,
        operation=args.action, source_ref=source_ref, source=args.source))
    return dict(decision=str(directory / (filename + ".json")), evidence_sha256=digest)


def revoke(host, args):
    ref = args.reference
    _require(_matches(_SESSION, ref) or _matches(_CREDENTIAL, ref), "INVALID_EVIDENCE")
    with host.store._private(host.folder) as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        row = _read_json(fd, ref + ".json")
        row.update(enabled=False, revoked_source=args.source, revoked_at=time.time())
        _save(fd, ref + ".json", row)
    return dict(reference=ref, enabled=False)


def reconcile_delivery(host, args):
    task_id = _identifier(args.task_id, "task")
    task = host.store.load_task(task_id)
    with _repo_gate(host.store, task):
        task = host.store.load_task(task_id)
        _require(task["creation"]["runtime_seal"] == host.store.runtime_seal, "RUNTIME_MISMATCH")
        bus = host.mailbox()
        _require(bus is not None, "NOT_FOUND")
        selected = bus.message(args.message_id)
        _require(selected["protocol"] == 2 and selected["task_id"] == task_id
                 and selected["kind"] in {"ato_question", "ato_answer", "ato_completion", "ato_failure"},
                 "FORBIDDEN")
        phase = selected["kind"].removeprefix("ato_")
        _, notice = host.interaction._reservation(task, selected["correlation_id"], phase)
        message = host.interaction._message(task, notice) if notice else None
        _require(message and message["id"] == selected["id"] and message["external_possible"], "FORBIDDEN")
        role = "controller" if phase == "answer" else "interaction"
        row = host._endpoint(notice["to_ref"], task["repo"]["key"], role, historical=True)
        scope = host.interaction._scope_record(task)
        _require(host.endpoint_grant(notice["to_ref"], scope, role, notice["binding_revision"], historical=True)
                 and row.get("mailbox"), "FORBIDDEN")
        credential = EndpointCredential(**row["mailbox"])
        _require(credential.endpoint_id == message["target"], "FORBIDDEN")
        owner = bus.acquire_endpoint(credential, owner="ato-host-reconcile", lease_seconds=60)
        try:
            endpoint = bus.authenticate(credential)
            identity = SessionIdentity(endpoint["kind"], endpoint["session_id"], endpoint["project"], endpoint["source"])
            import agents
            capability = agent_registry.session_capabilities(endpoint["kind"])
            _require(capability.state("session.activity") is agent_registry.CapabilityState.SUPPORTED,
                     "UNSUPPORTED_CAPABILITY")
            adapter = agents.registry.get(endpoint["kind"])
            with ato_config.use_unified_kernel(host.store.kernel_root, host.store.native_home):
                activity = adapter.session_activity(identity, generation=notice["generation"][-1])
            _require(type(activity) is SessionActivity and activity.session == identity
                     and activity.generation == notice["generation"][-1] and activity.state == "idle"
                     and 0 <= time.time() - activity.observed_at < 10, "RUN_UNSETTLED")
            with host.store._mutation(task_id, task["revision"]):
                result = bus.reconcile_correlated(credential, owner, message["id"], source=args.source,
                    outcome="not_running", generation=notice["generation"], endpoint_generation=endpoint["generation"])
            record(host, "delivery-reconciliation-" + secrets.token_hex(16), dict(
                task_id=task_id, message_id=message["id"], source=args.source,
                generation=notice["generation"], observed_at=activity.observed_at, state=result["state"]))
            return dict(message_id=message["id"], state=result["state"], busy=bool(result["busy"]))
        finally:
            bus.release_endpoint(credential, owner)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="operation", required=True)
    configure = commands.add_parser("configure", allow_abbrev=False)
    configure.add_argument("--profile", required=True)
    configure.add_argument("--update", action="store_true")
    bind = commands.add_parser("bind-acceptance", allow_abbrev=False)
    for name in ("repo", "session-id", "session-project"):
        bind.add_argument("--" + name, required=True)
    submit = commands.add_parser("submission", allow_abbrev=False)
    for name in ("repo", "spec"):
        submit.add_argument("--" + name, required=True)
    scoped = commands.add_parser("scope", allow_abbrev=False)
    scoped.add_argument("task_id")
    scoped.add_argument("--role", choices=tuple(OPERATIONS), required=True)
    scoped.add_argument("--reference")
    scoped.add_argument("--revision", type=int)
    for command in (submit, scoped):
        command.add_argument("--proof-root")
    evidence = commands.add_parser("attest", allow_abbrev=False)
    evidence.add_argument("request_id")
    evidence.add_argument("--answer", required=True)
    evidence.add_argument("--performed", action="store_true")
    enrollment = commands.add_parser("enroll-endpoint", allow_abbrev=False)
    enrollment.add_argument("--repo", required=True)
    enrollment.add_argument("--role", choices=("controller", "interaction"), required=True)
    enrollment.add_argument("--revision", type=int, required=True)
    enrollment.add_argument("--reference")
    enrollment.add_argument("--native-kind", choices=("codex", "pi_acp"))
    enrollment.add_argument("--native-session")
    approval = commands.add_parser("approve-binding", allow_abbrev=False)
    approval.add_argument("task_id")
    approval.add_argument("action", choices=("handoff", "reviewer"))
    for name in ("to", "interaction", "kind", "model", "effort", "proof-root"):
        approval.add_argument("--" + name)
    revocation = commands.add_parser("revoke", allow_abbrev=False)
    revocation.add_argument("reference")
    reconciliation = commands.add_parser("reconcile-delivery", allow_abbrev=False)
    reconciliation.add_argument("task_id")
    reconciliation.add_argument("message_id")
    quarantine = commands.add_parser("quarantine-task", allow_abbrev=False)
    quarantine.add_argument("task_id")
    for command in (configure, bind, submit, scoped, evidence, enrollment, approval, revocation, reconciliation, quarantine):
        command.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        _text(args.source)
        seal = runtime_seal(required=True)
        host = Host(seal=seal)
        with installed_boundary(seal):
            if args.operation == "configure":
                result = configure_host(host, args)
            elif args.operation == "bind-acceptance":
                result = bind_acceptance(host, args)
            else:
                host.compose()
                _require(host.execution.profile is not None, "UNSUPPORTED_CAPABILITY")
                handlers = {"submission": submission, "scope": scope_proof, "attest": attest,
                            "enroll-endpoint": enroll_endpoint, "approve-binding": approve_binding,
                            "revoke": revoke, "reconcile-delivery": reconcile_delivery,
                            "quarantine-task": lambda host, args: quarantine_task(
                                host.store, _identifier(args.task_id, "task"), args.source)}
                result = handlers[args.operation](host, args)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except TaskError as exc:
        print(json.dumps({"error": exc.code}))
        return exc.exit_code
    except MailboxError:
        print(json.dumps({"error": "DELIVERY_UNKNOWN"}))
        return 4
    except (OSError, TypeError, KeyError, ValueError):
        print(json.dumps({"error": "HOST_SETUP_REFUSED"}))
        return 5


if __name__ == "__main__":
    sys.exit(main())
