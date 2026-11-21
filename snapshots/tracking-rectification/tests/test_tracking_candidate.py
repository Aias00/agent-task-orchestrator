"""Portable packaging and origin-probe fences, using synthetic records only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/(name+'.py'))
    loaded = importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded


package = module('prepare_tracking_candidate')
probe = module('verify_tracking_readonly')


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ato-portable-package-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'source'; self.root.mkdir()
        for name, data in {'main.py': 'print("inert fixture")\n', 'README.md': 'Synthetic fixture\n',
                'tests/test_old.py': '# byte-protected fixture\n'}.items():
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(data)
        provenance = self.root/package.PROVENANCE; provenance.mkdir(parents=True)
        baseline = dict(seed_files={n: package.sha(self.root/n) for n in ('main.py', 'README.md', 'tests/test_old.py')},
                        protected_tests={'tests/test_old.py': package.sha(self.root/'tests/test_old.py')},
                        predecessor_test_count=1)
        (provenance/'baseline.json').write_bytes(package.encoded(baseline))
        (provenance/'acceptance.py').write_text('# immutable synthetic acceptance provenance\n')
        scripts = self.root/'scripts'; scripts.mkdir()
        shutil.copyfile(ROOT/'scripts/prepare_tracking_candidate.py', scripts/'prepare_tracking_candidate.py')

    def build(self):
        expected = package.expected(self.root)
        folder = self.root/package.BUILD; folder.mkdir(parents=True, exist_ok=True)
        (self.root/package.MANIFEST).write_bytes(package.encoded(expected))
        (self.root/package.ARCHIVE).write_bytes(package.archive_bytes(expected, self.root))
        return expected

    def test_deterministic_complete_archive_and_independent_extraction_rebuild(self):
        expected = self.build()
        archive = self.root/package.ARCHIVE
        first = archive.read_bytes()
        os.utime(self.root/'main.py', (123, 123))
        self.assertEqual(first, package.archive_bytes(package.expected(self.root), self.root))
        extracted = Path(self.temp.name)/'extracted'; extracted.mkdir()
        with tarfile.open(archive) as bundle:
            self.assertTrue(all(m.isfile() and not m.issym() and m.uid == m.gid == m.mtime == 0 for m in bundle.getmembers()))
            bundle.extractall(extracted, filter='data')
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ATO_', 'CTASK_', 'CODEX_'))}
        env.update(ATO_HOME=str(extracted/'absent-ato'), CODEX_HOME=str(extracted/'absent-native'), PYTHONDONTWRITEBYTECODE='1')
        command = [sys.executable, '-B', str(extracted/'scripts/prepare_tracking_candidate.py')]
        for argv in ([*command, '--check', '--archive', str(archive)], command, [*command, 'prepare'], [*command, '--check']):
            result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(first, (extracted/package.ARCHIVE).read_bytes())
        self.assertEqual(expected, json.loads((extracted/package.MANIFEST).read_text()))
        self.assertFalse((extracted/'absent-ato').exists()); self.assertFalse((extracted/'absent-native').exists())

    def test_protected_bytes_cannot_be_resealed(self):
        (self.root/'tests/test_old.py').write_text('# weakened test\n')
        with self.assertRaisesRegex(ValueError, 'protected'):
            package.expected(self.root)

    def test_omitted_seed_source_cannot_be_resealed(self):
        (self.root/'main.py').unlink()
        with self.assertRaisesRegex(ValueError, 'omitted'):
            package.expected(self.root)

    def test_source_drift_added_source_and_archive_corruption_fail_check(self):
        self.build(); package.check(self.root)
        (self.root/'new.py').write_text('# newly unsealed source\n')
        with self.assertRaisesRegex(ValueError, 'manifest'):
            package.check(self.root)
        (self.root/'new.py').unlink()
        archive = self.root/package.ARCHIVE; archive.write_bytes(archive.read_bytes()+b'corruption')
        with self.assertRaisesRegex(ValueError, 'archive'):
            package.check(self.root)

    def test_symlink_file_and_directory_cannot_escape(self):
        outside = Path(self.temp.name)/'outside'; outside.mkdir(); (outside/'bad.py').write_text('# outside\n')
        (self.root/'linked.py').symlink_to(outside/'bad.py')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            package.expected(self.root)
        (self.root/'linked.py').unlink()
        (self.root/'tests/linked').symlink_to(outside, target_is_directory=True)
        # glob on newer Pythons does not recurse into symlink directories. Such
        # a directory is never represented in the archive either way.
        try:
            values = package.expected(self.root)['files']
            self.assertFalse(any('linked' in name for name in values))
        except ValueError as exc:
            self.assertIn('symlink', str(exc))

    def test_private_runtime_and_dependency_trees_not_packaged(self):
        for name in ('.codex/tasks/state.json', 'node_modules/agent/index.py', '.aws/credentials',
                     'build/old/state.json', 'private/transcript.json', '.venv/lib/secrets.py'):
            path = self.root/name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('synthetic private bytes')
        manifest = self.build()
        self.assertFalse(any(set(Path(n).parts) & package.FORBIDDEN or n.startswith('build/') for n in manifest['files']))
        self.assertNotIn(b'synthetic private bytes', package.archive_bytes(manifest, self.root))

    def test_path_traversal_and_special_names_rejected(self):
        for name in ('/absolute.py', '../outside.py', 'private/secret.json', 'tests/../bad.py', '.git/config'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                package.portable(name)

    def test_detached_hash_receipt_tamper_rejected(self):
        self.build()
        receipt = package.check(self.root)
        receipt['runtime_activated'] = True
        (self.root/package.BUILD/'hashes.json').write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, 'receipt'):
            package.check(self.root)


class ReadonlyProbeTests(unittest.TestCase):
    def test_cli_rejects_unnamed_records_before_reading_any_live_data(self):
        with self.assertRaisesRegex(ValueError, 'exact version'):
            probe.validate_plan(dict(version=1, old_workflow='/unrelated'))

    def test_source_fingerprint_matches_frozen_algorithm(self):
        # Independent computation; never execute external acceptance (it writes
        # scheduler-owned evidence) or inspect a real record.
        patterns = ['*.py', '*.mjs', 'README.md', 'AGENTS.md', 'requirements*.lock',
                    'tests/**/*.py', 'tests/**/*.json', 'tests/**/*.mjs',
                    'docs/**/*.md', 'docs/**/*.json', 'docs/**/*.py', 'bin/*',
                    'skills/**/*.md', 'scripts/*.py', 'integrations/pi-acp/package.json',
                    'integrations/pi-acp/package-lock.json']
        paths = {p for pattern in patterns for p in ROOT.glob(pattern) if p.is_file() and not p.is_symlink()}
        values = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}
        self.assertEqual(probe.source_fingerprint(), hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest())

    def test_snapshot_detects_live_bytes_absence_and_native_header_drift(self):
        with tempfile.TemporaryDirectory(prefix='ato-readonly-hash-') as temp:
            root = Path(temp).resolve(); live = root/'state.json'; live.write_text('{}')
            header = root/'native.jsonl'; header.write_text(json.dumps(dict(payload=dict(id='synthetic')))+'\n')
            plan = dict(immutable_live_files={str(live): probe.sha(live)}, absent_live_files=[str(root/'STOP')],
                        native_headers={str(header): dict(native_id='synthetic', header_sha256=probe.sha(header))})
            probe.snapshot(plan)
            # Appended conversation body is not read or pinned as identity.
            with header.open('a') as stream: stream.write('synthetic conversation body\n')
            probe.snapshot(plan)
            (root/'STOP').touch()
            with self.assertRaisesRegex(ValueError, 'absence'): probe.snapshot(plan)
            (root/'STOP').unlink(); live.write_text('{"changed":true}')
            with self.assertRaisesRegex(ValueError, 'hash drift'): probe.snapshot(plan)
            live.write_text('{}'); header.write_text('{}\n')
            with self.assertRaisesRegex(ValueError, 'header'): probe.snapshot(plan)

    def test_exact_sql_notice_import_preserves_unknown_without_unrelated_rows(self):
        with tempfile.TemporaryDirectory(prefix='ato-readonly-sql-') as temp:
            path = Path(temp)/'control.sqlite'
            with sqlite3.connect(path) as con:
                con.executescript('create table actions(id text,body text);create table outbox(key text,body text);')
                con.execute('insert into actions values(?,?)', ('result-fixture', '{"id":"result-fixture"}'))
                con.execute('insert into actions values(?,?)', ('unrelated', '{}'))
                con.execute('insert into outbox values(?,?)', ('followup:result-fixture:0', '{"state":"unknown"}'))
            before = probe.sha(path)
            actions, claims = probe.readonly_rows(path, ['result-fixture'])
            self.assertEqual(actions, [dict(id='result-fixture')])
            self.assertEqual(claims, [('followup:result-fixture:0', dict(state='unknown'))])
            self.assertEqual(probe.sha(path), before)

    def test_audit_fence_forbids_live_writes_sockets_commands_and_unlisted_reads(self):
        # Audit hooks cannot be removed, so test the real fence in a disposable
        # subprocess. All paths and identities are private synthetic fixtures.
        script = '''import importlib.util,sys,tempfile,pathlib,socket,subprocess,sqlite3
spec=importlib.util.spec_from_file_location('probe',sys.argv[1]);p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)
temp=tempfile.TemporaryDirectory(prefix='ato-fence-');r=pathlib.Path(temp.name).resolve();private=r/'observation';private.mkdir();outside=r/'unchanged';outside.write_text('original')
plan=dict(immutable_live_files={},native_headers={},generic_workflows=[],native_home=str(r))
p.install_fence(plan,private,set())
failures=0
for f in (lambda:outside.write_text('forbidden'),lambda:outside.read_text(),lambda:socket.socket(),lambda:subprocess.run(['echo','forbidden']),lambda:sqlite3.connect(str(outside)),lambda:sqlite3.connect(outside.as_uri()+'?mode=ro',uri=True),lambda:outside.unlink()):
 try:f()
 except p.FenceViolation:failures+=1
 else:raise AssertionError('forbidden operation escaped')
(private/'safe').write_text('allowed')
assert (private/'safe').read_text()=='allowed'
print(failures)
# Process exit owns temp cleanup; live paths deliberately never mutated.
temp._finalizer.detach()
'''
        with tempfile.TemporaryDirectory(prefix='ato-fence-owner-') as scratch:
            result = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT/'scripts/verify_tracking_readonly.py')],
                env=dict(os.environ, TMPDIR=scratch), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(result.stdout.strip(), '7')


class ReadonlyProjectionIntegrationTests(unittest.TestCase):
    """All actual-record paths are exercised on synthetic private analogues."""
    def fixture(self, lineage=True):
        import test_approved_delivery as fixtures
        import interaction, control, runner, workflow, workflow_lineage
        f = fixtures.ApprovedDeliveryTests('runTest'); f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        generic = []
        for i in range(2):
            contract = runner.read(f.new/'contract.json')
            contract['objective'] = 'Synthetic generic delivery '+str(i)
            path = f.root/('generic-plan-'+str(i)+'.json'); runner.save(path, contract)
            d = workflow.create(f.repo, path, fixtures.ORIGIN)
            log = d/'evidence'/'verification.log'; log.write_text('Same exact original generic verification\n')
            state = runner.read(d/'state.json')
            state.update(status='completed', owner_pid=None, active_process=None,
                criteria={k: dict(status='passed', evidence=[str(log)]) for k in state['criteria']})
            runner.save(d/'state.json', state)
            generic.append(str(d))
        if lineage:
            a, b = map(Path, generic)
            proof = f.root/'existing-original-lineage.json'
            runner.save(proof, dict(predecessor_workflow=str(a), successor_workflow=str(b),
                predecessor_contract_sha256=workflow_lineage.sha(a/'contract.json'),
                successor_contract_sha256=workflow_lineage.sha(b/'contract.json'), permission_expanded=False,
                origin_thread=fixtures.ORIGIN, hub_thread=fixtures.HUB,
                acceptance_mapping={'deploy': 'deploy', 'public': 'public'},
                source='Synthetic preexisting original compatible operation proof'))
            workflow_lineage.link(a, b, proof, 'Synthetic original immutable linkage')
        identifiers = ['result-'+runner.digest([w, runner.read(Path(w)/'state.json')['contract_hash']])[:20] for w in generic]
        original_action = dict(id=identifiers[0], kind='workflow_result', workflow=generic[0],
            origin=fixtures.ORIGIN, hub=fixtures.HUB, state='needs_user', presented_at=123,
            notice=dict(state='queued', receipt='synthetic-original-receipt'))
        with control.database() as con, con:
            control.put(con, original_action)
        report = f.root/'original-duplicate-report.json'; runner.save(report, dict(results=identifiers))
        # Introduce the original parent-workspace mismatch after the original
        # immutable records were created, exactly as a legacy source analogue.
        f.headers[fixtures.ORIGIN].write_text(json.dumps(dict(type='session_meta',
            payload=dict(id=fixtures.ORIGIN, cwd=str(f.root))))+'\n')
        source_folder = interaction.directory()
        plan = dict(native_home=str(f.native), hub_file=str(source_folder/'hub.json'),
            question_file=str(f.qpath), policy_file=str(source_folder/'notification-policy.json'),
            mutes_file=str(source_folder/'notification-project-mutes.json'),
            control_db=str(control.directory()/'state.sqlite'), generic_workflows=generic,
            approved_criteria={'production': ['deploy', 'public']}, deferred_criteria=['external-source', 'measurement'],
            workspace_bindings={w: str(f.root) for w in [str(f.old), str(f.new), *generic]},
            duplicate_evidence=str(report), operation_receipt=None,
            native_headers={str(path): dict(native_id=actor, header_sha256=probe.sha(path))
                            for actor, path in f.headers.items()})
        self.overrides = patch.multiple(probe, OLD=str(f.old), DELIVERY=str(f.new), QUESTION=f.qid,
                                         RESULTS=set(identifiers), DUPLICATE=str(report))
        self.overrides.start(); self.addCleanup(self.overrides.stop)
        private = f.root/'private-observation'; private.mkdir()
        self.env = patch.dict(os.environ, ATO_HOME=str(private), CODEX_THREAD_ID=fixtures.HUB)
        self.env.start(); self.addCleanup(self.env.stop)
        return f, plan, private

    def test_end_to_end_real_candidate_projections_preserve_original_synthetic_records(self):
        f, plan, private = self.fixture()
        inputs = [p for w in (f.old, f.new, *map(Path, plan['generic_workflows'])) for p in w.rglob('*') if p.is_file()]
        inputs += [Path(plan[k]) for k in ('hub_file', 'question_file', 'policy_file', 'mutes_file', 'control_db')]
        before = {str(p): probe.sha(p) for p in inputs}
        output = probe.projection(plan, private)
        self.assertTrue(output['same_physical_delivery_dedup_verified'])
        self.assertEqual(output['raw_old_workflow_status'], 'blocked')
        self.assertEqual(set(output['retained_diagnostic_criteria']), {'external-source', 'measurement'})
        self.assertEqual(before, {str(p): probe.sha(p) for p in inputs})

    def test_no_legacy_operation_proof_refuses_equivalence_despite_equal_verification(self):
        f, plan, private = self.fixture(lineage=False)
        with self.assertRaisesRegex(ValueError, 'immutable compatible lineage'):
            probe.projection(plan, private)

    def test_complete_synthetic_projection_runs_under_real_audit_fence(self):
        script = '''import sys,os,pathlib
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/tests']
import test_tracking_candidate as tests
case=tests.ReadonlyProjectionIntegrationTests('runTest');f,plan,private=case.fixture()
f.tmp._finalizer.detach()
plan['immutable_live_files']={str(p):tests.probe.sha(p) for p in f.root.rglob('*') if p.is_file() and not p.is_relative_to(private) and p.suffix!='jsonl'}
source={p for pattern in ('*.py','scripts/*.py') for p in pathlib.Path(sys.argv[1]).glob(pattern)}
tests.probe.install_fence(plan,private,source)
result=tests.probe.projection(plan,private)
assert result['same_physical_delivery_dedup_verified'] and result['old_residuals_preserved']
print('synthetic fenced projection passed')
'''
        with tempfile.TemporaryDirectory(prefix='ato-fenced-projection-') as scratch:
            result = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT)],
                env=dict(os.environ, TMPDIR=scratch), capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(result.stdout.strip(), 'synthetic fenced projection passed')

    def test_reviewed_origin_plan_is_exact_bounded_and_preserves_absence_pins(self):
        f, plan, private = self.fixture()
        import copy
        headers = {str(path): dict(native_id=actor, header_sha256=probe.sha(path)) for actor, path in f.headers.items()}
        pinned = {str(p): probe.sha(p) for p in f.root.rglob('*') if p.is_file()
                  and str(p) not in headers and not p.is_relative_to(private)}
        workflows = [str(f.old), str(f.new), *plan['generic_workflows']]
        absent = [str(Path(w)/n) for w in workflows for n in ('STOP', 'user-closure.json', 'workflow-lineage.json')
                  if not (Path(w)/n).exists()]
        absent += [n+s for n in pinned if Path(n).suffix == '.sqlite' for s in ('-wal', '-shm') if not Path(n+s).exists()]
        plan.update(version=1, authorization=probe.AUTHORIZATION, candidate_manifest_sha256='0'*64,
                    source_sha256='1'*64, old_workflow=str(f.old), delivery_workflow=str(f.new),
                    immutable_live_files=pinned, native_headers=headers, absent_live_files=absent)
        probe.validate_plan(plan); probe.snapshot(plan)
        changed = copy.deepcopy(plan); changed['old_workflow'] = str(f.root/'unrelated')
        with self.assertRaisesRegex(ValueError, 'named VPS'): probe.validate_plan(changed)
        changed = copy.deepcopy(plan); changed['absent_live_files'].remove(str(f.old/'STOP'))
        with self.assertRaisesRegex(ValueError, 'pin STOP'): probe.validate_plan(changed)
        changed = copy.deepcopy(plan); changed['workspace_bindings'][str(f.root/'unrelated')] = str(f.root)
        with self.assertRaisesRegex(ValueError, 'outside named'): probe.validate_plan(changed)
        changed = copy.deepcopy(plan); del changed['immutable_live_files'][plan['control_db']]
        with self.assertRaisesRegex(ValueError, 'must be pinned'): probe.validate_plan(changed)
        changed = copy.deepcopy(plan); changed['implicit_approval'] = True
        with self.assertRaisesRegex(ValueError, 'exact version'): probe.validate_plan(changed)
        evidence = f.root/'delivery-authorization-proof.json'; evidence.write_text('{}')
        changed = copy.deepcopy(plan); changed['immutable_live_files'][str(evidence)] = probe.sha(evidence)
        probe.validate_plan(changed)
        credential = f.root/'auth.json'; credential.write_text('{}')
        changed['immutable_live_files'][str(credential)] = probe.sha(credential)
        with self.assertRaisesRegex(ValueError, 'credential-like'): probe.validate_plan(changed)
        changed = copy.deepcopy(plan); transcript = f.native/'unrelated.jsonl'; transcript.write_text('{}\n')
        changed['immutable_live_files'][str(transcript)] = probe.sha(transcript)
        with self.assertRaisesRegex(ValueError, 'first headers only'): probe.validate_plan(changed)


if __name__ == '__main__':
    unittest.main()
