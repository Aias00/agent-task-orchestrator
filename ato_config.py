"""Configuration boundary for ATO state and legacy Codex-native state.

``ATO_HOME`` locates ATO-owned ledgers and opaque session references. When it
is absent, ``CODEX_HOME`` remains the compatibility root so existing installs
continue to read their current state. ``CODEX_HOME`` always locates native
Codex indexes and rollouts; it is never authorization for an operation.
Setting a distinct ``ATO_HOME`` does not migrate existing ledgers. Environment
values are paths only, not actor credentials or session proofs.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import shlex

_unified_kernel = ContextVar('unified_kernel', default=None)


def unified_home():
    """Locate the new namespace without resolving away redirected components."""
    configured = os.environ.get('ATO_HOME')
    return Path(configured).expanduser().absolute() if configured else Path.home()/'.local/share/ato'


@contextmanager
def use_unified_kernel(kernel_root, native_home):
    """Bind the sealed runtime to host-verified roots for its kernel calls."""
    token = _unified_kernel.set((Path(kernel_root), Path(native_home)))
    try:
        yield
    finally:
        _unified_kernel.reset(token)


def _configured_path(name, fallback):
    raw = os.environ.get(name)
    path = Path(raw).expanduser() if raw else Path(fallback)
    return path.resolve()


def codex_home():
    """Return the vendor-owned Codex configuration root, independent of ATO_HOME."""
    if selected := _unified_kernel.get():
        return selected[1]
    return _configured_path('CODEX_HOME', Path.home()/'.codex')


def ato_home():
    """Return the ATO-owned state root, retaining CODEX_HOME fallback compatibility."""
    if selected := _unified_kernel.get():
        return selected[0]
    explicit = os.environ.get('ATO_HOME')
    if explicit:
        return _configured_path('ATO_HOME', Path.home()/'.codex')
    return codex_home()


def roots():
    """Describe resolved locators for diagnostics without reading private config."""
    return dict(ato_home=str(ato_home()), codex_home=str(codex_home()),
                ato_home_explicit=bool(os.environ.get('ATO_HOME')))


def interaction_directory():
    """An explicit legacy account locator selects an isolated ATO context.

    Multiple legacy accounts may share an ATO root, but must never share hub
    bindings, policies, endpoint UUIDs or actor credentials. No native data is
    read to choose the context. Without ATO_HOME the historical layout is exact;
    without an explicit CODEX_HOME neutral-only clients use the root context.
    Changing either locator does not migrate state or confer authority.
    """
    root = ato_home()/'ctask-interaction'
    if _unified_kernel.get() is not None:
        return root
    if os.environ.get('ATO_HOME') and os.environ.get('CODEX_HOME'):
        import hashlib
        namespace = hashlib.sha256(str(codex_home()).encode()).hexdigest()
        return root/'contexts'/namespace
    return root


def ensure_interaction_directory():
    """Create only the existing ATO-owned namespace; no registration or services."""
    path = interaction_directory()
    path.mkdir(parents=True, exist_ok=True)
    return path


def reader_command():
    """Resolve the trusted installed handoff helper without project/PATH discovery."""
    configured = os.environ.get('CTASK_HANDOFF_READER')
    path = Path(configured).expanduser() if configured else None
    if not (path and path.is_absolute() and path.is_file() and os.access(path, os.X_OK)):
        path = Path.home()/'.local/bin/ato-read-handoff'
    # Keep the standard helper name visible while quoting arbitrary directories.
    command = shlex.quote(str(path.parent))+'/'+shlex.quote(path.name)
    if path.name != 'ato-read-handoff':
        command = '/bin/sh -c '+shlex.quote('exec '+command+' "$@"')+' ato-read-handoff'
    return command
