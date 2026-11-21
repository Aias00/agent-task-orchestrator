#!/usr/bin/env python3.12
"""Local launcher gate for unsolicited queue messages into the configured hub.

Native Codex is unchanged. All other commands/targets preserve argv and exec.
Only canonical current ctask user questions enter a user-input-only hub.
"""
import ato_config
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys

NATIVE = os.environ.get('CTASK_NATIVE_CODEX', str(Path.home()/'.codex/packages/standalone/current/bin/codex'))
RUNTIME = str(Path(__file__).resolve().parent)


def queue_options(argv):
    value_options = {'-c', '--config', '-m', '--model', '-C', '--cd', '-p', '--profile',
                     '-s', '--sandbox', '--enable', '--disable', '--remote',
                     '--remote-auth-token-env', '--add-dir', '--local-provider', '-i', '--image'}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            return None
        if arg in value_options:
            i += 2
        elif arg.startswith('-'):
            i += 1
        else:
            if arg != 'queue':
                return None
            i += 1
            break
    else:
        return None
    options = {}
    while i < len(argv):
        arg = argv[i]
        if arg in ('--thread', '--message'):
            if i+1 >= len(argv):
                return None
            options[arg] = argv[i+1]; i += 2
        elif arg.startswith('--thread=') or arg.startswith('--message='):
            key, value = arg.split('=', 1); options[key] = value; i += 1
        elif arg == '--':
            break
        elif arg in value_options:
            i += 2
        else:
            i += 1
    if '--thread' not in options or '--message' not in options:
        return None
    return options


def pending_user_question(text, target):
    match = re.match(r'^\[ctask-(?:question id=(question-[0-9a-f]{20})|'
                     r'followup id=(question-[0-9a-f]{20}) reminder=\d+)\](?:\n|$)', text)
    if not match:
        return False
    sys.path.insert(0, RUNTIME)
    import interaction
    import runner as core
    identifier = match[1] or match[2]
    try:
        q = core.read(interaction.question_path(identifier))
        interaction.validate_question(q)
        import notifications
        if notifications.project_suppression(q):
            return False
        # Initial ask is in dispatching state while the real transport gate runs.
        # Later replay of that ask, or a follow-up after it, must not wake hub again.
        if match[2] and notifications.question_already_notified(q):
            return False
        if match[1] and q.get('question_delivery', {}).get('state') != 'dispatching' and notifications.question_already_notified(q):
            return False
        return (q['id'] == identifier and q['hub_thread'] == target and q['state'] == 'pending'
                and q.get('kind') in (None, 'question')
                and os.environ.get('CODEX_THREAD_ID') in (q['origin_thread'], q['hub_thread']))
    except (ValueError, OSError, KeyError):
        return False


def suppression(options):
    folder = ato_config.interaction_directory()
    hub_path = folder/'hub.json'; policy_path = folder/'notification-policy.json'
    if not hub_path.exists() or not policy_path.exists():
        return None
    hub = json.loads(hub_path.read_text()); policy = json.loads(policy_path.read_text())
    target = options['--thread']
    if (policy.get('mode') != 'user_input_only' or policy.get('hub_thread') != hub.get('thread_id')
            or target.lower() != hub.get('thread_id')):
        return None
    if pending_user_question(options['--message'], hub['thread_id']):
        return None
    if pending_failure_notice(options['--message'], hub['thread_id']):
        return None
    return dict(folder=folder, hub=hub['thread_id'], source=policy.get('source'))


def pending_failure_notice(text, target):
    match = re.match(r'^\[ctask-followup id=(failure-[0-9a-f]{20}) reminder=0\](?:\n|$)', text)
    if not match:
        return False
    sys.path.insert(0, RUNTIME)
    import control
    import failure_attention
    import notifications
    try:
        with control.database() as con:
            found = con.execute('select body from actions where id=?', (match[1],)).fetchone()
            sent = con.execute('select body from outbox where key=?', ('followup:'+match[1]+':0',)).fetchone()
        if not found or not sent:
            return False
        action, claim = json.loads(found[0]), json.loads(sent[0])
        return (action['hub'] == target and os.environ.get('CODEX_THREAD_ID') in (action['origin'], target)
                and not notifications.project_suppression(action) and failure_attention.pending(action)
                and claim['state'] == 'dispatching' and claim['target'] == target
                and claim.get('message_sha256') == hashlib.sha256(text.encode()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def record(options, rule):
    folder = rule['folder']
    row = dict(at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               source_thread=os.environ.get('CODEX_THREAD_ID'), target=rule['hub'],
               state='recorded_only', reason='noncanonical user-input-only hub message',
               message_sha256=hashlib.sha256(options['--message'].encode()).hexdigest(),
               message_bytes=len(options['--message'].encode()), policy_source=rule['source'])
    # Metadata only: no message body, credentials or private business text.
    lock_fd = os.open(folder/'hub-suppressed-queue.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder/'hub-suppressed-queue.jsonl'
        if path.exists() and path.stat().st_size >= 1024*1024:
            os.replace(path, path.with_suffix('.jsonl.1'))
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False)+'\n'); stream.flush(); os.fsync(stream.fileno())
    return row


def main(argv=None, native=NATIVE):
    argv = sys.argv[1:] if argv is None else argv
    options = queue_options(argv)
    if options is not None:
        try:
            rule = suppression(options)
            if rule is not None:
                record(options, rule)
                print('Recorded background hub update locally; no message queued. Use ctask ask for actual user input.')
                return 0
        except (ValueError, OSError, KeyError) as exc:
            print('Hub queue routing could not be verified; no message queued: '+type(exc).__name__, file=sys.stderr)
            return 2
    os.execv(native, [native]+argv)


if __name__ == '__main__':
    raise SystemExit(main())
