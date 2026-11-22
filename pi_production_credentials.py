"""Read-only Pi authentication at the adapter edge. Import performs no IO."""
import json
import os
from pathlib import Path
import re
import stat


def reject():
    raise ValueError('production credential rejected') from None


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            reject()
        result[key] = value
    return result


def private_json(path, *, forbidden=(), limit=65536):
    """Reject aliases, unsafe ancestors, special files and concurrent replacement."""
    try:
        path = Path(path)
        if not path.is_absolute() or path != path.resolve(strict=True):
            reject()
        if any(path.is_relative_to(Path(p).resolve()) for p in forbidden):
            reject()
        for parent in path.parents:
            info = parent.lstat()
            sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not sticky):
                reject()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > limit):
                reject()
            raw = stream.read(limit + 1)
            now = path.lstat()
            if len(raw) > limit or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
                reject()
            return json.loads(raw, object_pairs_hook=unique_object,
                              parse_constant=lambda _: reject())
    except (OSError, UnicodeError, ValueError, TypeError):
        reject()


class SecretLease:
    """Nonserializable, redacted, short-lived secret; never a Launch field."""
    __slots__ = ('__value',)

    def __init__(self, value):
        if (type(value) is not str or not 1 <= len(value) <= 8192
                or value != value.strip() or any(ord(c) < 33 or ord(c) > 126 for c in value)
                or value.startswith(('!', '$'))):
            reject()
        self.__value = value

    def __repr__(self):
        return '<SecretLease redacted>'

    def __reduce__(self):
        raise TypeError('credential serialization forbidden')

    def environment(self):
        if self.__value is None:
            reject()
        return {'ATO_PI_PROVIDER_KEY': self.__value}

    def close(self):
        self.__value = None


def acquire(source, *, environment=None, forbidden=()):
    """Exactly one explicit source. No auth search, command or OAuth fallback."""
    try:
        if type(source) is not dict:
            reject()
        if source.get('kind') == 'environment':
            if set(source) != {'kind', 'name'} or not re.fullmatch('[A-Z][A-Z0-9_]{1,63}', source['name']):
                reject()
            env = os.environ if environment is None else environment
            return SecretLease(env.get(source['name']))
        if source.get('kind') != 'native_auth' or set(source) != {'kind', 'path', 'provider'}:
            reject()
        if not re.fullmatch('[a-z][a-z0-9.-]{0,63}', source['provider']):
            reject()
        data = private_json(source['path'], forbidden=forbidden)
        selected = data[source['provider']]
        if type(selected) is not dict or set(selected) != {'type', 'key'} or selected['type'] != 'api_key':
            reject()
        return SecretLease(selected['key'])
    except (KeyError, TypeError, ValueError):
        reject()
