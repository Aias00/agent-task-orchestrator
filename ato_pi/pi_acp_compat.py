"""Pinned, deterministic pi-acp compatibility backend; no business imports.

Only a reviewed source, named backend, exact capabilities and verified output
can be published. Import is inert; registry/source selection is code-owned.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

from ato_pi.pi_toolchain import SOURCE, registry

BACKEND = 'pi-acp-0.0.34-compat-v1'
CAPABILITIES = {
    'worker': frozenset(('turn_nonce', 'native_receipt')),
    'coordinator': frozenset(('turn_nonce', 'native_receipt', 'native_identity',
                              'correlated_updates', 'slash_commands_disabled')),
}
MAX_SOURCE_BYTES = 32*1024*1024


class CompatibilityError(ValueError):
    """Diagnostics identify a backend/transform, never upstream source data."""


def replace_once(source, before, after, *, transform='legacy'):
    if source.count(before) != 1:
        raise CompatibilityError(BACKEND+': transform '+transform+' requires one match')
    return source.replace(before, after, 1)


@dataclass(frozen=True)
class Transformation:
    name: str
    before: str
    after: str

    def apply(self, source):
        return replace_once(source, self.before, self.after, transform=self.name)


BASE_TRANSFORMS = (
    Transformation('isolated_state',
        '  return join(homedir(), ".pi", "pi-acp");',
        ('  if (!process.env.CTASK_PI_ACP_STATE_DIR) throw new Error("isolated state directory required");\n'
         '  return process.env.CTASK_PI_ACP_STATE_DIR;')),
    Transformation('disable_update_probe',
        'const updateNotice = buildUpdateNotice();',
        'const updateNotice = null; // No npm/network version probe in this profile.'),
    Transformation('prompt_nonce',
        ('  async prompt(message, images = []) {\n'
         '    const expandedMessage'),
        ('  async prompt(message, images = [], ctaskTurn) {\n'
         '    const expandedMessage')),
    Transformation('queue_nonce',
        'const queued = { message: expandedMessage, images, resolve: resolve4, reject };',
        'const queued = { message: expandedMessage, images, ctaskTurn, resolve: resolve4, reject };'),
    Transformation('turn_initialization',
        ('    this.cancelRequested = false;\n'
         '    this.inAgentLoop = false;\n'
         '    this.pendingTurn = { resolve: t.resolve, reject: t.reject };'),
        ('    this.cancelRequested = false;\n'
         '    this.inAgentLoop = false;\n'
         '    this.ctaskTurn = t.ctaskTurn;\n'
         '    this.ctaskNativeError = false;\n'
         '    this.ctaskNativeTerminal = null;\n'
         '    this.ctaskNativeResult = null;\n'
         '    this.pendingTurn = { resolve: t.resolve, reject: t.reject };')),
    Transformation('settle_native_receipt',
        ('  async settleTurn() {\n'
         '    await this.publishContextUsage();'),
        ('  async settleTurn() {\n'
         '    if (this.ctaskNativeError) this.ctaskReceipt("error");\n'
         '    else if (this.ctaskNativeTerminal === "stop") this.ctaskReceipt("stop");\n'
         '    await this.publishContextUsage();')),
    Transformation('native_prompt_error',
        '    this.proc.prompt(t.message, t.images).catch((err) => {',
        ('    this.proc.prompt(t.message, t.images).catch((err) => {\n'
         '      this.ctaskNativeError = true;\n'
         '      this.ctaskReceipt("error");')),
    Transformation('native_event_receipt',
        ('  handlePiEvent(ev) {\n'
         '    const type = String(ev.type ?? "");'),
        ('  ctaskReceipt(reason) {\n'
         '    if (this.pendingTurn && typeof this.ctaskTurn === "string") {\n'
         '      this.emit({sessionUpdate: "session_info_update", _meta: {\n'
         '        ctaskNativeReceipt: {turn: this.ctaskTurn, stopReason: reason,\n'
         '          ...(reason === "stop" && this.ctaskNativeResult !== null ? {result: this.ctaskNativeResult} : {})}\n'
         '      }});\n'
         '    }\n'
         '  }\n'
         '  handlePiEvent(ev) {\n'
         '    if (ev.type === "message_end" && ev.message?.role === "assistant" && this.pendingTurn) {\n'
         '      const reason = ev.message.stopReason;\n'
         '      if (["error", "aborted", "cancelled"].includes(reason)) {\n'
         '        this.ctaskNativeError = true;\n'
         '      }\n'
         '      this.ctaskNativeTerminal = reason;\n'
         '      this.ctaskNativeResult = null;\n'
         '      if (reason === "stop" && Array.isArray(ev.message.content)) {\n'
         '        const parts = ev.message.content.filter(x => x?.type === "text" && typeof x.text === "string");\n'
         '        const result = parts.map(x => x.text).join("");\n'
         '        if (result.length <= 1048576) this.ctaskNativeResult = result;\n'
         '      }\n'
         '    }\n'
         '    const type = String(ev.type ?? "");')),
    Transformation('acp_prompt_metadata',
        'const result = await session.prompt(message, images);',
        'const result = await session.prompt(message, images, params._meta?.ctaskTurn);'),
)

COORDINATOR_TRANSFORMS = (
    Transformation('require_native_identity',
        'const sessionId = typeof state?.sessionId === "string" ? state.sessionId : crypto.randomUUID();',
        ('if (typeof state?.sessionId !== "string" || !state.sessionId || !state.sessionFile) throw new Error("native identity required");\n'
         '    const sessionId = state.sessionId;')),
    Transformation('publish_native_identity',
        ('    _meta: {\n'
         '        piAcp: {\n'
         '          startupInfo: preludeText || null'),
        ('    _meta: {\n'
         '        atoIdentity: {nativeId: state.sessionId, project: params.cwd},\n'
         '        piAcp: {\n'
         '          startupInfo: preludeText || null')),
    Transformation('correlated_updates',
        ('        sessionId: this.sessionId,\n'
         '        update\n'),
        ('        sessionId: this.sessionId,\n'
         '        update: {...update, _meta: {...(update._meta ?? {}), ctaskTurn}}\n')),
    Transformation('capture_emit_nonce',
        ('  emit(update) {\n'
         '    this.lastEmit'),
        ('  emit(update) {\n'
         '    const ctaskTurn = this.ctaskTurn;\n'
         '    this.lastEmit')),
    Transformation('disable_slash_commands',
        ('function loadSlashCommands(cwd) {\n'
         '  const commands = [];'),
        ('function loadSlashCommands(cwd) {\n'
         '  return [];\n'
         '  const commands = [];')),
)


def backend(role):
    """Validate an exact role contract, not a semver/capability guess."""
    if not isinstance(role, str) or role not in CAPABILITIES:
        raise CompatibilityError('unknown pi-acp role')
    try:
        entry = registry()['adapter']
        if (entry['backend'] != BACKEND or entry['name'] != 'pi-acp'
                or entry['version'] != '0.0.34'
                or set(entry['derived_sha256']) != set(CAPABILITIES)
                or set(entry['capabilities']) != set(CAPABILITIES)):
            raise ValueError
        for name, capabilities in CAPABILITIES.items():
            declared = entry['capabilities'][name]
            if (not isinstance(declared, list) or len(declared) != len(capabilities)
                    or set(declared) != capabilities):
                raise ValueError
        for value in (entry['source_sha256'], *entry['derived_sha256'].values()):
            if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
                raise ValueError
        return entry
    except (KeyError, TypeError, ValueError):
        raise CompatibilityError('unsupported pi-acp backend contract') from None


def derive(source, *, role='worker'):
    """Pure source transformation after input and before output verification."""
    entry = backend(role)
    try:
        raw = source.encode('utf-8')
    except (AttributeError, UnicodeError):
        raise CompatibilityError(BACKEND+': invalid source encoding') from None
    if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != entry['source_sha256']:
        raise CompatibilityError(BACKEND+': source fingerprint differs')
    transforms = BASE_TRANSFORMS + (COORDINATOR_TRANSFORMS if role == 'coordinator' else ())
    for transform in transforms:
        source = transform.apply(source)
    if hashlib.sha256(source.encode('utf-8')).hexdigest() != entry['derived_sha256'][role]:
        raise CompatibilityError(BACKEND+': output fingerprint differs')
    return source


def derive_coordinator(source):
    return derive(source, role='coordinator')


def _read_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise CompatibilityError(BACKEND+': invalid source file')
        value = stream.read(limit+1)
        if len(value) > limit:
            raise CompatibilityError(BACKEND+': source exceeds bound')
        return value


def dependency(name):
    # Resolve only the installed lock's nested/hoisted dependencies. No plan or
    # environment chooses another source/dependency root.
    root = SOURCE.parents[2].resolve()
    for base in (SOURCE.parents[1]/'node_modules', SOURCE.parents[2]):
        path = base/name
        if path.is_dir():
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise CompatibilityError(BACKEND+': dependency escapes installation')
            return resolved
    raise CompatibilityError(BACKEND+': missing pinned dependency')


def _write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _publish(staging, output):
    """Atomic no-replace directory publication, even if another host wins."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = libc.renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        args = (os.fsencode(staging), os.fsencode(output), 0x4)  # RENAME_EXCL
    elif sys.platform.startswith('linux') and hasattr(libc, 'renameat2'):
        rename = libc.renameat2
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        args = (-100, os.fsencode(staging), -100, os.fsencode(output), 1)  # AT_FDCWD, RENAME_NOREPLACE
    else:
        raise CompatibilityError(BACKEND+': atomic directory publication unavailable')
    rename.restype = ctypes.c_int
    if rename(*args) != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise CompatibilityError(BACKEND+': output already exists')
        raise CompatibilityError(BACKEND+': atomic directory publication failed')


