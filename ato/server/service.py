"""Transport-neutral authorized facade over the existing ATO business operations."""
from __future__ import annotations

from pathlib import Path
import re

from ato import executor, inbox, product
from ato.spec import (TaskError, canonical_json, new_id, normalize_inline_spec, normalize_repository,
                      require, require_id, result_envelope)
from ato.store import Store
from ato.server.config import Principal, ServerConfig
from ato.server.idempotency import Journal


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
TASK = {"type": "string", "pattern": "^task-[0-9a-f]{32}$"}
KEY = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"}
SPEC = schema({"repo": TEXT, "spec": {"type": "object"}}, ("repo", "spec"))
OPERATIONS = {
    "repositories": ("read", schema(), "List repository aliases available to this principal."),
    "validate": ("control", SPEC, "Validate an inline spec naming an allowed repository alias."),
    "submit": ("control", schema({**SPEC["properties"], "request_id": KEY}, ("repo", "spec", "request_id")),
               "Submit asynchronously. Poll status; submission is not completion. Trusted controllers only."),
    "list": ("read", schema({"repo": TEXT}), "List tasks within the caller's allowed repositories."),
    "status": ("read", schema({"task_id": TASK, "check_history": {"type": "boolean"}}, ("task_id",)),
               "Read current task, run, check, reviewer and STOP facts."),
    "diff": ("read", schema({"task_id": TASK, "stat": {"type": "boolean"}}, ("task_id",)), "Read bounded task changes."),
    "trace": ("read", schema({"task_id": TASK}, ("task_id",)), "Read the bounded task event timeline."),
    "stop": ("control", schema({"task_id": TASK, "request_id": KEY}, ("task_id", "request_id")), "Request durable STOP."),
    "resume": ("control", schema({"task_id": TASK, "request_id": KEY,
                                 "timeout_seconds": {"type": "integer", "minimum": 5, "maximum": 7200}},
                                ("task_id", "request_id")), "Resume an eligible task asynchronously."),
    "inbox": ("read", schema({"task_id": TASK}, ("task_id",)), "Read open human questions for one allowed task."),
    "answer": ("human", schema({"task_id": TASK, "question_id": {"type": "string", "pattern": "^request-[0-9a-f]{32}$"},
                                "answer": {"type": "string", "minLength": 1, "maxLength": 8192}, "request_id": KEY},
                               ("task_id", "question_id", "answer", "request_id")),
               "Record an explicit human answer. Requires separate human authority; does not resume or accept."),
}
MUTATIONS = frozenset({"submit", "resume", "stop", "answer"})


def validate_arguments(operation: str, arguments: dict) -> dict:
    require(operation in OPERATIONS, "NOT_FOUND")
    shape = OPERATIONS[operation][1]
    require(isinstance(arguments, dict) and set(shape["required"]) <= arguments.keys()
            <= shape["properties"].keys(), "USAGE")
    for key, value in arguments.items():
        rule = shape["properties"][key]
        types = {"string": str, "object": dict, "boolean": bool, "integer": int}
        require(type(value) is types[rule["type"]], "USAGE")
        if isinstance(value, str):
            require("\x00" not in value and len(value) >= rule.get("minLength", 0)
                    and len(value) <= rule.get("maxLength", 8192), "USAGE")
            require(not rule.get("pattern") or re.fullmatch(rule["pattern"], value) is not None, "USAGE")
        if type(value) is int:
            require(rule.get("minimum", value) <= value <= rule.get("maximum", value), "USAGE")
    args = dict(arguments)
    if operation == "status":
        args.setdefault("check_history", False)
    if operation == "diff":
        args.setdefault("stat", False)
    return args


