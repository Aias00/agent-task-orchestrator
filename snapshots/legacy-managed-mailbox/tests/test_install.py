import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_install_launcher_and_skill_in_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='ctask install ') as folder:
            base = Path(folder)
            result = subprocess.run([sys.executable, str(ROOT/'scripts/install.py'), '--bin-dir',
                str(base/'local bin'), '--skills-dir', str(base/'skills')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = json.loads(result.stdout)
            result = subprocess.run([installed['launcher'], '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('followups', result.stdout)
            text = (base/'skills/codex-long-task/SKILL.md').read_text()
            self.assertIn(str(ROOT/'README.md'), text)
            self.assertNotIn('CTASK_SOURCE', text)
            self.assertTrue((base/'skills/codex-long-task/references/controller.md').exists())

    def test_existing_launcher_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder); (base/'ctask').write_text('existing launcher')
            result = subprocess.run([sys.executable, str(ROOT/'scripts/install.py'), '--bin-dir', folder],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((base/'ctask').read_text(), 'existing launcher')


if __name__ == '__main__':
    unittest.main()
