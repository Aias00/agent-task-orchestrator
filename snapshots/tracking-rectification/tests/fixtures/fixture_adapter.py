"""Trusted offline adapter installed only by the fixture entry point."""
import json
import os
from pathlib import Path
import sys

import agent_registry
from transport import DeliveryResult


class FixtureAdapter:
    kind = 'fixture_neutral_e2e'
    capabilities = frozenset({'worker', 'planner', 'progress', 'session_create', 'session_verify', 'session_actor', 'session_observe', 'transport'})

    def progress_path(self, directory, label, log):
        return log

    def normalize_progress(self, row):
        if row.get('event') == 'fixture_tool_start':
            return [('start', row['id'], row['tool'], row['arguments'])]
        if row.get('event') == 'fixture_tool_end':
            return [('end', row['id'], None, None)]
        return []

    def create_session(self, repo, contract, name, identifier, persist):
        path = Path(repo)/'fixture-created.json'
        path.write_text(json.dumps(dict(name=name, identifier=identifier)))
        return agent_registry.CreatedSession('created:opaque', str(repo), contract['sandbox'], 'never',
                                             'fixture exact identity and policy readback')

    def command(self, request):
        fixture = Path(__file__).with_name('agent_fixture.py')
        return [sys.executable, str(fixture), 'planner' if request.planner else 'worker', str(request.output)]

    def verify_session(self, identity, project):
        return identity in ('origin:opaque', 'hub:opaque', 'created:opaque') and Path(project).is_dir()

    def is_actor(self, identity, project):
        return os.environ.get('FIXTURE_ACTOR') == identity

    def send(self, endpoint, message):
        with (Path(endpoint['project'])/'sent.jsonl').open('a') as f:
            f.write(json.dumps(dict(target=endpoint['session_id'], text=message['body']))+'\n')
        if os.environ.get('FIXTURE_SEND_UNKNOWN') == '1':
            return DeliveryResult('delivery_unknown')
        return DeliveryResult('delivered', 'fixture-receipt')

    def observe_session(self, identity, project, correlation_id, cursor):
        path = Path(project)/'fixture-events.jsonl'
        rows = path.read_text().splitlines() if path.exists() else []
        events = []
        for line in rows[cursor or 0:]:
            row = json.loads(line)
            if row['correlation_id'] == correlation_id:
                ref = agent_registry.SessionRef(self.kind, identity, project)
                events.append(agent_registry.AgentEvent(row['kind'], ref, correlation_id, 'offline fixture log',
                                                        turn_id=row.get('turn_id', ''), text=row.get('text', '')))
        return events, len(rows)

    def session_cursor(self, identity, project):
        path = Path(project)/'fixture-events.jsonl'
        return len(path.read_text().splitlines()) if path.exists() else 0


def register():
    if FixtureAdapter.kind not in agent_registry.kinds():
        agent_registry.register(FixtureAdapter())
