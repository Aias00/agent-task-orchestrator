import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from hub_support import repository

ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_optional_hub_launcher_skill_help_and_isolated_default_home(self):
        with tempfile.TemporaryDirectory(prefix='ato hub install ') as folder:
            base=Path(folder).resolve();home=base/'home';home.mkdir();repo=repository(base)
            env=dict(os.environ,HOME=str(home),CODEX_HOME=str(base/'ctask-native'))
            env.pop('ATO_HOME',None)
            result=subprocess.run([sys.executable,str(ROOT/'scripts/install.py'),'--hub','--hub-skills-dir',str(base/'hub skills'),
                '--bin-dir',str(base/'bin')],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);installed=json.loads(result.stdout)
            launcher=Path(installed['hub_launcher']);self.assertEqual(launcher.stat().st_mode&0o777,0o755)
            skill=Path(installed['hub_skill']);self.assertEqual(skill,base/'hub skills/ato-hub-orchestrator/SKILL.md')
            self.assertIn('ato-hub-orchestrator',skill.read_text())
            self.assertIn(str(ROOT/'docs/h1-offline-implementation.md'),skill.read_text())
            help_result=subprocess.run([str(launcher),'--help'],env=env,capture_output=True,text=True)
            self.assertEqual(help_result.returncode,0,help_result.stderr);self.assertIn('hub-list',help_result.stdout)
            listed=subprocess.run([str(launcher),'hub-list'],env=env,capture_output=True,text=True)
            self.assertEqual(listed.returncode,0,listed.stderr);self.assertEqual(json.loads(listed.stdout),[])
            default=home/'.local/share/ato-hub-state';self.assertFalse(default.exists())
            created=subprocess.run([str(launcher),'hub-create','--repo',str(repo),'--kind','agy'],env=env,capture_output=True,text=True)
            self.assertEqual(created.returncode,0,created.stderr)
            self.assertTrue((default/'hubs'/json.loads(created.stdout)['hub_id']/'hub.json').is_file())
            explicit=base/'explicit-hub-home'
            created=subprocess.run([str(launcher),'hub-create','--repo',str(repo),'--kind','codex'],
                env=dict(env,ATO_HOME=str(explicit)),capture_output=True,text=True)
            self.assertEqual(created.returncode,0,created.stderr)
            self.assertTrue((explicit/'hubs'/json.loads(created.stdout)['hub_id']).is_dir())
            self.assertFalse((base/'ctask-native').exists())

    def test_hub_targets_refuse_overwrite_before_touching_other_launchers_and_force_replaces(self):
        for target in ('launcher','skill'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as folder:
                base=Path(folder);bin_dir=base/'bin';bin_dir.mkdir()
                skill=base/'skills/ato-hub-orchestrator/SKILL.md';launcher=bin_dir/'ato-hub'
                if target=='launcher':launcher.write_text('old hub')
                else:skill.parent.mkdir(parents=True);skill.write_text('old skill')
                argv=[sys.executable,str(ROOT/'scripts/install.py'),'--bin-dir',str(bin_dir),'--hub',
                      '--hub-skills-dir',str(base/'skills')]
                rejected=subprocess.run(argv,capture_output=True,text=True)
                self.assertNotEqual(rejected.returncode,0);self.assertFalse((bin_dir/'ctask').exists())
                self.assertEqual((launcher if target=='launcher' else skill).read_text(),'old hub' if target=='launcher' else 'old skill')
                installed=subprocess.run(argv+['--force'],capture_output=True,text=True)
                self.assertEqual(installed.returncode,0,installed.stderr)
                self.assertIn('hub_cli.py',launcher.read_text());self.assertIn('ato-hub-orchestrator',skill.read_text())

    def test_default_and_helpers_install_do_not_create_hub_or_hub_state(self):
        for helpers in ([],['--helpers']):
            with tempfile.TemporaryDirectory() as folder:
                base=Path(folder)
                result=subprocess.run([sys.executable,str(ROOT/'scripts/install.py'),'--bin-dir',str(base/'bin'),*helpers],
                    env=dict(os.environ,HOME=str(base/'home')),capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr);installed=json.loads(result.stdout)
                self.assertNotIn('hub_launcher',installed);self.assertNotIn('hub_skill',installed)
                self.assertFalse((base/'bin/ato-hub').exists());self.assertFalse((base/'home').exists())
                self.assertEqual(len(installed['helpers']),2 if helpers else 0)

    def test_hub_skill_only_is_opt_in_without_hub_launcher(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            result=subprocess.run([sys.executable,str(ROOT/'scripts/install.py'),'--bin-dir',str(base/'bin'),
                '--hub-skills-dir',str(base/'skills')],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr);self.assertTrue(Path(json.loads(result.stdout)['hub_skill']).is_file())
            self.assertFalse((base/'bin/ato-hub').exists())

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
