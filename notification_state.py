"""Read-only projections of existing notification policy; no actor grants or delivery."""
import ato_config
import ato_core as core

MODES = ('all', 'user_input_only')


def hub_config():
    path = ato_config.interaction_directory()/'hub.json'
    return core.read(path) if path.exists() else {}


def status():
    path = ato_config.ensure_interaction_directory()/'notification-policy.json'
    hub = hub_config()
    if path.exists():
        policy = core.read(path)
        if policy.get('hub_thread') == hub.get('thread_id'):
            if policy.get('mode') not in MODES:
                raise ValueError('invalid notification policy')
            return dict(policy, configured=True)
    return dict(mode='all', configured=False, hub_thread=hub.get('thread_id'))


def project_mutes():
    path = ato_config.ensure_interaction_directory()/'notification-project-mutes.json'
    hub = hub_config()
    if not path.exists():
        return []
    data = core.read(path)
    if data.get('hub_thread') != hub.get('thread_id'):
        return []
    return [r for r in data.get('rules', []) if r.get('muted') is True]


def project_suppression(action):
    from pathlib import Path
    workflow = action.get('workflow')
    if not workflow:
        return None
    actual = Path(workflow).resolve()
    for rule in project_mutes():
        tasks = Path(rule['repo']).resolve()/'.codex/tasks'
        if actual.is_relative_to(tasks):
            return rule
    return None
