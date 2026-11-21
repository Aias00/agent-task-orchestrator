#!/usr/bin/env python3
"""Offline neutral CLI example; all identities/artifacts live in a private temp root.

Uses the inherited neutral fixture only to provision synthetic local identities
and a temporary repository. Does not start a workflow executor or send a model,
native queue, notification or business request. Requires Python 3.11+.
"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from test_neutral_hub import NeutralHubTests


def main():
    fixture = NeutralHubTests('runTest')
    try:
        fixture.setUp()
        material = fixture.repo/'diagnostic-material.txt'
        material.write_text('Synthetic private diagnostic report; no user decision.\n')
        report = fixture.json_cli('internal-report', 'submit', fixture.workflow,
            '--request-id', 'neutral-demo-material-v1', '--artifact', material,
            '--summary', 'Explicitly requested synthetic diagnosis',
            '--source', 'synthetic fixture instruction', '--actor-file', fixture.origin_grant)
        listed = fixture.json_cli('internal-report', 'inbox', fixture.workflow,
            '--actor-file', fixture.hub_grant)
        read = fixture.json_cli('internal-report', 'read', fixture.workflow,
            '--id', report['id'], '--source', 'explicit fixture maintainer read',
            '--actor-file', fixture.hub_grant)
        assigned = fixture.json_cli('internal-report', 'assign', fixture.workflow,
            '--id', report['id'], '--next-action', 'inspect exactly this synthetic evidence',
            '--seconds', 60, '--source', 'explicit fixture bounded assignment',
            '--actor-file', fixture.hub_grant)
        print(json.dumps(dict(id=report['id'], submission=report['submission_state'],
            inbox_before_read=listed['reports'][0]['inbox_state'], inbox_after_read=read['inbox_state'],
            assignment=assigned['assignment_state'], handling_progress=assigned['handling_progress'],
            wakeup=report['wakeup']['state'], user_notice=report['user_notice']['state'],
            actual_execution=assigned['actual_execution'], temporary_fixture_removed_on_exit=True), indent=2))
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
