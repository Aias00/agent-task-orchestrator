"""Hub delivery policy. Changes notification routing, never task authorization/state."""

MODES = ('all', 'user_input_only')


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


def disposition(action, channel='followup'):
    """Only a current, canonical pending ask requires user intervention.

    Never infer permission or urgency from free text, a failed check, a host
    handoff, or an automatically generated supervision alert.
    """
    policy = status()
    if policy['mode'] == 'all':
        return dict(mode='all', notify=True, reason='all notifications enabled')
    notify = False
    if channel == 'followup' and action.get('kind') == 'question' and action.get('state') in ('unpresented', 'needs_user'):
        import interaction
        import runner as core
        try:
            q = core.read(interaction.question_path(action['id']))
            interaction.validate_question(q)
            notify = (q['state'] == 'pending' and not q.get('kind')
                      and q['hub_thread'] == policy['hub_thread'])
        except (ValueError, OSError, KeyError):
            pass
    return dict(mode=policy['mode'], notify=notify, source=policy['source'],
                reason='current user question' if notify else 'no user intervention; recorded only')
