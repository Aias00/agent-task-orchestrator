"""Explicit host policy; credentials exist only in the serving process."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import os
from pathlib import Path
import re
from types import MappingProxyType
from urllib.parse import urlsplit

from ato.spec import TaskError, normalize_repository, read_regular_file, require, strict_json

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_TOKEN_ENV = re.compile(r"ATO_SERVER_TOKEN_[A-Z0-9_]+\Z")


@dataclass(frozen=True)
class Principal:
    name: str
    repositories: frozenset[str]
    scopes: frozenset[str]


@dataclass(frozen=True)
class ServerConfig:
    repositories: dict
    principals: dict
    token_hashes: dict = field(repr=False)
    token_envs: frozenset[str] = field(repr=False)
    allowed_hosts: frozenset[str] = frozenset({"127.0.0.1:8765", "localhost:8765"})
    allowed_origins: frozenset[str] = frozenset()
    max_body_bytes: int = 1024 * 1024
    stdio_principal: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> ServerConfig:
        try:
            raw = strict_json(read_regular_file(path, limit=65536)[1])
            require(set(raw) <= {"repositories", "principals", "allowed_hosts", "allowed_origins",
                                 "max_body_bytes", "stdio_principal"}, "INVALID_SERVER_CONFIG")
            repos, principals, hashes, envs = {}, {}, {}, set()
            require(isinstance(raw.get("repositories"), dict) and bool(raw["repositories"]), "INVALID_SERVER_CONFIG")
            for alias, root in raw["repositories"].items():
                require(_NAME.fullmatch(alias) is not None and isinstance(root, str)
                        and Path(root).is_absolute(), "INVALID_SERVER_CONFIG")
                repos[alias] = normalize_repository(root)
            require(len({r.root for r in repos.values()}) == len(repos), "INVALID_SERVER_CONFIG")
            require(isinstance(raw.get("principals"), dict) and bool(raw["principals"]), "INVALID_SERVER_CONFIG")
            for name, entry in raw["principals"].items():
                require(_NAME.fullmatch(name) is not None and isinstance(entry, dict)
                        and set(entry) == {"token_env", "repositories", "scopes"}, "INVALID_SERVER_CONFIG")
                env = entry["token_env"]
                require(isinstance(env, str) and _TOKEN_ENV.fullmatch(env) is not None, "INVALID_SERVER_CONFIG")
                secret = os.environ.get(env, "")
                require(32 <= len(secret) <= 4096 and secret.isascii()
                        and all(33 <= ord(c) <= 126 for c in secret), "INVALID_SERVER_CONFIG")
                require(isinstance(entry["repositories"], list) and isinstance(entry["scopes"], list),
                        "INVALID_SERVER_CONFIG")
                aliases, scopes = frozenset(entry["repositories"]), frozenset(entry["scopes"])
                require(bool(aliases) and aliases <= repos.keys() and bool(scopes)
                        and scopes <= {"read", "control", "human"}, "INVALID_SERVER_CONFIG")
                # A controller credential can never answer human questions, even by configuration mistake.
                require(not {"control", "human"} <= scopes, "INVALID_SERVER_CONFIG")
                digest = hashlib.sha256(secret.encode()).digest()
                require(digest not in hashes.values(), "INVALID_SERVER_CONFIG")
                principals[name] = Principal(name, aliases, scopes)
                hashes[name] = digest
                envs.add(env)
            hosts = raw.get("allowed_hosts", ["127.0.0.1:8765", "localhost:8765"])
            origins = raw.get("allowed_origins", [])
            require(isinstance(hosts, list) and bool(hosts) and isinstance(origins, list), "INVALID_SERVER_CONFIG")
            for host in hosts:
                require(isinstance(host, str) and bool(host) and host == host.lower()
                        and re.fullmatch(r"[a-z0-9.\-\[\]:]+", host) is not None, "INVALID_SERVER_CONFIG")
            for origin in origins:
                require(isinstance(origin, str), "INVALID_SERVER_CONFIG")
                parsed = urlsplit(origin)
                require(parsed.scheme in {"http", "https"} and parsed.netloc in hosts
                        and not parsed.path and not parsed.query and not parsed.fragment
                        and not parsed.username and not parsed.password, "INVALID_SERVER_CONFIG")
            limit = raw.get("max_body_bytes", 1024 * 1024)
            require(type(limit) is int and 1024 <= limit <= 4 * 1024 * 1024, "INVALID_SERVER_CONFIG")
            local = raw.get("stdio_principal")
            require(local is None or local in principals, "INVALID_SERVER_CONFIG")
            return cls(MappingProxyType(repos), MappingProxyType(principals), MappingProxyType(hashes),
                       frozenset(envs), frozenset(hosts), frozenset(origins), limit, local)
        except (TaskError, OSError, TypeError, ValueError):
            raise TaskError("INVALID_SERVER_CONFIG") from None

    def authenticate(self, authorization: str | None) -> Principal:
        require(isinstance(authorization, str) and authorization.startswith("Bearer ")
                and len(authorization) <= 4103, "AUTH_REQUIRED")
        digest = hashlib.sha256(authorization[7:].encode()).digest()
        matched = None
        for name, expected in self.token_hashes.items():
            if hmac.compare_digest(digest, expected):
                matched = name
        require(matched is not None, "AUTH_REQUIRED")
        return self.principals[matched]

    def worker_environment(self) -> dict[str, str]:
        """Strip transport credentials, including differently named copies, without mutating os.environ."""
        secrets = {os.environ[name] for name in self.token_envs if name in os.environ}
        return {k: v for k, v in os.environ.items()
                if not k.startswith("ATO_SERVER_") and k not in self.token_envs
                and k not in {"CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE"}
                and not any(secret in v for secret in secrets)}
