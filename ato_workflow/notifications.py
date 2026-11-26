"""Hub delivery policy. Changes notification routing, never task authorization/state."""

from ato_common import ato_core as core
from ato_workflow import notification_state

MODES = ('all', 'user_input_only')


def configure_failure_alerts(source):
    from ato_workflow import failure_attention
    return failure_attention.configure(source)


def status():
    return notification_state.status()


def configure(mode, source, *, completion=None):
    from ato_workflow import interaction
    import recovery
    hub = interaction.config()
    if not hub or not source or not source.strip() or mode not in MODES:
        raise ValueError('notification policy requires a verified hub, mode and user source')
    interaction.actor(hub['thread_id'])
    if completion is not None and completion not in ('once', 'silent'):
        raise ValueError('completion must be once or silent')
    with recovery.locked(interaction.directory()/'notification-policy.lock'):
        prior = status()
        policy = dict(mode=mode, hub_thread=hub['thread_id'], source=source, configured_at=core.now())
        if prior.get('hub_thread') == hub['thread_id']:
            for key in ('completion', 'completion_since', 'completion_source'):
                if key in prior:
                    policy[key] = prior[key]
        if completion is not None:
            if prior.get('completion') != completion or 'completion_since' not in prior:
                policy['completion_since'] = core.now()
            policy.update(completion=completion, completion_source=source)
        core.save(interaction.directory()/'notification-policy.json', policy)
    return status()


def project_mutes():
    return notification_state.project_mutes()


def configure_project(repo, muted, source):
    from pathlib import Path
    from ato_workflow import interaction
    import recovery
    hub = interaction.config()
    if not hub or not source or not source.strip() or type(muted) is not bool:
        raise ValueError('project notification preference requires verified hub and user source')
    interaction.actor(hub['thread_id'])
    project = Path(repo).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ValueError('project must be an existing directory')
    path = interaction.directory()/'notification-project-mutes.json'
    with recovery.locked(interaction.directory()/'notification-project-mutes.lock'):
        data = core.read(path) if path.exists() else {}
        if data.get('hub_thread') != hub['thread_id']:
            data = dict(hub_thread=hub['thread_id'], rules=[])
        rules = [r for r in data.get('rules', []) if r.get('repo') != str(project)]
        rules.append(dict(repo=str(project), muted=muted, source=source, updated_at=core.now()))
        core.save(path, dict(hub_thread=hub['thread_id'], rules=rules))
    return dict(repo=str(project), muted=muted, source=source, pending_tasks_unchanged=True)


def project_suppression(action):
    return notification_state.project_suppression(action)


def question_already_notified(question, action=None):
    """Under the user's once-only controller preference, no second channel wakeup.

    A presentation is not a read/answer. An ambiguous delivery is preserved and
    never replayed as a new reminder. A definite initial send failure may use
    the controller's single fallback notice.
    """
    from ato_workflow import control_store
    cfg = control_store.settings() or {}
    policy = status()
    if cfg.get('hub_thread') != policy.get('hub_thread') or cfg.get('max_reminders') != 0:
        return False
    action = action or {}
    delivery = question.get('question_delivery', {}).get('state')
    return (delivery in ('queued', 'local', 'dispatching', 'delivery_unknown')
            or bool(action.get('presented_at')) or bool(action.get('notice')))


def disposition(action, channel='followup'):
    """Only a current, canonical pending ask requires user intervention.

    Never infer permission or urgency from free text, a failed check, a host
    handoff, or an automatically generated supervision alert.
    """
    policy = status()
    mute = project_suppression(action)
    if mute:
        return dict(mode=policy['mode'], notify=False, source=mute['source'],
                    project=mute['repo'], reason='project notifications silenced by user; recorded only')
    notify = False
    if channel == 'followup' and action.get('kind') == 'failure_attention':
        from ato_workflow import failure_attention
        notify = failure_attention.pending(action)
        return dict(mode=policy['mode'], notify=notify, source=action.get('policy_source'),
                    reason='once-only unhandled failure notice' if notify else 'failure already presented, stale, or being handled')
    if channel == 'followup' and action.get('kind') == 'workflow_result' and policy.get('completion') == 'once':
        from ato_workflow import completion_notifications
        notify = completion_notifications.eligible(action)
        return dict(mode=policy['mode'], notify=notify, source=policy.get('completion_source'),
                    reason='verified final workflow completion; once only' if notify else 'completion already claimed, historical, muted, or not fully verified')
    if policy['mode'] == 'all':
        return dict(mode='all', notify=True, reason='all notifications enabled')
    if channel == 'followup' and action.get('kind') == 'question' and action.get('state') in ('unpresented', 'needs_user'):
        from ato_workflow import interaction
        try:
            q = core.read(interaction.question_path(action['id']))
            interaction.validate_question(q)
            notify = (q['state'] == 'pending' and not q.get('kind')
                      and q['hub_thread'] == policy['hub_thread'])
            if notify and question_already_notified(q, action):
                return dict(mode=policy['mode'], notify=False, source=policy['source'],
                            reason='user question already notified or presented; no repeated reminders')
        except (ValueError, OSError, KeyError):
            pass
    return dict(mode=policy['mode'], notify=notify, source=policy['source'],
                reason='current user question' if notify else 'no user intervention; recorded only')
