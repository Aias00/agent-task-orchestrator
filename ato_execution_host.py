"""Trusted Worker/check subprocess profile and H1 owned-group integration."""
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid

import ato_core
import ato_process
from ato_task_store import (TaskError, _absolute, _directory, _fields, _overlaps,
                            _read_file, _require, _save, _strict_json)
import hub_contract
import hub_sandbox
from hub_registry import FACTORIES


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

    def document(self):
        return asdict(self)

    @classmethod
    def from_document(cls, row):
        _fields(row, ("worker_executable", "worker_home", "review_tools", "fixture"))
        return cls(row["worker_executable"], row["worker_home"],
                   tuple(tuple(tool) for tool in row["review_tools"]), row["fixture"])

    def verify(self, store, task, spec, *, work):
        protected = (store.home, Path(task["repo"]["root"]), Path(task["worktree"]["path"]),
                     Path(task["worktree"]["common_dir"]), Path(__file__).resolve().parent)
        _require(type(self.fixture) is bool, "UNSUPPORTED_CAPABILITY")
        if not self.fixture:
            _require(Path("/usr/bin/sandbox-exec").is_file(), "UNSUPPORTED_CAPABILITY")
        homes = set()
        for executable, home in [(self.worker_executable, self.worker_home),
                                 *((tool, home) for _, tool, home in self.review_tools)]:
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
        if work:
            contract = spec["worker_contract"]
            # This profile has a bounded Codex filesystem boundary. Profiles for
            # other Worker protocols or wider grants need their own qualified fence.
            _require(contract["agent"]["kind"] == "codex"
                     and contract["sandbox"] in {"workspace-write", "read-only"}
                     and not contract.get("permissions"), "UNSUPPORTED_CAPABILITY")

    def reviewer(self, kind):
        for vendor, path, home in self.review_tools:
            if vendor == kind:
                if self.fixture:
                    return _FixtureReview(path, home)
                return FACTORIES[kind](executable=path, home=home)
        raise TaskError("UNSUPPORTED_CAPABILITY")


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


class CommandHost:
    """Receipts precede launch and retain identities if cleanup is ambiguous."""

    def __init__(self, driver, profile, folder):
        self.driver = driver
        self.profile = profile
        self.folder = folder
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
        except ato_core.Halt:
            return True
        return False

    @staticmethod
    def before_attempt(owner, row):
        # Runner retry resets only the local counter. The monotonic sequence
        # remains the upper bound for this step even on explicit recovery.
        if row.get("attempt_sequence", row["attempts"]) >= owner.plan["limits"]["max_attempts"]:
            raise ato_core.Halt("limit")

    def unconfirmed_attempt(self):
        self.unknown = True
        raise ato_core.Halt("interrupted")

    def _fence(self, argv, runtime, root, *, write):
        if self.profile.fixture:
            return argv
        q = lambda path: json.dumps(str(path))
        from pi_toolchain import write_exclusions
        rules = ["(version 1)", "(allow default)", "(deny file-write*)",
                 '(allow file-write* (literal "/dev/null") (require-all (subpath ' + q(runtime)
                 + ') ' + write_exclusions(runtime) + '))']
        if write:
            rules.append("(allow file-write* (require-all (subpath " + q(root)
                         + ") (require-not (literal " + q(root) + "))))")
        binding = self.driver.task["worktree"]
        for path in (root / ".codex", root / ".git", root / ".ato", runtime / "profile.sb",
                     *(root / name for name in binding["shared_link_identities"]),
                     *(Path(binding["repo"]) / name for name in binding["shared_link_identities"])):
            rules.append("(deny file-write* (subpath " + q(path) + "))")
        rules.append("(deny file-read* (require-all (subpath " + q(self.driver.store.root)
                     + ") (require-not (subpath " + q(runtime) + "))))")
        rules.append("(deny file-read* (subpath " + q(root / ".codex/tasks") + "))")
        profile = runtime / "profile.sb"
        hub_sandbox.write_private(profile, "\n".join(rules))
        return ["/usr/bin/sandbox-exec", "-f", str(profile), *argv]

    def _invoke(self, args, owner, runtime, environment, prompt, timeout, output, write):
        def run(command):
            return hub_sandbox.run(command, workspace=owner.repo, runtime=runtime,
                environment=environment, prompt=prompt, timeout=timeout,
                stop=lambda: self._command_stop(owner), truncate=output is None)

        if self.profile.fixture:
            return run(args)
        root = owner.repo
        if output is None:
            return run(self._fence(args, runtime, root, write=write))
        # Codex creates its own Seatbelt for command tools. Its frontend
        # needs the private output and environment; tool subprocesses
        # use a separate scratch path outside the protected ATO store.
        binding = self.driver.task["worktree"]
        with TemporaryDirectory(prefix="ato-codex-tool-") as scratch:
            temporary = Path(scratch).resolve()
            environment["TMPDIR"] = str(temporary)
            mode = args.index("--sandbox")
            args[mode:mode + 2] = hub_sandbox.guarded_codex_args(root, temporary,
                writable=write, denied=(self.driver.store.root, root / ".codex/tasks"),
                read_only=(root / ".git", root / ".codex", root / ".ato",
                           *(root / name for name in binding["shared_link_identities"])))
            return run(args)

    def command(self, owner, argv, name, timeout, input_text, agent_kind):
        owner.guard()
        args = list(argv)
        log = owner.dir / "evidence" / (name + ".log")
        owner.event("command_started", argv=argv, evidence=str(log))
        runtime = self.runtime()
        tmp = runtime / "tmp"
        tmp.mkdir(mode=0o700)
        home = self.profile.worker_home
        environment = hub_sandbox.clean_environment(home, tmp, native_home=Path(home) / ".codex")
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        output = None
        write = True
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
            write = args[args.index("--sandbox") + 1] == "workspace-write"
        executable = args[0]
        # Resolve frozen relative paths from the actual worktree and use only
        # the sanitized child PATH for unqualified executable names.
        if "/" in executable and not Path(executable).is_absolute():
            args[0] = str(owner.repo / executable)
        result = self._invoke(args, owner, runtime, environment, input_text or "", timeout, output, write)
        with log.open("xb") as stream:
            stream.write(result.stdout)
            stream.write(result.stderr)
            if result.truncated:
                stream.write(b"\n[Independent check output exceeded retained log capacity.]\n")
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
