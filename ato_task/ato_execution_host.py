"""Trusted Worker/check subprocess profile and H1 owned-group integration."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import uuid

from ato_common import ato_core
from ato_task import ato_process
from ato_release.support import safe_record, verify_tools
from ato_task.ato_task_store import (TaskError, _absolute, _directory, _fields, _overlaps,
                            _read_file, _require, _save, _strict_json)
from ato_review import hub_contract
from ato_review import hub_sandbox
from ato_adapters.hub_registry import FACTORIES
from ato_pi.pi_toolchain import locate, trusted_path


@dataclass(frozen=True)
class HostProfile:
    """Code-owned, explicit executables and sibling native homes.

    fixture is an injection seam for trusted offline tests with synthetic CLIs.
    It is never read from a task specification or the process environment.
    """
    worker_executable: str
    worker_home: str
    review_tools: tuple[tuple[str, str, str], ...] = ()
    fixture: bool = False
    proof_roots: tuple[str, ...] = ()

    def document(self):
        return asdict(self)

    @classmethod
    def from_document(cls, row):
        _fields(row, ("worker_executable", "worker_home", "review_tools", "fixture"), ("proof_roots",))
        return cls(row["worker_executable"], row["worker_home"],
                   tuple(tuple(tool) for tool in row["review_tools"]), row["fixture"],
                   tuple(row.get("proof_roots", ())))

    def proof_directories(self, store):
        roles = (Path(self.worker_home), *(Path(home) for _, _, home in self.review_tools))
        roots = tuple(hub_sandbox.private_dir(Path(path)) for path in self.proof_roots)
        _require(len(set(roots)) == len(roots) and not any(
            _overlaps(path, other) for path in roots
            for other in (store.home, *store.native_homes, *roles)
        ), "UNSUPPORTED_CAPABILITY")
        return roots

    def protected_reads(self, store, *, own_home=None):
        roles = (Path(self.worker_home), *(Path(home) for _, _, home in self.review_tools))
        return (store.home, *store.native_homes, *self.proof_directories(store),
                *(path for path in roles if path != own_home))

    def verify(self, store, task, spec, *, work):
        protected = (store.home, Path(task["repo"]["root"]), Path(task["worktree"]["path"]),
                     Path(task["worktree"]["common_dir"]), Path(__file__).resolve().parents[1])
        self.verify_configuration(store, protected=protected)
        if work:
            _require(spec["worker_contract"]["agent"]["kind"] == "codex", "UNSUPPORTED_CAPABILITY")

    def verify_configuration(self, store, *, protected=None):
        protected = protected or (store.home, Path(__file__).resolve().parents[1])
        _require(type(self.fixture) is bool, "UNSUPPORTED_CAPABILITY")
        proof_roots = self.proof_directories(store)
        _require(not any(_overlaps(path, other) for path in proof_roots for other in protected),
                 "UNSUPPORTED_CAPABILITY")
        protected += (*store.native_homes, *proof_roots)
        homes = set()
        worker = Path(self.worker_executable)
        _require(_absolute(self.worker_executable) and worker.is_file() and os.access(worker, os.X_OK),
                 "UNSUPPORTED_CAPABILITY")
        tools = [(tool, home) for _, tool, home in self.review_tools]
        if self.fixture:
            tools.append((self.worker_executable, self.worker_home))
        for executable, home in tools:
            _require(_absolute(executable) and _absolute(home), "UNSUPPORTED_CAPABILITY")
            executable, home = Path(executable), Path(home)
            _require(not any(_overlaps(p, q) for p in (executable, home) for q in protected)
                     and not any(_overlaps(home, old) for old in homes)
                     and executable == executable.resolve(strict=True) and executable.is_file()
                     and os.access(executable, os.X_OK), "UNSUPPORTED_CAPABILITY")
            hub_sandbox.private_dir(home)
            homes.add(home)
        kinds = [kind for kind, _, _ in self.review_tools]
        _require(len(set(kinds)) == len(kinds) and set(kinds) <= set(FACTORIES),
                 "UNSUPPORTED_CAPABILITY")

    def reviewer(self, kind):
        for vendor, path, home in self.review_tools:
            if vendor == kind:
                if self.fixture:
                    return _FixtureReview(path, home)
                return FACTORIES[kind](executable=path, home=home)
        raise TaskError("UNSUPPORTED_CAPABILITY")

    def reviewer_read_fences(self, store, kind):
        if self.fixture:
            return ()
        own_home = next(Path(home) for vendor, _, home in self.review_tools if vendor == kind)
        # A Codex tool runs in a separate native sandbox from its frontend.
        # AGY/Claude frontends authenticate inside the outer fence.
        return self.protected_reads(store, own_home=None if kind == "codex" else own_home)

    def node(self, store, task):
        roots = (store.home, Path(task["repo"]["root"]), Path(task["worktree"]["path"]))
        manifest = Path(__file__).resolve().parents[1] / ".release-manifest.json"
        try:
            if self.fixture:
                return locate("node", forbidden=roots)
            if not manifest.is_file():
                return None
            record = json.loads(safe_record(manifest)[0]).get("tools", {}).get("node")
            if record is None:
                return None
            verify_tools({"node": record})
            return trusted_path(record["path"], forbidden=roots, executable=True)
        except (OSError, ValueError):
            return None


@dataclass(frozen=True)
class _FixtureReview:
    path: str
    home: str

    def run_turn(self, request):
        def invoke():
            tmp = request.runtime_dir / "tmp"
            tmp.mkdir(mode=0o700)
            result = hub_sandbox.run([self.path], workspace=request.workspace, runtime=request.runtime_dir,
                environment=hub_sandbox.clean_environment(self.home, tmp),
                prompt=request.prompt, timeout=request.timeout_seconds, stop=request.stop)
            if result.code != "OK":
                return hub_contract.failed(result.code)
            return hub_contract.HubTurnResult("success", "OK",
                output=hub_contract.validate_review_verdict(hub_contract.strict_json(result.stdout)))
        return hub_sandbox.checked_turn(request, invoke)


def _real_worker_args(executable, adapter_args, controller_args, safe_schema, result_path):
    """Preserve controller overrides while keeping adapter sandbox/approval defaults."""
    controller = list(controller_args)
    has_sandbox = False
    has_approval = False
    has_model = False
    index = 0
    while index < len(controller):
        token = controller[index]
        if token in ("--sandbox", "-s", "--dangerously-bypass-approvals-and-sandbox") or token.startswith(
                ("--sandbox=", "-s=")):
            has_sandbox = True
        elif token in ("--model", "-m") or token.startswith(("--model=", "-m=")):
            has_model = True
        elif token == "-c" and index + 1 < len(controller):
            setting = controller[index + 1]
            if setting.startswith("sandbox_mode="):
                has_sandbox = True
            elif setting.startswith("approval_policy="):
                has_approval = True
            elif setting.startswith("model="):
                has_model = True
            index += 1
        index += 1
    fallback = []
    index = 2  # Skip [<codex>, "exec"].
    while index < len(adapter_args):
        token = adapter_args[index]
        if token == "--sandbox" and index + 1 < len(adapter_args):
            if not has_sandbox:
                fallback.extend((token, adapter_args[index + 1]))
            index += 2
            continue
        if token == "--model" and index + 1 < len(adapter_args):
            if not has_model:
                fallback.extend((token, adapter_args[index + 1]))
            index += 2
            continue
        if token == "-c" and index + 1 < len(adapter_args):
            setting = adapter_args[index + 1]
            if setting.startswith("approval_policy="):
                if not has_approval:
                    fallback.extend((token, setting))
            elif setting.startswith("sandbox_workspace_write."):
                if not has_sandbox:
                    fallback.extend((token, setting))
            index += 2
            continue
        index += 1
    return [executable, "exec", *fallback, *controller,
            "--json", "--output-schema", str(safe_schema), "-o", str(result_path), "-"]


class CommandHost:
    """Receipts precede launch and retain identities if cleanup is ambiguous."""

    def __init__(self, driver, profile, folder, controller_args=()):
        self.driver = driver
        self.profile = profile
        self.folder = folder
        self.controller_args = tuple(controller_args)
        self.advisory = not profile.fixture
        self.processes = {}
        self.cleanup_blocked = False
        self.unknown = False

    def runtime(self):
        runtime = self.folder / ("turn-" + uuid.uuid4().hex)
        runtime.mkdir(mode=0o700)
        return runtime

    def before_spawn(self, runtime):
        self.driver.guard()
        self._process_record(runtime, "launching")

    def _process_record(self, runtime, state, **facts):
        with _directory(self.folder, private_from=self.folder) as fd:
            _save(fd, f"{runtime.name}.{state}.json",
                  dict(state=state, run_id=self.driver.run["run_id"], **facts), exclusive=True)

    def own(self, process, runtime):
        birth = ato_process.identity(process.pid)
        group = hub_sandbox.OwnedGroup(process, birth, {process.pid: birth} if birth else {},
                                       members=ato_process.group_members)
        self.processes[runtime] = group
        self._process_record(runtime, "active", pid=process.pid, birth=birth)
        return group

    def finished(self, runtime, process, cleaned):
        group = self.processes.pop(runtime, None)
        if process is not None and not cleaned:
            self.cleanup_blocked = True
        self._process_record(runtime, "settled" if cleaned else "cleanup_blocked",
                             pid=process.pid if process else None,
                             birth=group.leader_identity if group else None,
                             group_identities=sorted(group.known.items()) if group else [],
                             returncode=process.returncode if process else None)

    def stop(self):
        try:
            self.driver.guard()
        except (ato_core.Halt, TaskError):
            return True
        return False

    def before_attempt(self, owner, row):
        # Runner retry resets only the local counter. The monotonic sequence
        # remains the upper bound for this step even on explicit recovery.
        if not self.advisory and row.get("attempt_sequence", row["attempts"]) >= owner.plan["limits"]["max_attempts"]:
            raise ato_core.Halt("limit")

    def unconfirmed_attempt(self):
        self.unknown = True
        raise ato_core.Halt("interrupted")

    def command(self, owner, argv, name, timeout, input_text, agent_kind):
        owner.guard()
        args = list(argv)
        log = owner.dir / "evidence" / (name + ".log")
        owner.event("command_started", argv=argv, evidence=str(log))
        runtime = self.runtime()
        tmp = runtime / "tmp"
        tmp.mkdir(mode=0o700)
        if self.profile.fixture:
            home = self.profile.worker_home
            environment = hub_sandbox.clean_environment(home, tmp, native_home=Path(home) / ".codex")
            environment["GIT_OPTIONAL_LOCKS"] = "0"
        else:
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"ATO_ACTOR_FILE", "ATO_LAUNCH_SEAL", "ATO_LAUNCHER", "CODEX_THREAD_ID",
                                          "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID"}
                           and not key.startswith("ATO_PRIVATE_")}
            environment["CODEX_HOME"] = str(self.driver.store.native_home)
        output = None
        if agent_kind is not None:
            _require(agent_kind == "codex" and len(args) > 2 and args[1] == "exec",
                     "UNSUPPORTED_CAPABILITY")
            args[0] = self.profile.worker_executable
            schema_index = args.index("--output-schema") + 1
            schema = Path(args[schema_index])
            _require(schema.parent == owner.dir)
            with _directory(schema.parent, private_from=schema.parent) as fd:
                data = _read_file(fd, schema.name, limit=hub_sandbox.LIMIT)
            safe_schema = runtime / "schema.json"
            hub_sandbox.write_private(safe_schema, data.decode("utf-8"))
            args[schema_index] = str(safe_schema)
            destination = args.index("-o") + 1
            output = Path(args[destination])
            _require(output.parent == owner.dir / "checkpoints")
            args[destination] = str(runtime / "result.json")
            if not self.profile.fixture:
                args = _real_worker_args(self.profile.worker_executable, args, self.controller_args,
                                         safe_schema, runtime / "result.json")
        executable = args[0]
        # Resolve frozen relative paths from the actual worktree. Bare
        # command names use the PATH inherited by the Run driver.
        if "/" in executable and not Path(executable).is_absolute():
            args[0] = str(owner.repo / executable)
        environment["PATH"] = hub_sandbox.tool_path(
            self.profile.worker_executable, hub_sandbox.trusted_node.get(), inherit_path=True)
        result = hub_sandbox.run(args, workspace=owner.repo, runtime=runtime,
            environment=environment, prompt=input_text or "", timeout=timeout if self.profile.fixture else None,
            stop=lambda: self._command_stop(owner), truncate=True)
        with log.open("xb") as stream:
            stream.write(result.stdout)
            stream.write(result.stderr)
            if result.truncated:
                stream.write(b"\n[Command output exceeded retained log capacity.]\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not result.cleaned or self.cleanup_blocked:
            raise ato_core.Halt("cleanup_blocked")
        if result.code == "STOPPED":
            self._raise_stop(owner)
        owner.guard()
        if output is not None and result.code == "OK":
            try:
                with _directory(runtime, private_from=runtime) as fd:
                    raw = _read_file(fd, "result.json", limit=hub_sandbox.LIMIT, private=False,
                                     stable_path=True, code="RUN_UNSETTLED")
                result_value = _strict_json(raw, "RUN_UNSETTLED")
                ato_core.save(output, result_value)
            except (OSError, ValueError):
                self.unconfirmed_attempt()
        code = (0 if result.code == "OK" else 124 if result.code == "TIMEOUT"
                else result.returncode or 127)
        if output is not None and code:
            self.unconfirmed_attempt()
        owner.event("command_finished", code=code, evidence=str(log))
        return code, str(log)

    @staticmethod
    def _command_stop(owner):
        try:
            owner.guard()
        except (ato_core.Halt, TaskError):
            return True
        return False

    @staticmethod
    def _raise_stop(owner):
        owner.guard()
        raise ato_core.Halt("interrupted")
