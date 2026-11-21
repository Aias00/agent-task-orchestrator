"""Trusted human bootstrap. Never install this command in the AGY action surface."""
import argparse
from pathlib import Path
import sys
import json

import external_coordinator as external
import local_session_adapter as local
import runner


def provision(repo, contract_file, actor_file, source):
    if not isinstance(source, str) or not source.strip():
        raise ValueError('actual human authorization source required')
    ato, _ = external.isolated_roots()
    repo = external.repository(repo)
    actor_file = Path(actor_file).absolute()
    if actor_file.exists() or actor_file.is_symlink() or repo in actor_file.resolve().parents:
        raise ValueError('actor output must be unused and outside worker repository')
    contract = external.reviewed_contract(runner.read(contract_file))
    root = ato/'external-operator'
    root.mkdir(mode=0o700, exist_ok=True)
    import uuid
    key = root/(uuid.uuid4().hex+'.json')
    session = local.create(repo, 'origin', source, key)
    scope = dict(type=external.SCOPE, repo=str(repo), contract_hash=runner.digest(contract),
        contract=contract, request=external.fingerprint(repo, runner.digest(contract)))
    local._credential(session['reference'], 'origin', source, actor_file, scope)
    # Readback authentication without disclosing root or proof material.
    external.load_scope(actor_file)
    return dict(repo=str(repo), actor_file=str(actor_file), contract_hash=scope['contract_hash'],
                request=scope['request'], source=source, native_origin='synthetic local_cli endpoint')


def main():
    parser = argparse.ArgumentParser(description='HUMAN ONLY: provision exact reviewed ATO external scope')
    for name in ('repo', 'contract', 'actor-file', 'source'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(provision(args.repo, args.contract, args.actor_file, args.source), ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError):
        print('Trusted bootstrap refused; check private homes, unused actor path and exact reviewed contract', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
