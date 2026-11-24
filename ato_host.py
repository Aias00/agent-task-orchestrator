"""Private host configuration, local CLI authority and scoped actor grants."""
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
import fcntl
from functools import partial
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

import agent_registry
import ato_config
from ato_acceptance import InteractionService, MailboxRoute, VerifiedEvent
from ato_binding import BindingDecision, BindingGrant, BindingService, read_binding
from ato_execution import ExecutionHooks, ExecutionService, HostProfile
from ato_task_store import (
    InitialBinding, SubmissionGrant, SubmissionOrigin, TaskError, TaskScope, TaskStore, _REF, _SHA, _absolute,
    _directory, _existing, _regular, _fields, _matches, _overlaps, _read_file, _require, _save, _sha, _strict_json,
    _time, canonical_json,
)
from codex_session_adapter import CodexSessionAdapter
from message_bus import ConflictError, EndpointCredential, IdentityError, MailboxError, MessageBus

_CREDENTIAL = re.compile(r"credential-[0-9a-f]{32}")
_SESSION = re.compile(r"agentref-[0-9a-f]{32}")
_ROLE = {"controller": "origin", "interaction": "hub", "user": "user"}
_LOCAL_OPERATIONS = frozenset({"initial", "observe", "stop", "resume", "recheck",
                               "trace", "cleanup", "binding"})
