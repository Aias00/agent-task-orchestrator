"""Private host configuration and grants for the unified services.

Files are provisioned by a trusted local host using the scoped actor boundary.
The CLI never issues credentials or grants. Repository content is data only.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict
from functools import partial
import hmac
import os
from pathlib import Path
import re
import sqlite3
import time

import agent_registry
from ato_acceptance import InteractionService, MailboxRoute, VerifiedEvent
from ato_binding import BindingDecision, BindingGrant, BindingService, read_binding
from ato_execution import ExecutionHooks, ExecutionService, HostProfile
from ato_task_store import (
    InitialBinding, SubmissionGrant, TaskError, TaskScope, TaskStore, _SHA, _directory, _existing, _regular,
    _fields, _matches, _overlaps, _read_file, _require, _sha, _strict_json, canonical_json,
)
from message_bus import EndpointCredential, MessageBus

_CREDENTIAL = re.compile(r"credential-[0-9a-f]{32}")
_SESSION = re.compile(r"agentref-[0-9a-f]{32}")
_ROLE = {"controller": "origin", "interaction": "hub", "user": "user"}
_CONTROLLER_OPERATIONS = frozenset({"initial", "observe", "stop", "resume", "recheck",
                                    "reconcile", "trace", "cleanup", "binding", "ask", "read_answer"})


def host_record(path, root, *, code="STATE_CORRUPT"):
    with _directory(path.parent, private_from=root, code=code) as fd:
        data = _read_file(fd, path.name, limit=1024 * 1024, code=code, stable_path=True)
    return _strict_json(data, code)


class Host:
    def __init__(self, *, store=None, seal=None, profile=None, read_only=False):
        self.store = store if store is not None else TaskStore(runtime_seal=seal)
        self.folder = self.store.root / "host"
        self.profile = profile
        self.read_only = read_only
        self.store.authenticate = self.authenticate
        self.store.can_observe = self.can_observe
        self.store.authorize_submit = self.authorize_submit

    def record(self, name, default=None):
        try:
            return host_record(self.folder / (name + ".json"), self.store.home)
        except FileNotFoundError:
            return default

    def configured_profile(self):
        if self.profile is not None:
            return self.profile
        row = self.record("profile")
        if row is None:
            return None
        _fields(row, ("worker_executable", "worker_home", "review_tools"))
        return HostProfile.from_document(dict(row, fixture=False))

    def actor_file(self):
        raw = os.environ.get("ATO_ACTOR_FILE")
        _require(raw, "AUTH_REQUIRED")
        path = Path(raw).expanduser()
        _require(path.is_absolute() and path == path.resolve() and
                 not any(_overlaps(path, p) for p in (self.store.home, *self.store.native_homes)),
                 "AUTH_REQUIRED")
        try:
            row = host_record(path, path.parent, code="AUTH_REQUIRED")
            _fields(row, ("version", "reference", "proof"), ("workflow_id",), code="AUTH_REQUIRED")
            proof = agent_registry.ActorProof(**row["proof"])
            _require(row["version"] == 2 and proof.method == "local-cli-token"
                     and _matches(_CREDENTIAL, proof.credential_ref) and _matches(_SESSION, row["reference"]),
                     "AUTH_REQUIRED")
            return proof, row["reference"], path
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            raise TaskError("AUTH_REQUIRED") from None

    def credential(self, identifier):
        if _matches(_CREDENTIAL, identifier):
            row = self.record(identifier)
            if row and row.get("enabled") is True:
                return row
        return None

    def authenticate(self, locator):
        if locator is None:
            return None
        proof, ref, path = locator
        row = self.credential(proof.credential_ref)
        if (row is None or row["reference"] != ref or not _matches(_SHA, row["token_hash"])
                or not hmac.compare_digest(row["token_hash"], _sha(proof.challenge.encode()))):
            return None
        identity = agent_registry.SessionIdentity.from_mapping(row["identity"])
        # Keep human proofs out of every repo to which this principal is enrolled.
        _require(not _overlaps(path, Path(identity.project)), "AUTH_REQUIRED")
        workflow_id, contract_hash, generation = "enrollment", "enrollment", 0
        if scope := row.get("scope"):
            task = self.store.load_task(scope["task_id"])
            _require(not _overlaps(path, Path(task["worktree"]["path"])), "AUTH_REQUIRED")
            workflow_id = task["workflow"]["directory"]
            contract_hash = task["workflow"]["contract_hash"]
            generation = row["generation"][1]
        return agent_registry.ActorContext._from_verified_adapter(
            proof.credential_ref, _ROLE[row["role"]], agent_registry.SessionReference(ref, identity),
            identity.project, workflow_id, contract_hash, generation, "private scoped host credential")

    def _actor_row(self, actor):
        row = self.credential(actor.actor_id)
        if (row and row["reference"] == actor.session.reference and _ROLE[row["role"]] == actor.role
                and row["identity"] == actor.session.identity.to_mapping()):
            return row
        return None

    def initial(self, spec, actor):
        row = self._actor_row(actor)
        _require(row and row["role"] == "controller" and row.get("scope") is None
                 and actor.project == spec.repository.root, "FORBIDDEN")
        for grant in row.get("submissions", []):
            if (grant["repo_key"], grant["spec_sha256"]) == (spec.repository.key, spec.spec_sha256):
                binding = InitialBinding(**grant["binding"])
                endpoint = self._endpoint(binding.interaction_ref, grant["repo_key"], "interaction")
                _require(binding.revision == 1 and binding.controller_ref == actor.session.reference
                         and endpoint and endpoint.get("scope") is None and 1 in endpoint["revisions"],
                         "FORBIDDEN")
                return SubmissionGrant(grant["reference"], spec.repository.key, spec.spec_sha256, binding)
        raise TaskError("FORBIDDEN")

    def authorize_submit(self, spec, locator, binding):
        actor = self.store._principal(locator)
        grant = self.initial(spec, actor)
        return grant if grant.binding == binding else None

    def can_observe(self, actor, scope):
        row = self._actor_row(actor)
        if row is None:
            return False
        scoped = row.get("scope") is not None
        if scoped and (row["scope"] != asdict(scope) or "observe" not in row["operations"]):
            return False
        if not scoped and self._submitted(row, scope) is None:
            return False
        try:
            task = self.store.load_task(scope.task_id)
            binding = read_binding(self.store, task, self.store.load_spec(scope.task_id))
        except TaskError as exc:
            # Scope is already verified. The projection reports damaged state
            # without disclosing unrelated scope records.
            return exc.code in {"STATE_CORRUPT", "SPEC_CHANGED"}
        route = "controller_ref" if row["role"] == "controller" else "interaction_ref"
        # A prospective controller needs observation to record an authorized
        # handoff. Its operation grant remains fenced inside BindingService.
        prospective = scoped and row["role"] == "controller" and "handoff" in row["operations"]
        return ((row["reference"] == binding[route]
                 and row.get("binding_revision", 1) == binding["revision"])
                or (prospective and row["binding_revision"] == binding["revision"] + 1))

    @staticmethod
    def _submitted(row, scope):
        return next((grant for grant in row.get("submissions", []) if
                     (grant["repo_key"], grant["spec_sha256"], grant["reference"]) ==
                     (scope.repo_key, scope.spec_sha256, scope.authorization_ref)), None)

    def actor_grant(self, actor, scope, binding, operation):
        row = self._actor_row(actor)
        if row is None:
            return None
        if row.get("scope") == asdict(scope) and row["binding_revision"] == binding["revision"]:
            return BindingGrant(scope, row["reference"], row["role"], binding["revision"],
                                frozenset(row["operations"]))
        if row.get("scope") is None and binding["revision"] == 1 and self._submitted(row, scope):
            return BindingGrant(scope, row["reference"], "controller", 1, _CONTROLLER_OPERATIONS)
        return None

    def authorize(self, actor, scope, binding, operation):
        grant = self.actor_grant(actor, scope, binding, operation)
        if not grant or operation not in grant.operations:
            return False
        if grant.role == "controller" and grant.ref != binding["controller_ref"]:
            return False
        if grant.role == "user" and actor.session.reference != binding["interaction_ref"]:
            return False
        if grant.role not in {"controller", "user"}:
            return False
        row = self._actor_row(actor)
        if row is None:
            return False
        if row.get("scope") is None or operation in {"stop", "trace", "cleanup"}:
            return True
        task = self.store.load_task(scope.task_id)
        generation, _ = self.interaction._kernel(task)
        return (row.get("run_id"), row.get("generation")) == (task["run_id"], generation)

    def _endpoint(self, ref, repo_key, role):
        if not _matches(_SESSION, ref):
            return None
        row = self.record(ref)
        if row and row.get("enabled") is True and (row["reference"], row["repo_key"], row["role"]) == (
                ref, repo_key, role):
            return row
        return None

    def endpoint_grant(self, ref, scope, role, revision):
        row = self._endpoint(ref, scope.repo_key, role)
        if row and (row.get("scope") is None or row["scope"] == asdict(scope)) and revision in row["revisions"]:
            return BindingGrant(scope, ref, role, revision, frozenset())

    def authorize_interaction(self, actor, scope, binding, action, item):
        row = self._actor_row(actor)
        grant = self.actor_grant(actor, scope, binding, action)
        if not row or not grant or action not in grant.operations or row.get("scope") != asdict(scope):
            return False
        generation = item.get("generation")
        if generation is None:
            task = self.store.load_task(scope.task_id)
            generation = self.interaction._kernel(task)[0] if not task["worktree_removed"] else (
                self.interaction._removed_generation(task))
        task = self.store.load_task(scope.task_id)
        if row.get("generation") != generation or row.get("run_id") != item.get("run_id", task["run_id"]):
            return False
        addressed = row.get("request_ids")
        return addressed is None or item.get("request_id") in addressed

    def verify_event(self, actor, scope, binding, action, item, value, source):
        # An explicit invocation under the separately enrolled actual-user CLI
        # principal is the event for ordinary answers and exact acceptance.
        # Privileged approvals and performed host actions require a host receipt.
        if source is not None or actor.role != "user" or action not in ("answer", "accept"):
            return None
        key = _sha(canonical_json([asdict(scope), binding["revision"], actor.actor_id, action, item, value]))
        approved = self.record("event-" + key)
        if approved:
            _fields(approved, ("grant_ref", "performed"))
            return VerifiedEvent("event-" + key, **approved)
        # An answer alone never materializes a permission or host action.
        return VerifiedEvent("event-" + key)

    def verify_continuation(self, scope, binding, request, answer):
        actor = self.credential(answer["actor_ref"])
        if (not actor or actor["reference"] != binding["interaction_ref"] or
                actor.get("scope") != asdict(scope) or actor["binding_revision"] != binding["revision"] or
                (actor.get("run_id"), actor["generation"]) != (request["run_id"], request["generation"]) or
                "answer" not in actor["operations"]):
            return False
        if request["kind"] not in {"permission", "host_handoff"}:
            return True
        event = answer["event_ref"]
        receipt = self.record(event) if re.fullmatch(r"event-[0-9a-f]{64}", event) else None
        return bool(receipt and receipt["grant_ref"] == answer["grant_ref"]
                    and receipt["performed"] is answer["performed"])

    def verify_binding(self, scope, operation, decision, digest):
        row = self.record("decision-" + digest)
        if not row or row["source_ref"] != decision["source"] or row["operation"] != operation:
            return None
        user = self.credential(row["actor_id"])
        if (not user or user["role"] != "user" or user["scope"] != asdict(scope)
                or user["binding_revision"] != decision["binding_revision"] or operation not in user["operations"]):
            return None
        grant = BindingGrant(scope, row["actor_id"], "user", user["binding_revision"], frozenset(user["operations"]))
        return BindingDecision(grant, row["source_ref"], digest)

    def route(self, scope, binding, phase, sender, target):
        refs = ((sender, "interaction"), (target, "controller")) if phase == "answer" else (
            (sender, "controller"), (target, "interaction"))
        credentials = []
        for ref, role in refs:
            grant = self.endpoint_grant(ref, scope, role, binding["revision"])
            endpoint = self._endpoint(ref, scope.repo_key, role) if grant else None
            if endpoint is None or not endpoint.get("mailbox"):
                return None
            credentials.append(EndpointCredential(**endpoint["mailbox"]))
        source = self.record("observation")
        if source and source.get("enabled") is True:
            return MailboxRoute(*credentials, source["source_ref"])
        return None

    def mailbox(self):
        path = self.store.kernel_root / "ctask-interaction" / "mailbox" / "state.sqlite"
        try:
            with self.store._private(path.parent) as fd:
                info = _existing(fd, path.name)
                if info is None:
                    return None
                _regular(info, True, "STATE_CORRUPT")
        except FileNotFoundError:
            return None
        bus = _ExistingMailbox() if self.read_only else MessageBus.__new__(MessageBus)
        bus.path, bus.clock = path, time.time
        return bus

    def compose(self, *, render_notice=None):
        self.execution = ExecutionService(self.store, self.configured_profile(), authorize=self.authorize,
            hooks=ExecutionHooks(binding=partial(read_binding, self.store)))
        self.interaction = InteractionService(self.store, self.execution, authorize=self.authorize_interaction,
            verify_event=self.verify_event, verify_continuation=self.verify_continuation, route=self.route,
            mailbox=self.mailbox, render_notice=render_notice)
        self.execution.hooks = self.interaction.execution_hooks()
        self.binding = BindingService(self.execution, actor_grant=self.actor_grant,
            endpoint_grant=self.endpoint_grant, verify_decision=self.verify_binding,
            reviewer_supported=self.reviewer_supported)
        return self

    def reviewer_supported(self, scope, agent, mode):
        profile = self.configured_profile()
        if mode == "require_resume" or profile is None:
            return False
        try:
            profile.reviewer(agent.kind)
            return True
        except (TaskError, OSError, ValueError):
            return False


class _ExistingMailbox(MessageBus):
    """Projection-only connection, without schema setup or a writer lease."""
    @contextmanager
    def _transaction(self):
        from scripts.release_snapshot import mailbox_copy
        with mailbox_copy(self.path) as copy, closing(sqlite3.connect(copy)) as con:
            con.row_factory = sqlite3.Row
            yield con
