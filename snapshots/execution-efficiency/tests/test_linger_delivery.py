"""Inactive delivery integrity, portability and rollback artifact preservation."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/prepare_linger_candidate.py'
spec = importlib.util.spec_from_file_location('linger_packaging', SCRIPT)
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


class LingerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-linger-bundle-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'source'; self.root.mkdir()
        for name, data in {'runner.py': b'# source\n', 'README.md': b'Portable fixture\n',
                           'tests/test_old.py': b'# protected predecessor fixture\n',
                           'bin/ctask': b'#!/bin/sh\nexit 0\n'}.items():
            p = self.root/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
        (self.root/'bin/ctask').chmod(0o755)
        old = self.root/pack.OLD_MANIFEST; old.parent.mkdir(parents=True)
        self.old_bytes = b'{"version":3,"runtime_activated":false}\n'; old.write_bytes(self.old_bytes)
        self.old_archive = old.parent/'candidate.tar.gz'; self.old_archive.write_bytes(b'historical proof only')
        self.baseline = Path(self.tmp.name)/'external-baseline.json'
        hashes, _ = pack.inventory(self.root)
        self.baseline.write_text(json.dumps(dict(predecessor_manifest_sha256=pack.digest(self.old_bytes),
            seed_files=hashes, protected_tests={'tests/test_old.py': hashes['tests/test_old.py']})))

    def prepare(self):
        return pack.prepare(self.root, self.baseline)

    def test_identical_rebuild_has_identical_manifest_archive_bytes_and_preserves_history(self):
        first = self.prepare()
        before = ((self.root/pack.MANIFEST).read_bytes(), (self.root/pack.ARCHIVE).read_bytes())
        self.assertFalse(first['runtime_activated']); self.assertFalse(first['overall_accepted'])
        self.assertEqual(first, self.prepare())
        self.assertEqual(before, ((self.root/pack.MANIFEST).read_bytes(), (self.root/pack.ARCHIVE).read_bytes()))
        self.assertEqual(pack.check(self.root, self.baseline), first)
        self.assertEqual((self.root/pack.OLD_MANIFEST).read_bytes(), self.old_bytes)
        self.assertEqual(self.old_archive.read_bytes(), b'historical proof only')

    def test_archive_metadata_and_extracted_portable_check_without_original_host(self):
        manifest = self.prepare()
        out = Path(self.tmp.name)/'extracted'; out.mkdir()
        with tarfile.open(self.root/pack.ARCHIVE, 'r:gz') as bundle:
            for member in bundle.getmembers():
                self.assertTrue(member.isfile()); self.assertEqual((member.uid, member.gid, member.mtime), (0,0,0))
                self.assertFalse(Path(member.name).is_absolute()); self.assertNotIn('..', Path(member.name).parts)
            bundle.extractall(out, filter='data')
        # The archive is external to its own members, as in a transferred bundle.
        (out/pack.ARCHIVE).write_bytes((self.root/pack.ARCHIVE).read_bytes())
        self.assertEqual(pack.check(out), manifest)
        self.assertEqual((out/'bin/ctask').stat().st_mode & 0o777, 0o755)
        self.assertFalse((out/pack.OLD_MANIFEST).exists())

    def test_source_change_addition_removal_and_mode_change_are_detected(self):
        self.prepare()
        path = self.root/'runner.py'; original = path.read_bytes(); mode = path.stat().st_mode & 0o777
        path.write_bytes(original+b'# changed\n')
        with self.assertRaises(ValueError): pack.check(self.root)
        path.write_bytes(original); path.chmod(mode ^ 0o100)
        with self.assertRaises(ValueError): pack.check(self.root)
        path.chmod(mode)
        extra = self.root/'new.py'; extra.write_text('# addition')
        with self.assertRaises(ValueError): pack.check(self.root)
        extra.unlink(); path.unlink()
        with self.assertRaises(ValueError): pack.check(self.root)

    def test_protected_test_change_cannot_be_blessed_by_regeneration(self):
        self.prepare()
        (self.root/'tests/test_old.py').write_text('# changed protected test')
        with self.assertRaisesRegex(ValueError, 'protected predecessor'): self.prepare()

    def test_test_workflow_configuration_is_portable_hashed_and_mode_checked(self):
        name = '.github/workflows/test.yml'
        workflow = self.root/name; workflow.parent.mkdir(parents=True)
        original = b'name: Tests\non: [push]\njobs: {}\n'
        workflow.write_bytes(original); workflow.chmod(0o644)
        manifest = self.prepare()
        self.assertEqual(manifest['files'][name], pack.digest(original))
        self.assertEqual(manifest['file_modes'][name], 0o644)
        with tarfile.open(self.root/pack.ARCHIVE, 'r:gz') as bundle:
            self.assertEqual(bundle.extractfile(name).read(), original)
        workflow.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'mode/manifest differs'): pack.check(self.root)
        workflow.chmod(0o644); workflow.write_bytes(original+b'# changed\n')
        with self.assertRaisesRegex(ValueError, 'manifest differs'): pack.check(self.root)

    def test_symlink_input_never_copies_external_bytes(self):
        outside = Path(self.tmp.name)/'outside'; outside.write_text('must not enter bundle')
        (self.root/'runner.py').unlink(); (self.root/'runner.py').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'): self.prepare()

    def test_symlink_output_directory_cannot_receive_artifacts(self):
        outside = Path(self.tmp.name)/'outside'; outside.mkdir()
        (self.root/pack.BUILD).symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink candidate output'): self.prepare()
        self.assertEqual(list(outside.iterdir()), [])

    def test_task_auth_dependency_and_git_state_are_excluded(self):
        for name in ('.codex/tasks/private.json', '.aws/auth.json', 'node_modules/dependency.py',
                     'private/credentials.json', '.env', 'build/runtime/state.json'):
            p = self.root/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('private sentinel')
        manifest = self.prepare()
        data = (self.root/pack.ARCHIVE).read_bytes()
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as bundle:
            self.assertFalse(any(b'private sentinel' in bundle.extractfile(m).read() for m in bundle.getmembers()))
        self.assertFalse(set(manifest['files']) & {'.env', 'build/runtime/state.json'})

    def test_archive_corruption_and_forged_manifest_are_detected(self):
        self.prepare()
        archive = self.root/pack.ARCHIVE; original = archive.read_bytes()
        archive.write_bytes(original+b'unexpected trailer')
        with self.assertRaisesRegex(ValueError, 'archive bytes'): pack.check(self.root)
        archive.write_bytes(original)
        manifest = self.root/pack.MANIFEST
        data = json.loads(manifest.read_text()); data['runtime_activated'] = True
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'manifest differs'): pack.check(self.root)

    def test_changed_provenance_or_predecessor_is_refused(self):
        self.prepare()
        original = self.baseline.read_bytes()
        data = json.loads(original); data['extra'] = 'changed baseline'
        self.baseline.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'external baseline'): pack.check(self.root, self.baseline)
        with self.assertRaisesRegex(ValueError, 'overwrite frozen provenance'): self.prepare()
        self.baseline.write_bytes(original)
        (self.root/pack.OLD_MANIFEST).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'historical predecessor'): pack.check(self.root)
        with self.assertRaisesRegex(ValueError, 'original predecessor'): self.prepare()
