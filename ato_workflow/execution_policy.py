"""Explicit side-effect ownership; authorization is never inferred from prose.

This controls worker instructions/admission, not the OS sandbox or a grant from
the user. The plan creator must bind an existing authorization to bounded work.
Legacy plans retain their existing worker restrictions.
"""

ACTIONS = ('git_commit', 'git_push', 'deploy', 'send_messages')
LABELS = dict(git_commit='git commit', git_push='git push', deploy='deploy',
              send_messages='send messages')
OWNERS = ('worker', 'origin', 'deny')


def validate(plan, capabilities):
    policy = plan.get('execution_policy')
    if 'execution_policy' not in plan:
        return
    if not isinstance(policy, dict) or set(policy) - {
            'source', 'instructions', 'actions', 'required_actions'}:
        raise ValueError('invalid execution_policy fields')
    for key in ('source', 'instructions'):
        if not isinstance(policy.get(key), str) or not policy[key].strip():
            raise ValueError('execution_policy requires bounded instructions and authorization source')
    actions = policy.get('actions')
    if (not isinstance(actions, dict) or not actions or set(actions) - set(ACTIONS)
            or any(owner not in OWNERS for owner in actions.values())):
        raise ValueError('execution_policy actions require known action/owner pairs')
    required = policy.get('required_actions', [])
    if (not isinstance(required, list) or any(action not in ACTIONS for action in required)
            or len(set(required)) != len(required)):
        raise ValueError('execution_policy required_actions must be unique known actions')
    for action in required:
        if actions.get(action, 'deny') == 'deny':
            raise ValueError('execution_policy has no execution path for required action: ' + action)
    if 'origin' in actions.values() and not plan.get('host_handoff'):
        raise ValueError('origin-owned execution_policy actions require host_handoff')
    if 'worker' in actions.values():
        if 'explicit_permissions' not in capabilities or plan.get('sandbox') != 'danger-full-access':
            raise ValueError('worker side effects require adapter explicit_permissions and danger-full-access')
        grant = plan.get('permissions', {})
        if not isinstance(grant.get('source'), str) or not grant['source'].strip():
            raise ValueError('worker side effects require an explicit permissions source')


def prompt(plan):
    policy = plan.get('execution_policy')
    if policy is None:
        return ('Legacy worker action policy: do not git commit/push, deploy, or send messages.\n'
                'Task authorization does not by itself assign those actions to this worker.\n'
                'If assigned work conflicts with this policy, report blocked with the exact conflict; '
                'do not repeat unchanged checks or ask for authorization already recorded in the contract.')
    lines = ['Explicit worker action policy (bounded by the original task authorization):',
             'Authorization source: ' + policy['source'],
             'Action scope: ' + policy['instructions']]
    for action in ACTIONS:
        owner = policy['actions'].get(action, 'deny')
        if owner == 'worker':
            lines.append('Worker may ' + LABELS[action] + ' only within the recorded action scope.')
        elif owner == 'origin':
            lines.append('Only the origin may ' + LABELS[action] + '; prepare a bounded host handoff.')
        else:
            lines.append('Worker must not ' + LABELS[action] + '.')
    lines.append('A policy/scope conflict is blocked, never a reason to widen permissions or retry unchanged work.')
    return '\n'.join(lines)
