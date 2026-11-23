"""Installation selects a sealed source and is inert in a temporary home."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from release_support import fake_env, snapshot, unseal, write


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory(prefix='ato install '))).resolve()
        self.root = snapshot(self.base/'snapshot with spaces', executable=True)
        self.addCleanup(unseal, self.root)
        self.target = self.base/'bin with spaces'
        self.skills = self.base/'skill parent'
        self.env = fake_env(self.base)
        self.cmd = [str(self.root/'.venv/bin/python'), '-IB', str(self.root/'scripts/install.py'),
                    '--bin-dir', str(self.target), '--skills-dir', str(self.skills)]

    def install(self, *extra):
        return subprocess.run(self.cmd + list(extra), env=self.env, capture_output=True, text=True, timeout=15)

    def test_one_launcher_one_skill_idempotently_installed_with_spaces(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        launcher = Path(receipt['launcher'])
        self.assertEqual(list(self.target.iterdir()), [launcher])
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        skill = self.skills/'ato/SKILL.md'
        self.assertEqual(skill.read_bytes(), (self.root/'skills/ato/SKILL.md').read_bytes())
        before = launcher.stat().st_ino, skill.stat().st_ino
        repeat = self.install()
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(before, (launcher.stat().st_ino, skill.stat().st_ino))
        help_result = subprocess.run([launcher, '--help'], env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('inbox', help_result.stdout)
        status = subprocess.run([launcher, 'system', 'status'], env=dict(self.env, PATH=''),
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(status.returncode, 0, status.stderr + status.stdout)
        self.assertEqual(json.loads(status.stdout)['data']['release'], receipt['release'])
        self.assertFalse((self.base/'ato').exists())
        self.assertFalse((self.base/'native').exists())

    def test_previous_launcher_and_foreign_skill_are_refused_without_writes(self):
        for name in ('ctask', 'ctask-notify', 'ato-read-handoff', 'ato-hub', 'ato'):
            with self.subTest(name=name):
                target = write(self.target, name, 'unrelated content', 0o700)
                result = self.install()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(self.target.iterdir()), [target])
                self.assertEqual(target.read_text(), 'unrelated content')
                self.assertFalse(self.skills.exists())
                target.unlink()
        skill = write(self.skills, 'ato/SKILL.md', 'different file')
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(skill.read_text(), 'different file')
        self.assertEqual(list(self.target.iterdir()), [])

    def test_retired_installer_options_are_rejected(self):
        for args in (['--helpers'], ['--hub'], ['--force'], ['--hub-skills-dir', str(self.skills)]):
            self.assertEqual(self.install(*args).returncode, 2)
        self.assertFalse(self.target.exists())
