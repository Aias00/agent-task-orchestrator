"""Offline T2 tests using real Git, Workflow and the public service entry points."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import hashlib
import json
import os
import stat
import subprocess
import time
import unittest
from unittest.mock import patch

import ato_config
import ato_core
import ato_task_store as ato
import workflow


def git(repo, *args, env=None):
    environment = dict(PATH=os.defpath, LC_ALL="C", GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_NOSYSTEM="1", GIT_ALLOW_PROTOCOL="",
                       GIT_AUTHOR_NAME="ATO Offline Fixture", GIT_AUTHOR_EMAIL="ato@example.invalid",
                       GIT_COMMITTER_NAME="ATO Offline Fixture", GIT_COMMITTER_EMAIL="ato@example.invalid")
    environment.update(env or {})
    return subprocess.run(["git", "-C", str(repo), *args], env=environment, check=True,
                          capture_output=True, text=True, timeout=30).stdout.strip()


@contextmanager
def private_umask():
    before = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(before)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory(prefix="ato-unified-task-"))
        self.root = Path(folder).resolve()
        self.home, self.native = self.root / "ato", self.root / "native"
        self.enterContext(patch.dict(os.environ, ATO_HOME=str(self.home), CODEX_HOME=str(self.native)))
        self.repo = self.make_repo("one/repo")
        self.binding = ato.InitialBinding("controller-1", "interaction-1")
        self.submit = object()
        self.observer = object()
        self.other_actor = object()
        self.allowed_repos = set()
        self.store = self.make_store()

    def make_repo(self, path):
        repo = self.root / path
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        (repo / "source.txt").write_text("source\n")
        (repo / ".gitignore").write_text(".codex/tasks/\n.venv/\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "base")
        return repo

    def make_store(self, **overrides):
        settings = dict(runtime_seal="a" * 64, authenticate=self.authenticate,
                        can_observe=self.can_observe, authorize_submit=self.authorize_submit)
        return ato.TaskStore(**(settings | overrides))

    def authenticate(self, proof):
        if proof is self.observer or proof is self.other_actor:
            return proof
        return None

    def can_observe(self, principal, scope):
        return (principal is self.observer and scope.repo_key in self.allowed_repos
                and scope.authorization_ref == "verified-grant")

    def authorize_submit(self, spec, proof, binding):
        if proof is self.submit and binding == self.binding:
            return ato.SubmissionGrant("verified-grant", spec.repository.key, spec.spec_sha256, binding)
        return None

    def spec_input(self, repo=None):
        return dict(schema_version=1, repo=str(repo or self.repo), base_ref="HEAD", worker_contract=dict(
            objective="local implementation", scope="temporary repository only",
            authorization="description-only-attribution",
            acceptance=[
                dict(id="test", description="The independent check passes", checks=[["true"]]),
                dict(id="user", description="The user accepts this product", kind="manual"),
            ]))

    def spec_file(self, value=None, *, path=None):
        path = path or self.root / "spec.json"
        path.write_text(json.dumps(self.spec_input() if value is None else value, ensure_ascii=False, indent=2))
        return path

    def normalized(self, repo=None, value=None):
        repo = repo or self.repo
        spec = self.store.normalize_spec(repo, self.spec_file(value or self.spec_input(repo)))
        self.allowed_repos.add(spec.repository.key)
        return spec

    def create(self, spec=None, *, intent_id=None):
        return self.store.create_task(spec or self.normalized(), self.submit, self.binding,
                                      intent_id=intent_id)["task_id"]

    def assert_code(self, expected, function, *args, task_id=None, **kwargs):
        with self.assertRaises(ato.TaskError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(caught.exception.task_id, task_id)
        return caught.exception

    def root_snapshot(self):
        items = {}
        for path in self.root.rglob("*"):
            info = path.lstat()
            payload = (os.readlink(path) if path.is_symlink() else path.read_bytes()
                       if stat.S_ISREG(info.st_mode) else None)
            items[str(path.relative_to(self.root))] = (info.st_mode, info.st_mtime_ns, payload)
        return items

    def test_normalization_uses_exact_git_roots_and_freezes_base_document_bytes(self):
        (self.repo / "design.md").write_text("base design\n")
        (self.repo / "redirect.md").symlink_to("design.md")
        git(self.repo, "add", "design.md", "redirect.md")
        git(self.repo, "commit", "-qm", "design")
        base = git(self.repo, "rev-parse", "HEAD")
        linked = self.root / "linked"
        git(self.repo, "worktree", "add", "--detach", str(linked), base)
        alias = self.root / "alias"
        alias.symlink_to(linked, target_is_directory=True)
        (self.repo / "design.md").write_text("uncommitted change\n")
        (self.repo / "subdirectory").mkdir()
        rival = self.make_repo("two/repo")
        bare = self.root / "bare"
        bare.mkdir()
        git(bare, "init", "--bare", "-q")
        value = self.spec_input()
        value["repo"] = "one/repo"
        value["worker_contract"]["objective"] = "Implement café support"
        value["reviewer"] = dict(agent=dict(kind="codex", model="fixture"), design_docs=["design.md"])
        spec_file = self.spec_file(value)
        with patch.dict(os.environ, GIT_DIR=str(rival / ".git"), GIT_WORK_TREE=str(rival),
                        PATH=str(self.root / "untrusted-bin")):
            spec = self.store.normalize_spec(alias, spec_file)
        self.assertEqual(spec.repository, ato.normalize_repository(linked))
        relative = os.path.relpath(self.repo, Path.cwd())
        self.assertEqual(spec.repository, ato.normalize_repository(relative))
        self.assertEqual(spec.repository.root, str(self.repo))
        self.assertEqual(spec.repository.key, str(self.repo / ".git"))
        self.assertEqual(spec.preview(), dict(repo=str(self.repo), base_commit=base, spec_sha256=spec.spec_sha256))
        self.assertEqual(spec.input_sha256, hashlib.sha256(spec_file.read_bytes()).hexdigest())
        self.assertEqual(spec.spec_sha256, hashlib.sha256(spec.canonical).hexdigest())
        self.assertIn("café".encode(), spec.canonical)
        self.assertFalse(spec.canonical.endswith(b"\n"))
        worker = spec.document["worker_contract"]
        self.assertIs(worker["memory"], False)
        self.assertEqual(worker["agent"], {"kind": "codex"})
        self.assertEqual(worker["stages"][0]["id"], "hub-initial")
        self.assertEqual(worker["stages"][0]["plan"]["tasks"][0]["checks"], [["true"]])
        self.assertEqual(spec.document["reviewer"]["design_docs"],
                         [dict(path="design.md", sha256=hashlib.sha256(b"base design\n").hexdigest())])
        for invalid in (self.repo / "subdirectory", rival, bare, self.root):
            with self.subTest(invalid=invalid.name):
                self.assert_code("INVALID_REPO", self.store.normalize_spec, invalid, spec_file)
        value["reviewer"]["design_docs"] = ["redirect.md"]
        self.assert_code("INVALID_SPEC", self.store.normalize_spec, alias, self.spec_file(value))
        self.assertFalse(self.home.exists())
        self.assertFalse(self.native.exists())

    def test_strict_spec_and_safe_file_open_reject_invalid_input_without_state(self):
        spec_file = self.spec_file()
        good = spec_file.read_bytes()
        malformed = [
            b'{"x":{"y":0,"y":1}}', b'{"x":1e1000}', b'{"x":NaN}', b'{"x":"\\ud800"}',
            b'{"x":"\xff"}', b'\xef\xbb\xbf' + good, good + b'{}',
            b'{"x":' + b'[' * 32 + b'0' + b']' * 32 + b'}',
        ]
        for raw in malformed:
            with self.subTest(raw=raw[:30]):
                spec_file.write_bytes(raw)
                self.assert_code("INVALID_JSON", self.store.normalize_spec, self.repo, spec_file)
        invalid_values = []
        for field, new_value in (("schema_version", True), ("base_ref", "-not-a-ref"),
                                 ("base_ref", "$(touch unrelated)"), ("worktree", {"branch": "main"})):
            value = self.spec_input()
            value[field] = new_value
            invalid_values.append(value)
        value = self.spec_input()
        value["worker_contract"]["acceptance"][-1]["checks"] = []
        invalid_values.append(value)
        value = self.spec_input()
        value["worker_contract"]["acceptance"].pop()
        invalid_values.append(value)
        value = self.spec_input()
        value["worker_contract"]["acceptance"][0]["checks"] = [["x\x00"]]
        invalid_values.append(value)
        value = self.spec_input()
        value["worker_contract"]["memory"] = True
        invalid_values.append(value)
        value = self.spec_input()
        value["worker_contract"]["stages"] = [dict(id="first", covers=["test"], plan=dict(
            objective="stage", tasks=[dict(id="step", prompt="work", checks=[["different-check"]])]))]
        invalid_values.append(value)
        override = deepcopy(value)
        override["worker_contract"]["stages"][0]["plan"].update(memory=True)
        invalid_values.append(override)
        duplicate = deepcopy(value)
        plan = duplicate["worker_contract"]["stages"][0]["plan"]
        plan["tasks"] = [
            dict(id="step", prompt="work", checks=[["true"]]),
            dict(id="next", prompt="work", checks=[["true"]], depends_on=["step", "step"]),
        ]
        invalid_values.append(duplicate)
        for value in invalid_values:
            with self.subTest(value=value):
                self.assert_code("INVALID_SPEC", self.store.normalize_spec, self.repo, self.spec_file(value))
        value = self.spec_input()
        value["schema_version"] = 2
        self.assert_code("UNSUPPORTED_VERSION", self.store.normalize_spec, self.repo, self.spec_file(value))
        spec_file.write_bytes(good)
        symlink = self.root / "file-link"
        symlink.symlink_to(spec_file)
        parent_link = self.root / "parent-link"
        parent_link.symlink_to(self.root, target_is_directory=True)
        self.assert_code("INVALID_SPEC", self.store.normalize_spec, self.repo, symlink)
        self.assert_code("INVALID_SPEC", self.store.normalize_spec, self.repo, parent_link / spec_file.name)
        hardlink = self.root / "file-hardlink"
        os.link(spec_file, hardlink)
        self.assert_code("INVALID_SPEC", self.store.normalize_spec, self.repo, spec_file)
        hardlink.unlink()
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        self.assert_code("INVALID_SPEC", self.store.normalize_spec, self.repo, fifo)
        spec_file.write_bytes(b" " * (1024 * 1024 + 1))
        self.assert_code("INVALID_SPEC", self.store.normalize_spec, self.repo, spec_file)
        self.assertFalse(self.home.exists())

    def test_root_selection_and_unauthorized_queries_or_submissions_are_read_only(self):
        self.native.mkdir(mode=0o700)
        (self.native / "legacy-marker").write_text("leave native data in place")
        fake_home = self.root / "user-home"
        fake_home.mkdir()
        with patch.dict(os.environ, HOME=str(fake_home)):
            del os.environ["ATO_HOME"]
            try:
                store = self.make_store()
                self.assertEqual(store.root, fake_home / ".local/share/ato/unified-v1")
                self.assertEqual(store.list_tasks(self.observer), {"tasks": [], "warnings": []})
                self.assert_code("NOT_FOUND", store.load_task, ato.new_id("task"))
                self.assertEqual(sorted(path.name for path in self.native.iterdir()), ["legacy-marker"])
                self.assertFalse((fake_home / ".local").exists())
                self.assertEqual(ato_config.ato_home(), self.native)
                with ato_config.use_unified_kernel(store.kernel_root, self.root / "isolated-native"):
                    self.assertEqual(ato_config.ato_home(), store.kernel_root)
                    self.assertEqual(ato_config.codex_home(), self.root / "isolated-native")
                    self.assertEqual(ato_config.interaction_directory(),
                                     store.kernel_root / "ctask-interaction")
                self.assertEqual(ato_config.ato_home(), self.native)
            finally:
                os.environ["ATO_HOME"] = str(self.home)
        spec = self.normalized()
        self.assert_code("AUTH_REQUIRED", self.store.create_task, spec, None, self.binding)
        self.assert_code("AUTH_REQUIRED", self.store.list_tasks, None)
        self.assert_code("INVALID_ID", self.store.task_status, self.observer, self.repo)
        self.assert_code("NOT_FOUND", self.store.task_status, self.observer, ato.new_id("task"))
        wrong_grant = self.make_store(authorize_submit=lambda spec, proof, binding: ato.SubmissionGrant(
            "verified-grant", spec.repository.key, "b" * 64, binding))
        self.assert_code("FORBIDDEN", wrong_grant.create_task, spec, self.submit, self.binding)
        self.assertFalse(self.home.exists())
        self.assert_code("BINDING_CHANGED", self.make_store, native_home=self.home / "overlap")
        within_repo = self.make_store(home=self.repo / "state")
        self.assert_code("INVALID_REPO", within_repo.normalize_spec, self.repo, self.spec_file())
        self.assertFalse((self.repo / "state").exists())

    def test_creation_binds_real_worktree_and_workflow_and_queries_do_not_write(self):
        secondary = self.make_repo("two/repo")
        old_store = self.home / "legacy"
        old_store.mkdir(parents=True, mode=0o700)
        self.home.chmod(0o700)
        (old_store / "marker").write_text("legacy")
        self.native.mkdir(mode=0o700)
        (self.native / "marker").write_text("native")
        with patch.dict(os.environ, CODEX_THREAD_ID="inherited-session-must-not-be-used",
                        CTASK_SESSION_REF="inherited-ref", GIT_DIR=str(secondary / ".git")):
            first = self.create()
            second = self.create(self.normalized(secondary))
        self.assertNotEqual(first, second)
        task = self.store.load_task(first)
        run = self.store.load_run(first, task["run_id"])
        worktree = Path(task["worktree"]["path"])
        kernel = Path(task["workflow"]["directory"])
        self.assertEqual(worktree, self.repo.parent / first)
        self.assertEqual(self.store.verify_worktree(first), worktree)
        self.assertEqual(run["workflow_dir"], str(kernel))
        self.assertNotEqual(run["workflow_id"], run["run_id"])
        self.assertEqual(run["generation"], [None, 0])
        self.assertEqual(run["phase"], "reserved")
        self.assertEqual(git(worktree, "symbolic-ref", "HEAD"), "refs/heads/ato/" + first)
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), task["creation"]["base_commit"])
        self.assertEqual(ato_core.read(kernel / "state.json")["status"], "created")
        self.assertIsNone(ato_core.read(kernel / "origin.json")["origin_thread_id"])
        self.assertEqual(ato_core.read(kernel / "ato-binding.json")["task_id"], first)
        self.assertTrue((self.store.kernel_root / "ctask-interaction" / "control").is_dir())
        self.assertFalse((self.home / "ctask-interaction").exists())
        self.assertEqual((old_store / "marker").read_text(), "legacy")
        self.assertEqual(list(self.native.iterdir()), [self.native / "marker"])
        self.assertFalse((self.repo / ".codex/tasks").exists())

        snapshot = self.root_snapshot()
        listing = self.store.list_tasks(self.observer)
        self.assertEqual([row["task_id"] for row in listing["tasks"]], sorted([first, second]))
        self.assertEqual(listing["warnings"], [])
        self.assertEqual(self.store.list_tasks(self.observer, worktree)["tasks"][0]["task_id"], first)
        self.assertEqual(self.store.list_tasks(self.other_actor), {"tasks": [], "warnings": []})
        self.assert_code("NOT_FOUND", self.store.task_status, self.other_actor, first)
        status = self.store.task_status(self.observer, first)
        self.assertEqual((status["status"], status["next_step"]), ("preparing", "ADMIT_INITIAL_RUN"))
        self.assertEqual(status["stage"]["state"], "created")
        self.assertEqual(status["execution"]["run_id"], run["run_id"])
        self.assertEqual(status["acceptance"]["criteria"], ["user"])
        self.assertIsNone(status["product"])
        self.assertEqual(self.store.task_snapshot(first)["generation"], [None, 0])
        for hidden in ("description-only-attribution", "controller-1", str(kernel), run["workflow_id"]):
            self.assertNotIn(hidden, json.dumps(status))
        self.assertEqual(snapshot, self.root_snapshot())

    def test_distinct_concurrent_submissions_and_retries_of_one_intent(self):
        spec = self.normalized()
        go = Barrier(2)

        def submit_independently():
            go.wait(timeout=10)
            return self.create(spec)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit_independently)
            second = pool.submit(submit_independently)
            identifiers = {first.result(timeout=30), second.result(timeout=30)}
        self.assertEqual(len(identifiers), 2)
        entered, release = Event(), Event()
        intent = ato.new_id("task")
        actual_kernel_create = self.store._kernel_create

        def pause_at_workflow(*args):
            entered.set()
            if not release.wait(timeout=15):
                raise AssertionError("concurrent retry did not finish")
            return actual_kernel_create(*args)

        with patch.object(self.store, "_kernel_create", side_effect=pause_at_workflow):
            with ThreadPoolExecutor(max_workers=1) as pool:
                worker = pool.submit(self.create, spec, intent_id=intent)
                try:
                    self.assertTrue(entered.wait(timeout=15))
                    with self.assertRaises(ato.TaskError) as busy:
                        self.create(spec, intent_id=intent)
                    self.assertEqual(busy.exception.code, "BUSY")
                    view = self.store.task_status(self.observer, intent)
                    self.assertEqual(view["status"], "preparing")
                    self.assertEqual(view["run_history"], [])
                finally:
                    release.set()
                self.assertEqual(worker.result(timeout=30), intent)
        self.assertEqual(self.create(spec, intent_id=intent), intent)
        self.assertEqual(self.store.load_task(intent)["run_count"], 1)
        self.assertEqual(len(self.store.list_tasks(self.observer)["tasks"]), 3)

    def test_competing_reservations_cannot_replace_a_published_intent(self):
        first_spec = self.normalized()
        changed = self.spec_input()
        changed["worker_contract"]["objective"] = "different local implementation"
        second_spec = self.normalized(value=changed)
        intent = ato.new_id("task")
        staged = Barrier(2)
        publish = ato._publish

        def synchronize_reservations(fd, name, data, **options):
            publish(fd, name, data, **options)
            if name == "evidence.lock":
                staged.wait(timeout=15)

        def submit(store, spec):
            try:
                return store.create_task(spec, self.submit, self.binding, intent_id=intent)
            except ato.TaskError as error:
                return error

        with patch.object(ato, "_publish", side_effect=synchronize_reservations):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(submit, self.store, first_spec)
                second = pool.submit(submit, self.make_store(), second_spec)
                results = [first.result(timeout=30), second.result(timeout=30)]

        successes = [result for result in results if isinstance(result, dict)]
        errors = [result for result in results if isinstance(result, ato.TaskError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual([(error.code, error.task_id) for error in errors], [("NOT_FOUND", None)])
        task = self.store.load_task(intent)
        self.assertEqual(successes[0], {"task_id": intent, "spec_sha256": task["spec_sha256"]})
        self.assertEqual(task["run_count"], 1)
        self.assertEqual(sorted(path.name for path in self.store.tasks.iterdir()), [intent])

    def test_collision_does_not_overwrite_or_adopt_preexisting_identity(self):
        spec = self.normalized()
        occupied_id = self.create(spec)
        fresh_id = ato.new_id("task")
        real_new_id = ato.new_id
        ids = iter((occupied_id, fresh_id))
        with patch.object(ato, "new_id", side_effect=lambda kind: next(ids) if kind == "task" else real_new_id(kind)):
            self.assertEqual(self.create(spec), fresh_id)
        self.assertEqual(self.store.load_task(occupied_id)["run_count"], 1)
        wrong_input = self.spec_input()
        wrong_input["worker_contract"]["objective"] = "different authorization target"
        foreign = self.normalized(value=wrong_input)
        self.assert_code("NOT_FOUND", self.store.create_task, foreign, self.submit, self.binding,
                         intent_id=occupied_id)

        branch_collision = ato.new_id("task")
        git(self.repo, "branch", "ato/" + branch_collision)
        self.assert_code("BINDING_CHANGED", self.create, spec, intent_id=branch_collision,
                         task_id=branch_collision)
        self.assertFalse((self.repo.parent / branch_collision).exists())
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/ato/" + branch_collision),
                         spec.document["base_ref"])
        worktree_collision = ato.new_id("task")
        colliding = self.repo.parent / worktree_collision
        git(self.repo, "worktree", "add", "-b", "ato/" + worktree_collision, str(colliding))
        marker = colliding / "source.txt"
        original = marker.read_bytes()
        self.assert_code("BINDING_CHANGED", self.create, spec, intent_id=worktree_collision,
                         task_id=worktree_collision)
        self.assertEqual(marker.read_bytes(), original)
        self.assertFalse((colliding / ".codex/tasks").exists())
        task = self.store.task_status(self.observer, worktree_collision)
        self.assertEqual((task["status"], task["next_step"]), ("preparing", "RECONCILE_CREATION"))
        self.assertEqual(task["run_history"], [])

    def test_interrupt_before_reservation_and_after_git_effect_recovers_same_intent(self):
        spec = self.normalized()
        intent = ato.new_id("task")
        save = ato._save

        def cut_before_reservation(fd, name, *args, **options):
            save(fd, name, *args, **options)
            if name == "scope.json":
                raise KeyboardInterrupt

        with patch.object(ato, "_save", side_effect=cut_before_reservation):
            with self.assertRaises(KeyboardInterrupt):
                self.create(spec, intent_id=intent)
        self.assertEqual(self.store.list_tasks(self.observer), {"tasks": [], "warnings": []})
        self.assertFalse((self.repo.parent / intent).exists())
        self.assertEqual(git(self.repo, "branch", "--list", "ato/" + intent), "")
        run_git = ato._git

        def cut_after_git(repo, *args, **options):
            output = run_git(repo, *args, **options)
            if args[:2] == ("worktree", "add"):
                raise KeyboardInterrupt
            return output

        with patch.object(ato, "_git", side_effect=cut_after_git):
            with self.assertRaises(KeyboardInterrupt):
                self.create(spec, intent_id=intent)
        reserved = self.store.load_task(intent)
        self.assertEqual(reserved["creation"]["phase"], "git_pending")
        target = Path(reserved["creation"]["target"])
        inode = target.stat().st_ino
        listed = self.store.list_tasks(self.observer)
        self.assertEqual(listed["tasks"][0]["status"], "preparing")
        self.assertEqual(self.create(spec, intent_id=intent), intent)
        ready = self.store.load_task(intent)
        self.assertEqual(ready["run_id"], reserved["creation"]["initial_run_id"])
        self.assertEqual(Path(ready["worktree"]["path"]).stat().st_ino, inode)
        self.assertEqual(len(list((target / ".codex/tasks/workflows").iterdir())), 1)

    def test_interrupt_after_workflow_and_after_run_write_never_duplicates_effects(self):
        spec = self.normalized()
        intent = ato.new_id("task")
        initialize = self.store._kernel_create

        def cut_after_workflow(*args):
            initialize(*args)
            raise KeyboardInterrupt

        with patch.object(self.store, "_kernel_create", side_effect=cut_after_workflow):
            with self.assertRaises(KeyboardInterrupt):
                self.create(spec, intent_id=intent)
        task = self.store.load_task(intent)
        path = Path(task["worktree"]["path"]) / ".codex/tasks/workflows"
        original = list(path.iterdir())
        self.assertEqual(len(original), 1)
        initial_run = task["creation"]["initial_run_id"]
        publish = self.store._advance

        def cut_before_commit(fd, candidate):
            if candidate["creation"]["phase"] == "ready":
                raise KeyboardInterrupt
            return publish(fd, candidate)

        with patch.object(self.store, "_advance", side_effect=cut_before_commit):
            with self.assertRaises(KeyboardInterrupt):
                self.create(spec, intent_id=intent)
        self.assertEqual(self.store.task_status(self.observer, intent)["run_history"], [])
        self.assert_code("NOT_FOUND", self.store.load_run, intent, initial_run)
        orphan = self.store.tasks / intent / "runs" / (initial_run + ".json")
        self.assertTrue(orphan.is_file())
        orphan_inode = orphan.stat().st_ino
        self.assertEqual(self.create(spec, intent_id=intent), intent)
        ready = self.store.load_task(intent)
        self.assertEqual(list(path.iterdir()), original)
        self.assertEqual(ready["run_id"], initial_run)
        self.assertEqual(orphan.stat().st_ino, orphan_inode)

    def test_partial_workflow_is_fenced_and_kept_for_reconciliation(self):
        spec = self.normalized()
        intent = ato.new_id("task")

        def abort_inside_kernel(_task, worktree, _lock_fd):
            incomplete = worktree / ".codex/tasks/workflows/19990101-000000-00000000"
            incomplete.mkdir(mode=0o700)
            raise KeyboardInterrupt

        with patch.object(self.store, "_kernel_create", side_effect=abort_inside_kernel):
            with self.assertRaises(KeyboardInterrupt):
                self.create(spec, intent_id=intent)
        with patch.object(self.store, "_kernel_create", side_effect=AssertionError("duplicate creation")):
            self.assert_code("BINDING_CHANGED", self.create, spec, intent_id=intent, task_id=intent)
            self.assert_code("BINDING_CHANGED", self.create, spec, intent_id=intent, task_id=intent)
        task = self.store.load_task(intent)
        self.assertEqual(task["creation"]["phase"], "workflow_pending")
        self.assertEqual(task["creation"]["problem"], "BINDING_CHANGED")
        parents = Path(task["worktree"]["path"]) / ".codex/tasks/workflows"
        self.assertEqual([path.name for path in parents.iterdir()], ["19990101-000000-00000000"])
        self.assertEqual(self.store.task_status(self.observer, intent)["next_step"], "RECONCILE_CREATION")

    def test_corruption_permissions_and_spec_tampering_are_visible_only_in_scope(self):
        task_id = self.create()
        location = self.store.tasks / task_id
        frozen = location / "spec.json"
        original = frozen.read_bytes()
        frozen.write_bytes(original.replace(b"local implementation", b"secret changed text"))
        error = self.assert_code("SPEC_CHANGED", self.store.task_status, self.observer, task_id,
                                 task_id=task_id)
        response = ato.result_envelope(error=error)
        self.assertEqual(response["error"]["task_id"], task_id)
        self.assertNotIn("secret changed text", json.dumps(response))
        self.assertEqual(error.exit_code, 5)
        self.assertEqual(self.store.list_tasks(self.observer), {"tasks": [], "warnings": ["SPEC_CHANGED"]})
        self.assertEqual(self.store.list_tasks(self.other_actor), {"tasks": [], "warnings": []})
        missing = self.assert_code("NOT_FOUND", self.store.task_status, self.other_actor, task_id)
        self.assertEqual(ato.result_envelope(error=missing),
                         ato.result_envelope(error=self.assert_code("NOT_FOUND", self.store.task_status,
                                                                    self.other_actor, ato.new_id("task"))))
        frozen.write_bytes(original)
        os.link(frozen, self.root / "spec-alias")
        self.assert_code("SPEC_CHANGED", self.store.task_status, self.observer, task_id, task_id=task_id)
        (self.root / "spec-alias").unlink()

        task_file = location / "task.json"
        original = task_file.read_bytes()
        task_file.chmod(0o644)
        self.assert_code("STATE_CORRUPT", self.store.task_status, self.observer, task_id, task_id=task_id)
        task_file.chmod(0o600)
        task_file.write_bytes(b'{"schema_version":1,"schema_version":1}')
        self.assert_code("STATE_CORRUPT", self.store.task_status, self.observer, task_id, task_id=task_id)
        self.assertEqual(self.store.list_tasks(self.observer)["warnings"], ["STATE_CORRUPT"])
        task_file.write_bytes(original)
        task = self.store.load_task(task_id)
        kernel_contract = Path(task["workflow"]["directory"]) / "contract.json"
        kernel_output = kernel_contract.read_bytes()
        kernel_contract.write_bytes(kernel_output.replace(b"local implementation", b"other objective"))
        self.assert_code("SPEC_CHANGED", self.store.task_status, self.observer, task_id, task_id=task_id)
        kernel_contract.write_bytes(kernel_output)
        backlink = Path(task["workflow"]["directory"]) / "ato-binding.json"
        backlink.write_bytes(backlink.read_bytes().replace(task_id.encode(), ato.new_id("task").encode()))
        self.assert_code("BINDING_CHANGED", self.store.task_status, self.observer, task_id, task_id=task_id)

    def test_state_roots_do_not_follow_symlinks_or_loosen_modes(self):
        directory = self.root / "real-home"
        directory.mkdir(mode=0o700)
        self.home.symlink_to(directory, target_is_directory=True)
        spec = self.normalized()
        self.assert_code("STATE_CORRUPT", self.create, spec)
        self.assert_code("STATE_CORRUPT", self.store.list_tasks, self.observer)
        self.assertEqual(list(directory.iterdir()), [])
        self.home.unlink()
        self.home.mkdir(mode=0o700)
        self.home.chmod(0o755)
        self.assert_code("STATE_CORRUPT", self.create, spec)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o755)

    def test_moved_repository_worktree_or_shared_dependency_cannot_substitute(self):
        dep = self.repo / ".venv"
        dep.mkdir()
        (dep / "dependency").touch()
        value = self.spec_input()
        value["worktree"] = {"shared_env_links": [".venv"]}
        spec = self.normalized(value=value)
        task_id = self.create(spec)
        task = self.store.load_task(task_id)
        target = Path(task["worktree"]["path"])
        self.assertTrue((target / ".venv").is_symlink())
        self.assertEqual((target / ".venv").resolve(), dep)
        moved = self.repo / ".venv-moved"
        dep.rename(moved)
        dep.mkdir()
        self.assert_code("BINDING_CHANGED", self.store.verify_worktree, task_id, task_id=task_id)
        dep.rmdir()
        moved.rename(dep)
        self.assertEqual(self.store.verify_worktree(task_id), target)

        redirected = target.with_name("worktree-moved")
        target.rename(redirected)
        target.symlink_to(redirected, target_is_directory=True)
        self.assert_code("BINDING_CHANGED", self.store.task_status, self.observer, task_id, task_id=task_id)
        target.unlink()
        redirected.rename(target)
        self.assertEqual(self.store.verify_worktree(task_id), target)

        moved_repo = self.repo.with_name("repo-moved")
        self.repo.rename(moved_repo)
        self.repo.symlink_to(moved_repo, target_is_directory=True)
        self.assert_code("BINDING_CHANGED", self.create, spec)
        self.assert_code("BINDING_CHANGED", self.store.task_status, self.observer, task_id, task_id=task_id)

    def test_provisioning_isolates_git_hooks_filters_and_ambient_origin(self):
        attributes = self.repo / ".gitattributes"
        attributes.write_text("source.txt filter=unsafe-fixture\n")
        git(self.repo, "add", attributes.name)
        git(self.repo, "commit", "-qm", "filter attribute")
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "local-program-ran"
        script = hooks / "post-checkout"
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n')
        script.chmod(0o700)
        git(self.repo, "config", "core.hooksPath", str(hooks))
        git(self.repo, "config", "filter.unsafe-fixture.smudge", str(script))
        git(self.repo, "config", "filter.unsafe-fixture.clean", str(script))
        git(self.repo, "config", "filter.unsafe-fixture.required", "true")
        task_id = self.create()
        task = self.store.load_task(task_id)
        self.assertFalse(marker.exists())
        self.assertEqual((Path(task["worktree"]["path"]) / "source.txt").read_text(), "source\n")
        self.assertEqual(git(self.repo, "config", "filter.unsafe-fixture.smudge"), str(script))
        self.assertEqual(git(self.repo, "config", "core.hooksPath"), str(hooks))

    def test_receipts_are_monotonic_and_unknown_previous_run_fences_new_run(self):
        task_id = self.create()
        task = self.store.load_task(task_id)
        initial = task["run_id"]
        evidence = b"trusted offline fixture receipt"
        digest = self.store.put_evidence(task_id, evidence)
        self.assertEqual(digest, self.store.put_evidence(task_id, evidence))
        self.assertEqual(self.store.read_evidence(task_id, digest), evidence)

        def receipt(phase, outcome=None, **overrides):
            fields = dict(task_id=task_id, run_id=initial, spec_sha256=task["spec_sha256"],
                          runtime_seal="a" * 64, generation=(None, 0), phase=phase,
                          outcome=outcome, evidence_sha256=digest, recorded_at=time.time())
            return ato.RunReceipt(**(fields | overrides))

        launch = receipt("launching")
        task = self.store.record_run_receipt(task_id, initial, launch, task["revision"])
        self.assertEqual(task, self.store.record_run_receipt(task_id, initial, launch, task["revision"]))
        stale_revision = task["revision"]
        task = self.store.record_run_receipt(task_id, initial, receipt("unknown"), task["revision"])
        self.assertEqual(self.store.task_status(self.observer, task_id)["status"], "delivery_unknown")
        resume = ato.RunIntent(ato.new_id("run"), initial, "resume", 2, "USER_RESUME", "a" * 64, (None, 0))
        self.assert_code("BUSY", self.store.append_run, task_id, resume, stale_revision)
        self.assert_code("RUN_UNSETTLED", self.store.append_run, task_id, resume, task["revision"])
        self.assert_code("RUN_UNSETTLED", self.store.record_run_receipt, task_id, initial, receipt("launching"),
                         task["revision"])
        self.assert_code("RUN_UNSETTLED", self.store.record_run_receipt, task_id, initial,
                         receipt("settled", "failed", runtime_seal="b" * 64), task["revision"])
        self.assert_code("RUN_UNSETTLED", self.store.record_run_receipt, task_id, initial,
                         receipt("active", generation=(time.time(), 99)), task["revision"])
        task = self.store.record_run_receipt(task_id, initial, receipt("launch_failed", "launch_failed"),
                                             task["revision"])
        self.assert_code("BINDING_CHANGED", self.store.append_run, task_id,
                         replace(resume, generation=(time.time(), 99)), task["revision"])
        new = self.store.append_run(task_id, resume, task["revision"])
        self.assertEqual(new["workflow"], task["workflow"])
        self.assertEqual(new["worktree"], task["worktree"])
        self.assertEqual(new["run_id"], resume.run_id)
        self.assertEqual(self.store.load_run(task_id, initial)["phase"], "launch_failed")
        self.assertEqual(self.store.load_run(task_id, resume.run_id)["predecessor_run_id"], initial)
        late = receipt("settled", "failed")
        self.assert_code("RUN_UNSETTLED", self.store.record_run_receipt, task_id, initial, late, new["revision"])
        late_bytes = ato.canonical_json(vars(late))
        self.assertEqual(self.store.read_evidence(task_id, hashlib.sha256(late_bytes).hexdigest()), late_bytes)
        self.assertEqual([row["kind"] for row in self.store.task_status(self.observer, task_id)["run_history"]],
                         ["initial", "resume"])
        blob = self.store.tasks / task_id / "evidence" / digest
        blob.write_bytes(b"altered proof")
        self.assert_code("STATE_CORRUPT", self.store.task_status, self.observer, task_id, task_id=task_id)

    def test_query_projects_authoritative_kernel_stage_without_treating_checks_as_acceptance(self):
        task_id = self.create()
        task = self.store.load_task(task_id)
        original = (self.store.tasks / task_id / "task.json").read_bytes()
        directory = Path(task["workflow"]["directory"])
        with private_umask():
            engine = workflow.Workflow(directory)
            first = engine.contract["stages"][0]
            engine.stage(first["plan"], first["covers"], first["id"])
            child_dir = Path(engine.state["active_run"])
            child = ato_core.read(child_dir / "state.json")
            child["tasks"]["implement"].update(status="completed", attempts=1, attempt_sequence=3)
            child.update(status="completed", elapsed_seconds=12)
            ato_core.save(child_dir / "state.json", child)
            engine.state.update(status="needs_input", elapsed_seconds=17, active_run=None)
            engine.state["stages"][0]["status"] = "completed"
            engine.state["criteria"]["test"] = {"status": "passed", "evidence": ["private-check-log"]}
            engine.state["criteria"]["user"] = {"status": "needs_input"}
            engine.persist()
            engine.event("workflow_started")
        result = self.store.task_status(self.observer, task_id)
        self.assertEqual(result["stage"]["state"], "needs_input")
        self.assertEqual(result["stage"]["attempts"], 3)
        self.assertEqual(result["stage"]["elapsed_seconds"], 17)
        self.assertEqual(result["checks"], [dict(criterion_id="test", state="passed", receipt_id=None)])
        self.assertEqual(result["acceptance"]["disposition"], "pending")
        self.assertIsNone(result["acceptance"]["receipt_id"])
        self.assertEqual(result["status"], "delivery_unknown")
        self.assertNotIn("private-check-log", json.dumps(result))
        snapshot = self.store.task_snapshot(task_id)
        self.assertIsInstance(snapshot["generation"][0], float)
        self.assertEqual(snapshot["revision"], task["revision"])
        self.assertEqual((self.store.tasks / task_id / "task.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
