"""Configuration boundary for ATO state and legacy Codex-native state.

``ATO_HOME`` locates ATO-owned ledgers and opaque session references. When it
is absent, ``CODEX_HOME`` remains the compatibility root so existing installs
continue to read their current state. ``CODEX_HOME`` always locates native
Codex indexes and rollouts; it is never authorization for an operation.
Setting a distinct ``ATO_HOME`` does not migrate existing ledgers. Environment
values are paths only, not actor credentials or session proofs.
"""
import os
from pathlib import Path


def _configured_path(name, fallback):
    raw = os.environ.get(name)
    path = Path(raw).expanduser() if raw else Path(fallback)
    return path.resolve()


def codex_home():
    """Return the vendor-owned Codex configuration root, independent of ATO_HOME."""
    return _configured_path('CODEX_HOME', Path.home()/'.codex')


def ato_home():
    """Return the ATO-owned state root, retaining CODEX_HOME fallback compatibility."""
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