class Service:
    def __init__(self, config: ServerConfig, db_file: Path):
        self.config, self.db_file = config, Path(db_file).resolve()
        self.journal = Journal(self.db_file.parent / "transport-requests.db")

    def _repo(self, principal: Principal, alias: str):
        require(alias in principal.repositories and alias in self.config.repositories, "NOT_FOUND")
        bound = self.config.repositories[alias]
        require(normalize_repository(bound.root) == bound, "BINDING_CHANGED")
        return bound

    def _task(self, store: Store, principal: Principal, task_id: str):
        require_id(task_id, "task")
        task = store.get_task(task_id)
        alias = self._alias(principal, task)
        require(alias is not None, "NOT_FOUND")
        self._repo(principal, alias)
        return task

    def _alias(self, principal: Principal, task: dict):
        return next((alias for alias in sorted(principal.repositories)
                     if task["repo_root"] == self.config.repositories[alias].root
                     and task["repo_key"] == self.config.repositories[alias].key), None)

    def call(self, principal: Principal, operation: str, arguments: dict, *, origin: str) -> dict:
        """Return the same envelope for REST and MCP; never return exception/request text."""
        try:
            require(origin in {"api", "mcp"}, "FORBIDDEN")
            require(self.config.principals.get(principal.name) == principal, "FORBIDDEN")
            args = validate_arguments(operation, arguments)
            require(OPERATIONS[operation][0] in principal.scopes, "FORBIDDEN")
            require(len(canonical_json(args)) <= self.config.max_body_bytes, "BODY_TOO_LARGE")
            store = Store(self.db_file)  # Store opens/closes its own connection for every transaction.
            task = self._task(store, principal, args["task_id"]) if "task_id" in args else None
            repo = self._repo(principal, args["repo"]) if "repo" in args else None
            if operation == "answer":
                question = store.get_question(args["question_id"])
                require(question["task_id"] == task["id"], "NOT_FOUND")
                require(bool(args["answer"].strip()) and len(args["answer"].encode()) <= 8192, "USAGE")
            if operation not in MUTATIONS:
                return result_envelope(data=self._read(store, principal, operation, args, task, repo))
            task_id = task["id"] if task else new_id("task")
            # Digest caller arguments, not resolved HEAD or origin, so cross-transport retries match.
            canonical_args = {k: v for k, v in args.items() if k != "request_id"}
            if repo is not None:
                # A host changing an alias must not reveal a cached result for the old repository.
                canonical_args["repository_binding"] = {"root": repo.root, "key": repo.key}
            norm = None

            def prepare():
                nonlocal norm
                if operation == "submit":
                    norm = normalize_inline_spec(repo.root, args["repo"], args["spec"])

            def effect():
                if operation == "submit":
                    result = executor.submit_normalized(store, norm, origin=(origin, None), task_id=task_id,
                                                        child_env=self.config.worker_environment())
                else:
                    store.append_event(task_id, "transport_operation", {"origin": origin, "principal": principal.name,
                                                                         "operation": operation})
                    if operation == "resume":
                        result = executor.resume(store, task_id, timeout_seconds=args.get("timeout_seconds"),
                                                 child_env=self.config.worker_environment())
                    elif operation == "stop":
                        result = executor.request_stop(store, task_id)
                    else:
                        result = inbox.record_answer(store, args["question_id"], args["answer"])
                if "repo" in result:
                    result = {**result, "repo": args.get("repo") or self._alias(principal, task)}
                return result_envelope(data=result)

            return self.journal.perform(principal.name, args["request_id"], operation, canonical_args, task_id,
                                        effect, prepare=prepare)
        except TaskError as exc:
            return result_envelope(error=TaskError(exc.code, task_id=exc.task_id))
        except Exception:
            return result_envelope(error=TaskError("INTERNAL_ERROR"))

    def _read(self, store, principal, operation, args, task, repo):
        if operation == "repositories":
            return {"repositories": [{"alias": alias} for alias in sorted(principal.repositories)]}
        if operation == "validate":
            preview = normalize_inline_spec(repo.root, args["repo"], args["spec"]).preview()
            # Replace the canonical filesystem root with the public alias.
            return {"repo": args["repo"], "valid": True, "spec_sha256": preview["spec_sha256"],
                    "base_commit": preview["base_commit"]}
        if operation == "list":
            rows = []
            for item in store.list_tasks(repo_key=repo.key if repo else None):
                alias = self._alias(principal, item)
                if alias is not None:
                    rows.append({"task_id": item["id"], "repo": alias,
                                 "status": "blocked" if item["legacy_advisory_completion"] else item["status"],
                                 "reason": item["reason"], "updated_at": item["updated_at"]})
            return {"tasks": rows}
        task_id = task["id"]
        if operation == "status":
            return {**executor.task_status_view(store, task_id, include_check_history=args["check_history"]),
                    "repo": self._alias(principal, task)}
        if operation == "diff":
            return {"task_id": task_id, "base": task["base_commit"], "branch": task["branch"],
                    "worktree_state": task["worktree_state"],
                    **product.collect_diff(task, include_patch=not args["stat"])}
        if operation == "trace":
            return {"task_id": task_id, "events": store.list_events(task_id)}
        if operation == "inbox":
            rows = store.list_questions(task_id, state="open")
            return {"requests": [{"request_id": row["id"], "task_id": task_id,
                                  **{key: row[key] for key in ("kind", "question", "reason", "continuation", "created_at")}}
                                 for row in rows], "count": len(rows)}
        raise TaskError("NOT_FOUND")
