"""Bounded body-free diagnostics. Evidence never relaxes profile equality.

Only fixed field labels, hashes and enums cross the evidence boundary. Unknown
keys and config paths are hashed too; exception messages/values are not saved.
There is no native invocation, home preparation or permission change here.
"""
import hashlib
import json
import copy

MAX_FIELDS = 128
MAX_CHANGED = 32
LABELS = frozenset(('adapter', 'binary', 'binary_sha256', 'launcher', 'native_home',
    'configs', 'environment_hash', 'model', 'sandbox', 'approval', 'permissions',
    'source_manifest_sha256', 'cwd', 'approval_policy', 'sandbox_policy',
    'model_provider', 'permission_profile'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def snapshot(profile):
    fields, config_states = {}, {}
    for key, value in profile.items():
        label = key if key in LABELS else 'field:'+digest(key)
        fields[label] = digest(value)
        # Config paths are useful for correlating a change, never for display.
        # Keep the aggregate even when the bounded child list is truncated.
        if key == 'configs' and isinstance(value, dict):
            for path, content_hash in value.items():
                child = 'configs:'+digest(path)
                fields[child] = digest(content_hash)
                config_states[child] = 'absent' if content_hash is None else 'present'
    names = sorted(fields)
    visible = names[:MAX_FIELDS]
    return dict(profile_sha256=digest(profile), fields={k: fields[k] for k in visible},
                config_states={k: config_states[k] for k in visible if k in config_states},
                field_count=len(names), truncated=len(names) > MAX_FIELDS)


def difference(before, after):
    names = sorted(k for k in before['fields'].keys() | after['fields'].keys()
                   if before['fields'].get(k) != after['fields'].get(k))
    return dict(changed_fields=names[:MAX_CHANGED], changed_count=len(names),
        truncated=len(names) > MAX_CHANGED or before['truncated'] or after['truncated'],
        equal=before['profile_sha256'] == after['profile_sha256'])


class ProfileDriftError(ValueError):
    def __init__(self, reason, expected, observed):
        self.profile_evidence = dict(expected=snapshot(expected), observed=snapshot(observed))
        self.profile_evidence.update(difference(self.profile_evidence['expected'], self.profile_evidence['observed']))
        super().__init__(reason+'; changed fields: '+','.join(self.profile_evidence['changed_fields']))


class Turn:
    """Publish pre-launch evidence and a post observation on every exit.

    A killed host leaves status=prepared, never a fabricated post observation.
    Unreadable post state is unknown and rejects success. Full Python equality,
    including fields omitted from bounded display, remains the admission rule.
    """
    def __init__(self, expected, read, publish, sequence):
        self.expected, self.read, self.publish = copy.deepcopy(expected), read, publish
        self.record = dict(version=1, sequence=sequence, status='prepared',
                           expected=snapshot(expected), post=None)

    def __enter__(self):
        try:
            before = self.read()
        except (OSError, ValueError, KeyError, TypeError):
            self.record.update(status='pre_unobservable', pre=None)
            self.publish(self.record)
            raise ValueError('native profile pre-observation unavailable; no launch') from None
        self.record['pre'] = snapshot(before)
        self.record['admission'] = difference(self.record['expected'], self.record['pre'])
        self.publish(self.record)
        if before != self.expected:
            self.record['status'] = 'pre_drift'
            self.publish(self.record)
            raise ProfileDriftError('native profile/binary/config drift; no further turn', self.expected, before)
        return self

    def __exit__(self, kind, error, traceback):
        self.record['execution_raised'] = kind is not None
        if isinstance(error, ProfileDriftError):
            self.record['effective_context_drift'] = error.profile_evidence
        try:
            after = self.read()
        except (OSError, ValueError, KeyError, TypeError):
            self.record.update(status='post_unobservable', post=None)
            self.publish(self.record)
            if kind is None:
                raise ValueError('native profile post-observation unavailable; no replay') from None
            return False
        self.record['post'] = snapshot(after)
        self.record['comparison'] = difference(self.record['pre'], self.record['post'])
        self.record['status'] = 'unchanged' if after == self.expected else 'post_drift'
        self.publish(self.record)
        if after != self.expected and kind is None:
            raise ProfileDriftError('native profile changed during turn; no replay', self.expected, after)
        return False
