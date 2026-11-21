"""Source-only installation boundaries; no live hub, agent or provider."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SourceDistributionTests(unittest.TestCase):
    def test_optional_helpers_use_selected_checkout_and_do_not_start_controller(self):
        with tempfile.TemporaryDirectory(prefix='ato install helpers ') as folder:
            base = Path(folder)
            env = dict(os.environ, ATO_HOME=str(base/'ato'), CODEX_HOME=str(base/'native'))
            for name in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'ATO_ACTOR_FILE'):
                env.pop(name, None)
            result = subprocess.run([sys.executable, str(ROOT/'scripts/install.py'),
                '--helpers', '--bin-dir', str(base/'bin with spaces')],
                capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual({Path(p).name for p in receipt['helpers']},
                             {'ctask-notify', 'ato-read-handoff'})
            for path in receipt['helpers']:
                self.assertIn(str(ROOT), Path(path).read_text())
            reader = base/'bin with spaces/ato-read-handoff'
            help_result = subprocess.run([str(reader), '--help'], env=env,
                                         capture_output=True, text=True)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            rejected = subprocess.run([str(base/'bin with spaces/ctask-notify'), 'start', 'untrusted'],
                                      env=env, capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 2)
            self.assertFalse((base/'ato').exists())
            self.assertFalse((base/'native').exists())

    def test_existing_helper_is_rejected_before_any_launcher_is_written(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            helper = base/'ato-read-handoff'; helper.write_text('operator-owned')
            result = subprocess.run([sys.executable, str(ROOT/'scripts/install.py'),
                '--helpers', '--bin-dir', str(base)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(helper.read_text(), 'operator-owned')
            self.assertFalse((base/'ctask').exists())

    def test_documented_distribution_has_no_private_absolute_profile_paths(self):
        for name in ('pi_acp_exec.py', 'pi_acp_worker.py', 'pi_session_profile.py',
                     'scripts/derive_pi_acp.py', 'bin/ctask-notify', 'bin/ato-read-handoff'):
            text = (ROOT/name).read_text()
            self.assertNotRegex(text, r"Path\(['\"]/(?:Users|Volumes)/")
        self.assertTrue((ROOT/'tests/fixtures/pi-error-end-turn.json').is_file())


if __name__ == '__main__':
    unittest.main()