def derive_directory(output, *, role='worker'):
    """Validate entirely in memory, then publish one new private directory.

    This never changes installed packages, locks, or an existing runtime. The
    worker and coordinator share identical dependency wiring and publication.
    """
    entry = backend(role)
    raw = _read_regular(SOURCE, MAX_SOURCE_BYTES)
    metadata = json.loads(_read_regular(SOURCE.parents[1]/'package.json', 1024*1024))
    if metadata.get('name') != entry['name'] or metadata.get('version') != entry['version']:
        raise CompatibilityError(BACKEND+': installed package identity differs')
    try:
        derived = derive(raw.decode('utf-8'), role=role).encode('utf-8')
    except UnicodeError:
        raise CompatibilityError(BACKEND+': invalid source encoding') from None
    dependencies = {name: dependency(name) for name in ('@agentclientprotocol', 'cross-spawn')}
    output = Path(output).absolute()
    if os.path.lexists(output):
        raise CompatibilityError(BACKEND+': output already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.ato-adapter-', dir=output.parent))
    try:
        _write_new(staging/'pi-acp.mjs', derived)
        _write_new(staging/'package.json', (json.dumps(dict(name=entry['name'],
            version=entry['version'], type='module'))+'\n').encode())
        modules = staging/'node_modules'
        modules.mkdir(mode=0o700)
        for name, path in dependencies.items():
            (modules/name).symlink_to(path, target_is_directory=True)
        _publish(staging, output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output/'pi-acp.mjs'
