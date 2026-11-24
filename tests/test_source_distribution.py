"""Source distribution boundaries independent of the installed state."""
from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SourceDistributionTests(unittest.TestCase):
    def test_one_launcher_and_one_skill_without_legacy_entrypoints(self):
        self.assertEqual(sorted(p.name for p in (ROOT/'bin').iterdir()), ['ato'])
        self.assertEqual([p.parent.name for p in (ROOT/'skills').rglob('SKILL.md')], ['ato'])
        data = tomllib.loads((ROOT/'pyproject.toml').read_text())
        setuptools = data['tool']['setuptools']
        self.assertNotIn('hub_cli', setuptools['py-modules'])
        self.assertNotIn('legacy_cli', setuptools['packages'])
        self.assertFalse(data['project'].get('scripts'))
        skills = [path for name, paths in setuptools['data-files'].items() if '/skills/' in name for path in paths]
        self.assertEqual(skills, ['skills/ato/SKILL.md'])

    def test_batch4_distribution_contains_release_and_native_support_files(self):
        data = tomllib.loads((ROOT/'pyproject.toml').read_text())['tool']['setuptools']
        paths = {path for entries in data['data-files'].values() for path in entries}
        self.assertIn('ato_release_lib', data['py-modules'])
        self.assertIn('bin/ato', paths)
        self.assertEqual({path for path in paths if path.endswith('.mjs')},
                         {path.name for path in ROOT.glob('*.mjs')})
        for name in ('package.json', 'package-lock.json', 'compatibility.json'):
            self.assertIn('integrations/pi-acp/' + name, paths)
        self.assertTrue(all((ROOT/path).is_file() for path in paths))

    def test_documented_distribution_has_no_private_absolute_profile_paths(self):
        for name in ('pi_acp_exec.py', 'pi_acp_worker.py', 'pi_session_profile.py',
                     'scripts/derive_pi_acp.py', 'bin/ato', 'ato_runtime.py', 'ato_host.py'):
            self.assertNotRegex((ROOT/name).read_text(), r"Path\(['\"]/(?:Users|Volumes)/")
        self.assertTrue((ROOT/'tests/fixtures/pi-error-end-turn.json').is_file())
