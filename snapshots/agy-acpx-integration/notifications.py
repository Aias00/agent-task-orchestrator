"""Hub delivery policy. Changes notification routing, never task authorization/state."""

MODES = ('all', 'user_input_only')


def configure_failure_alerts(source):
    import failure_attention
    return failure_attention.configure(source)


def status():
    import interaction
    import runner as core
    path = interaction.directory()/'notification-policy.json'
    hub = interaction.config() or {}
    if path.exists():
        policy = core.read(path)
        if policy.get('hub_thread') == hub.get('thread_id'):
            if policy.get('mode') not in MODES:
                raise ValueError('invalid notification policy')
            return dict(policy, configured=True)
    return dict(mode='all', configured=False, hub_thread=hub.get('thread_id'))


def configure(mode, source):
    import interaction
    import recovery
    import runner as core
    hub = interaction.config()
    if not hub or not source or not source.strip() or mode not in MODES:
        raise ValueError('notification policy requires a verified hub, mode and user source')
    interaction.actor(hub['thread_id'])
    policy = dict(mode=mode, hub_thread=hub['thread_id'], source=source,
                  configured_at=core.now())
    with recovery.locked(interaction.directory()/'notification-policy.lock'):
        core.save(interaction.directory()/'notification-policy.json', policy)
    return status()


def project_mutes():
    import interaction
    import runner as core
    path = interaction.directory()/'notification-project-mutes.json'
    hub = interaction.config() or {}
    if not path.exists():
        return []
    data = core.read(path)
    if data.get('hub_thread') != hub.get('thread_id'):
        return []
    return [r for r in data.get('rules', []) if r.get('muted') is True]


def configure_project(repo, muted, source):
    from pathlib import Path
    import interaction
    import recovery
    import runner as core
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


def question_already_notified(question, action=None):
    """Under the user's once-only controller preference, no second channel wakeup.

    A presentation is not a read/answer. An ambiguous delivery is preserved and
    never replayed as a new reminder. A definite initial send failure may use
    the controller's single fallback notice.
    """
    import control
    cfg = control.settings() or {}
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
        import failure_attention
        notify = failure_attention.pending(action)
        return dict(mode=policy['mode'], notify=notify, source=action.get('policy_source'),
                    reason='once-only unhandled failure notice' if notify else 'failure already presented, stale, or being handled')
    if policy['mode'] == 'all':
        return dict(mode='all', notify=True, reason='all notifications enabled')
    if channel == 'followup' and action.get('kind') == 'question' and action.get('state') in ('unpresented', 'needs_user'):
        import interaction
        import runner as core
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