_CONTROLLER_OPERATIONS = _LOCAL_OPERATIONS | {"reconcile", "ask", "read_answer"}
ACCEPTANCE_NOTIFICATIONS = "acceptance-notifications"


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
        self._local_owner = object()
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
        _fields(row, ("worker_executable", "worker_home", "review_tools", "proof_roots"))
        return HostProfile.from_document(dict(row, fixture=False))

    def caller(self, operation):
        """Use the sealed CLI's local account for routine local operations."""
        if operation not in _LOCAL_OPERATIONS | {"accept"} or "ATO_ACTOR_FILE" in os.environ:
            return self.actor_file()
        _require(_matches(_SHA, self.store.runtime_seal) and os.getuid() == os.geteuid(), "AUTH_REQUIRED")
        try:
            with self.store._private(self.store.home):
                pass
        except FileNotFoundError:
            # A fresh read-only invocation must not initialize the state root.
            pass
        return self._local_owner

    def _local_authorization(self):
        return f"local-owner:{os.geteuid()}"

    def _local_binding(self, repo_key, spec_sha256):
        source = [self._local_authorization(), str(self.store.home), repo_key, spec_sha256]
        controller, interaction = (
            "agentref-" + _sha(canonical_json([*source, role]))[:32] for role in ("controller", "interaction")
        )
        return InitialBinding(controller, interaction)

    def _local_scope_binding(self, scope):
        if scope.authorization_ref == self._local_authorization():
            return self._local_binding(scope.repo_key, scope.spec_sha256)
        return None

    def _owns_local_binding(self, scope, binding):
        local = self._local_scope_binding(scope)
        return local is not None and (binding["controller_ref"], binding["interaction_ref"]) == (
            local.controller_ref, local.interaction_ref)

    def authorize_local_acceptance(self, actor, scope, binding):
        return actor is self._local_owner and self._owns_local_binding(scope, binding)

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
        if locator is self._local_owner:
            return self._local_owner
        if locator is None:
            return None
        proof, ref, path = locator
        profile = self.configured_profile()
        if profile is None or (not profile.fixture and not any(
                path.is_relative_to(parent) for parent in profile.proof_directories(self.store))):
            return None
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
        if actor is self._local_owner:
            binding = self._local_binding(spec.repository.key, spec.spec_sha256)
            return SubmissionGrant(self._local_authorization(), spec.repository.key, spec.spec_sha256, binding)
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
        if grant.binding != binding:
            return None
        if actor is self._local_owner:
            adapter = CodexSessionAdapter()
            if session_id := adapter.current_locator():
                with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
                    project = adapter.session_project(session_id)
                native_home = str(self.store.native_home)
                origin = SubmissionOrigin(native_home, session_id, project)
                if not origin.valid():
                    origin = SubmissionOrigin(native_home, None, None)
                grant = replace(grant, origin=origin)
        return grant

    def can_observe(self, actor, scope):
        local = None
        if actor is self._local_owner:
            local = self._local_scope_binding(scope)
            if local is None:
                return False
        else:
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
        if local is not None:
            return binding["controller_ref"] == local.controller_ref
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
        if actor is self._local_owner:
            local = self._local_scope_binding(scope)
            if local and binding["controller_ref"] == local.controller_ref:
                return BindingGrant(scope, local.controller_ref, "controller", binding["revision"], _LOCAL_OPERATIONS)
            return None
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
        if actor is self._local_owner:
            return True
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
        local = self._local_scope_binding(scope)
        if local and role in {"controller", "interaction"} and ref == getattr(local, role + "_ref"):
            return BindingGrant(scope, ref, role, revision, frozenset())
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
        key = self.event_ref(actor, scope, binding, action, item, value)
        approved = self.record(key)
        if approved:
            _fields(approved, ("grant_ref", "performed"), ("source",))
            return VerifiedEvent(key, approved["grant_ref"], approved["performed"])
        # An answer alone never materializes a permission or host action.
        return VerifiedEvent(key)

    @staticmethod
    def event_ref(actor, scope, binding, action, item, value):
        return "event-" + _sha(canonical_json(
            [asdict(scope), binding["revision"], actor.actor_id, action, item, value]))

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

    def _acceptance_record(self, name, *, legacy=False):
        row = self.record(name)
        if row is None:
            return None
        fields = ("version", "native_home", "source_ref", "sender", "target")
        _fields(row, fields + (("repositories",) if legacy else ()))
        _require(type(row["version"]) is int and row["version"] == 1
                 and _absolute(row["native_home"]) and _matches(_REF, row["source_ref"]))
        if legacy:
            _require(isinstance(row["repositories"], dict)
                     and all(_absolute(key) and _time(since) for key, since in row["repositories"].items()))
        for key in ("sender", "target"):
            endpoint = row[key]
            _fields(endpoint, ("endpoint_id", "session_id", "project", "token"))
            _require(all(isinstance(value, str) and value for value in endpoint.values())
                     and _absolute(endpoint["project"]))
        _require(row["sender"]["endpoint_id"] != row["target"]["endpoint_id"])
        return row

    def acceptance_notifications(self):
        return self._acceptance_record(ACCEPTANCE_NOTIFICATIONS, legacy=True)

    @staticmethod
    def _acceptance_origin_name(origin):
        return "acceptance-origin-" + _sha(canonical_json([origin.native_home, origin.session_id]))

    @staticmethod
    def _matches_acceptance_origin(row, origin):
        return bool(row and (row["native_home"], row["target"]["session_id"], row["target"]["project"])
                    == (origin.native_home, origin.session_id, origin.project))

    def register_acceptance_route(self, bus, origin, source, *, repo_key=None):
        """Enroll one native session or opt a repository into the legacy route."""
        _require(origin.valid() and origin.session_id and origin.native_home == str(self.store.native_home),
                 "FORBIDDEN")
        name = self._acceptance_origin_name(origin)
        with self.store._private(self.folder, create=True) as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TaskError("BUSY") from None
            legacy = self.acceptance_notifications()
            if repo_key is not None and legacy is not None:
                _require(self._matches_acceptance_origin(legacy, origin), "DECISION_CONFLICT")
            row = self._acceptance_record(name)
            if row is not None:
                _require(self._matches_acceptance_origin(row, origin), "BINDING_CHANGED")
            elif self._matches_acceptance_origin(legacy, origin):
                row = {key: value for key, value in legacy.items() if key != "repositories"}
            with bus._transaction() as connection:
                if not _registered_acceptance_endpoints(bus, connection, row):
                    registrar = _EndpointRegistration(connection, bus.clock)
                    source_ref = row["source_ref"] if row else "ato-acceptance-" + secrets.token_hex(16)
                    sender = registrar.register_endpoint(source_ref + "-sender", kind="local_cli",
                        session_id=source_ref, project=origin.project, source=source)
                    target = registrar.register_endpoint(source_ref + "-target", kind="codex",
                        session_id=origin.session_id, project=origin.project, source=source)
                    row = dict(version=1, native_home=origin.native_home, source_ref=source_ref,
                               sender=asdict(sender), target=asdict(target))
                if repo_key is None:
                    _save(fd, name + ".json", row)
                else:
                    repositories = legacy["repositories"] if legacy else {}
                    repositories.setdefault(repo_key, bus.clock())
                    _save(fd, ACCEPTANCE_NOTIFICATIONS + ".json", dict(row, repositories=repositories))
        return row

    def acceptance_queue_binary(self):
        packages = self.store.native_home / "packages/standalone"
        try:
            executable = (packages / "current/bin/codex").resolve(strict=True)
            releases = (packages / "releases").resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if (releases == packages / "releases" and executable.is_relative_to(releases)
                and executable.is_file() and os.access(executable, os.X_OK)):
            return str(executable)
        return None

    def local_acceptance_route(self, scope, binding, item):
        if binding["revision"] != 1 or not self._owns_local_binding(scope, binding):
            return None
        if not self.acceptance_queue_binary():
            return None
        observer = self.record("observation", {})
        if observer.get("enabled") is not True:
            return None
        bus = self.mailbox()
        if bus is None:
            return None
        origin = self.store.submission_origin(scope.task_id)
        if origin is not None:
            return self._origin_acceptance_route(bus, origin)
        row = self.acceptance_notifications()
        if row is None or row["native_home"] != str(self.store.native_home):
            return None
        since = row["repositories"].get(scope.repo_key)
        if since is None or item["created_at"] < since:
            return None
        return self._acceptance_mailbox_route(bus, row)

    def _origin_acceptance_route(self, bus, origin):
        if origin.session_id is None or origin.native_home != str(self.store.native_home):
            return None
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            if CodexSessionAdapter().session_project(origin.session_id) != origin.project:
                return None
        row = self._acceptance_record(self._acceptance_origin_name(origin))
        if row is not None:
            _require(self._matches_acceptance_origin(row, origin), "BINDING_CHANGED")
            if route := self._acceptance_mailbox_route(bus, row):
                return route
        if self.read_only:
            return None
        try:
            row = self.register_acceptance_route(bus, origin, "verified ATO submission session")
        except ConflictError:
            return None  # A separately enrolled native endpoint belongs to its current owner.
        return self._acceptance_mailbox_route(bus, row)

    @staticmethod
    def _acceptance_mailbox_route(bus, row):
        sender = EndpointCredential(**row["sender"])
        target = EndpointCredential(**row["target"])
        try:
            endpoints = [bus.authenticate(endpoint) for endpoint in (sender, target)]
        except MailboxError:
            return None
        if [(ep["kind"], ep["state"]) for ep in endpoints] != [("local_cli", "active"), ("codex", "active")]:
            return None
        return MailboxRoute(sender, target, row["source_ref"], local_acceptance=True)

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
        bus_type = _ExistingMailbox if self.read_only else MessageBus
        bus = bus_type.__new__(bus_type)
        bus.path, bus.clock = path, time.time
        return bus

    def compose(self, *, render_notice=None):
        self.execution = ExecutionService(self.store, self.configured_profile(), authorize=self.authorize,
            hooks=ExecutionHooks(binding=partial(read_binding, self.store)))
        self.interaction = InteractionService(self.store, self.execution, authorize=self.authorize_interaction,
            verify_event=self.verify_event, verify_continuation=self.verify_continuation, route=self.route,
            mailbox=self.mailbox, render_notice=render_notice, local_acceptance_route=self.local_acceptance_route,
            authorize_local_acceptance=self.authorize_local_acceptance)
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


class _EndpointRegistration(MessageBus):
    """Register both mailbox endpoints in the caller's transaction."""

    def __init__(self, connection, clock):
        self.connection = connection
        self.clock = clock

    @contextmanager
    def _transaction(self):
        yield self.connection


def _registered_acceptance_endpoints(bus, connection, row):
    if row is None:
        return False
    found = []
    for key in ("sender", "target"):
        credential = EndpointCredential(**row[key])
        try:
            bus._endpoint(connection, credential.endpoint_id)
        except IdentityError:
            continue
        found.append(bus._authenticate(connection, credential))
    if not found:
        # The private record can outlive an interrupted SQLite commit.
        return False
    _require([(ep["kind"], ep["state"]) for ep in found] == [("local_cli", "active"), ("codex", "active")])
    return True
