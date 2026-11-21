#!/usr/bin/env python3
"""Origin-owned actual Codex qualification contract and proof validator.

The executor is origin-only. Import/--describe/--validate-proof are inert.
--existing-profile runs the explicit bounded host contract, never by a worker.
No fixture can satisfy an actual native proof.
This entry never silently substitutes synthetic results for actual qualification.
"""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ARMS = ('foreground', 'legacy_ato', 'warm_ato')
REQUIRED_ASSERTIONS = ('exact_native_identity_verified', 'same_profile_model_permissions',
    'two_turns_same_session', 'tool_artifact_verified', 'independent_checks_passed',
    'authority_preserved', 'warm_resume_not_replayed', 'all_three_arms_verified',
    'workflow_acceptance_independent')
HASH = re.compile(r'[0-9a-f]{64}')
PROOF_SCHEMA = dict(version=1, kind='actual_codex_worker_resume_benchmark',
    origin_only=True, fixture_allowed=False, max_native_turns=12, max_wall_seconds=1200,
    samples_per_arm=2, turns_per_sample=2, arms=list(ARMS),
    required_assertions=list(REQUIRED_ASSERTIONS),
    required_provenance=['codex_binary_sha256', 'model', 'profile_fingerprint',
        'candidate_manifest_sha256', 'evidence', 'initial_files_sha256', 'contract_sha256'],
    sample_fields=['turns', 'artifact_sha256', 'initial_files_sha256', 'contract_sha256',
        'independent_check', 'profile_fingerprint', 'model'],
    turn_fields=['native_id', 'turn_id', 'message_id', 'cleanup_confirmed', 'delivery',
        'profile_fingerprint', 'model', 'argv_sha256', 'evidence_sha256'],
    prohibitions=['auth/config mutation or extraction', 'production effects',
        'automatic replay/resample on unknown/failure', 'permission widening',
        'unverified session, model, message, turn or profile', 'fixture presented as actual'])


def validate_proof(proof):
    def require(ok, message):
        if not ok:
            raise ValueError(message)
    def digest(value):
        return isinstance(value, str) and bool(HASH.fullmatch(value))
    require(isinstance(proof, dict), 'proof must be an object')
    require(proof.get('kind') == PROOF_SCHEMA['kind'] and proof.get('fixture_only') is False,
            'only an explicit actual proof is admissible')
    for name in REQUIRED_ASSERTIONS:
        require(proof.get(name) is True, 'unverified actual property: '+name)
    for name in ('production_side_effects', 'auth_read_or_copied', 'native_config_mutated', 'automatic_resample'):
        require(proof.get(name) is False, 'authority boundary unknown: '+name)
    require(type(proof.get('wall_seconds')) in (int, float) and 0 < proof['wall_seconds'] <= 1200,
            'bounded actual wall time required')
    for name in ('codex_binary_sha256', 'profile_fingerprint', 'candidate_manifest_sha256', 'initial_files_sha256', 'contract_sha256'):
        require(digest(proof.get(name)), 'missing exact digest: '+name)
    require(isinstance(proof.get('model'), str) and bool(proof['model'].strip()), 'effective native model required')
    evidence = proof.get('evidence')
    require(isinstance(evidence, list) and evidence and all(isinstance(e, dict) and digest(e.get('sha256')) for e in evidence),
            'retained evidence digests required')
    arms = proof.get('arms', {})
    require(isinstance(arms, dict) and set(arms) == set(ARMS), 'exact three arms required')
    total, native_turns, native_messages, identities, artifacts = 0, set(), set(), set(), {}
    for arm in ARMS:
        samples = arms[arm].get('samples', [])
        require(isinstance(samples, list) and len(samples) == 2, 'two samples per arm required')
        for index, sample in enumerate(samples):
            for field in ('model', 'profile_fingerprint', 'initial_files_sha256', 'contract_sha256'):
                require(sample.get(field) == proof[field], 'same-contract/profile mismatch: '+field)
            require(digest(sample.get('artifact_sha256')), 'actual artifact hash required')
            artifacts.setdefault(index, set()).add(sample['artifact_sha256'])
            check = sample.get('independent_check', {})
            require(type(check.get('returncode')) is int and check['returncode'] == 0 and check.get('fresh') is True
                    and digest(check.get('evidence_sha256')) and check.get('actor') == 'host',
                    'fresh host independent check required')
            turns = sample.get('turns', [])
            require(isinstance(turns, list) and len(turns) == 2, 'two actual turns per sample required')
            sessions = set()
            for turn in turns:
                require(turn.get('cleanup_confirmed') is True and turn.get('delivery') == 'delivered',
                        'unknown delivery/cleanup cannot pass')
                for field in ('model', 'profile_fingerprint'):
                    require(turn.get(field) == proof[field], 'turn profile/model drift')
                for field in ('argv_sha256', 'evidence_sha256'):
                    require(digest(turn.get(field)), 'exact native argv/evidence digest required')
                for field, seen in (('turn_id', native_turns), ('message_id', native_messages)):
                    key = turn.get(field)
                    require(isinstance(key, str) and bool(key) and key not in seen, 'missing/replayed '+field)
                    seen.add(key)
                identifier = turn.get('native_id')
                require(isinstance(identifier, str) and bool(identifier), 'exact native identity required')
                sessions.add(identifier)
                total += 1
            require(len(sessions) == (2 if arm == 'legacy_ato' else 1), 'wrong cold/warm session relationship')
            require(not identities.intersection(sessions), 'native identity shared across task/sample/arm')
            identities.update(sessions)
    require(all(len(a) == 1 for a in artifacts.values()), 'actual arm artifacts differ')
    require(type(proof.get('native_turn_count')) is int and proof['native_turn_count'] == total == 12,
            'exact bounded total turn count required')
    # Latency speedup is optional; missing provider metrics must remain unknown.
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    parser.add_argument('--existing-profile', action='store_true')
    parser.add_argument('--max-turns', type=int, default=12)
    parser.add_argument('--describe', action='store_true')
    parser.add_argument('--validate-proof')
    args = parser.parse_args()
    if args.describe:
        print(json.dumps(PROOF_SCHEMA, indent=2))
        return 0
    if args.validate_proof:
        validate_proof(json.loads(Path(args.validate_proof).read_text()))
        print('actual proof structure verified; native provenance still requires origin evidence review')
        return 0
    if not args.output or not args.existing_profile or args.max_turns != 12:
        parser.error('origin contract requires --output PATH --existing-profile --max-turns 12')
    from scripts.execution_efficiency_actual import run_origin
    return run_origin(args.output, args.max_turns)

if __name__ == '__main__':
    raise SystemExit(main())
