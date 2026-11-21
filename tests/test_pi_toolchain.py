"""Tool trust, private native launch and actual OS policy; synthetic Pi only."""
import ast
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agents
import pi_session_profile
import pi_toolchain as policy
from pi_toolchain_support import tools_fixture

ROOT = Path(__file__).resolve().parents[1]


class ToolchainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base/'project'; self.project.mkdir()
        self.runtime = self.base/'runtime'; self.runtime.mkdir(mode=0o700)
        self.tools, self.data = tools_fixture(self.base)
        self.environment = dict(CTASK_NODE_BIN=str(self.tools.node), CTASK_PI_BIN=str(self.tools.pi))
        # This development checkout's volume has a group-writable ancestor.
        # Exercise resolution from a protected installation, without changing
        # the host mount's permissions or weakening executable trust checks.
        host=self.base/'owned installation';host.mkdir()
        for name in ('pi_toolchain.py','pi_acp_compat.py','pi_acp_exec.py','pi_coordinator_exec.py',
                     'pi-path-guard.mjs','pi-coordinator-guard.mjs'):
            shutil.copyfile(ROOT/name,host/name)
        source=host/'integrations/pi-acp/node_modules/pi-acp/dist/index.js'
        source.parent.mkdir(parents=True)
        shutil.copyfile(policy.SOURCE,source)
        manifest=host/'integrations/pi-acp/compatibility.json'
        manifest.write_text(json.dumps(self.data))
        paths=patch.multiple(policy,ROOT=host,REGISTRY=manifest,SOURCE=source)
        paths.start();self.addCleanup(paths.stop)
        self.registry = patch('pi_toolchain.registry', return_value=self.data)
        self.registry.start(); self.addCleanup(self.registry.stop)

    def resolve(self, **kwargs):
        return policy.prepare_toolchain(self.project, self.runtime,
            environment=kwargs.pop('environment', self.environment), **kwargs)

    def test_custom_paths_actual_version_probes_use_same_node_and_no_native_session(self):
        result = self.resolve()
        self.assertEqual(result, self.tools)
        self.assertFalse((self.runtime/'native-argv.json').exists())
        seen=[]
        def probe(command, *, home):
            self.assertTrue(home.is_dir())
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            seen.append(command)
            return 'v25.1.2' if len(command)==2 else '1.5.2'
        self.assertEqual(self.resolve(probe=probe).node_version, 'v25.1.2')
        self.assertEqual(seen[0], [str(self.tools.node), '--version'])
        self.assertEqual(seen[1][:2], [str(self.tools.node), str(self.tools.pi)])
        self.assertIn('--offline', seen[1])

    def test_invalid_explicit_never_falls_back_to_good_path(self):
        for candidate in ('', 'node', str(self.base/'missing')):
            with self.subTest(candidate=candidate), self.assertRaises((ValueError,OSError)):
                self.resolve(environment=dict(self.environment,CTASK_NODE_BIN=candidate,
                    PATH=str(self.tools.node.parent)))

    def test_path_search_skips_empty_relative_workspace_runtime_and_symlink_entries(self):
        alias=self.base/'alias'; alias.symlink_to(self.project, target_is_directory=True)
        for root in (self.project,self.runtime):
            shutil.copyfile(self.tools.node,root/'node'); (root/'node').chmod(0o700)
        env=dict(CTASK_PI_BIN=str(self.tools.pi),PATH=os.pathsep.join(
            ['', '.', str(self.project),str(alias),str(self.runtime),str(self.tools.node.parent)]))
        self.assertEqual(self.resolve(environment=env).node,self.tools.node)
        for target in (self.project/'node',self.runtime/'node'):
            link=self.base/'explicit-link';link.symlink_to(target)
            with self.assertRaisesRegex(ValueError,'workspace or runtime'):
                self.resolve(environment=dict(self.environment,CTASK_NODE_BIN=str(link)))
            link.unlink()

    def test_non_executable_and_writable_by_others_are_rejected_before_probe(self):
        for mode in (0o600,0o722):
            self.tools.node.chmod(mode)
            with patch('pi_toolchain._probe') as probe, self.assertRaises(ValueError):self.resolve()
            probe.assert_not_called()
        self.tools.node.chmod(0o700)
        self.tools.node.parent.chmod(0o777)
        with patch('pi_toolchain._probe') as probe, self.assertRaises(ValueError):self.resolve()
        probe.assert_not_called()
        self.tools.node.parent.chmod(0o700)

    def test_selected_old_malformed_or_prerelease_node_does_not_search_again(self):
        for version in ('v23.9.0','24.0.0','v24.0.0-rc.1','v024.0.0','v24.00.0','v24.0.00','v24.0.0\ncredential'):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError,'Node >= 24'):
                self.resolve(probe=lambda *args,**kw:version)

    def test_unapproved_pi_version_or_modified_package_is_not_executed(self):
        for mutate in ('entry','extra','version'):
            with self.subTest(mutate=mutate):
                original=self.tools.pi.read_text()
                package=self.tools.package_root/'package.json'; saved=package.read_text()
                if mutate=='entry':self.tools.pi.write_text(original+'\n# changed\n')
                if mutate=='extra':(self.tools.package_root/'extra.js').write_text('changed')
                if mutate=='version':package.write_text(json.dumps(dict(name='synthetic-pi',version='9.0.0')))
                with patch('pi_toolchain._probe') as probe, self.assertRaises(ValueError):self.resolve()
                probe.assert_not_called()
                self.tools.pi.write_text(original);package.write_text(saved)
                (self.tools.package_root/'extra.js').unlink(missing_ok=True)

    def test_package_helpers_reject_special_files_links_writable_and_unowned_resources(self):
        from types import SimpleNamespace
        path = self.tools.package_root/'resource'
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, 'invalid toolchain file'):
            policy.package_sha256(self.tools.package_root)
        path.unlink(); path.symlink_to(self.tools.pi)
        with self.assertRaisesRegex(ValueError, 'unsafe Pi package contents'):
            policy.package_sha256(self.tools.package_root)
        path.unlink(); path.write_text('synthetic resource'); path.chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'unsafe Pi package contents'):
            policy.package_sha256(self.tools.package_root)
        path.chmod(0o600)
        original = Path.stat
        def resource_owner(candidate, *args, **kwargs):
            info = original(candidate, *args, **kwargs)
            if candidate == path:
                return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid()+10000,
                    st_size=info.st_size)
            return info
        with patch.object(Path, 'stat', resource_owner), self.assertRaisesRegex(ValueError, 'unsafe Pi package contents'):
            policy.package_sha256(self.tools.package_root)

    def test_pi_version_probe_is_consistency_check_and_output_is_not_disclosed(self):
        secret='synthetic probe payload'
        def probe(command,**kw):return 'v24.0.0' if len(command)==2 else secret
        with self.assertRaises(ValueError) as error:self.resolve(probe=probe)
        self.assertNotIn(secret,str(error.exception))

    def test_probe_timeout_and_byte_bound_and_nonzero_are_safe(self):
        for body in ('import time;time.sleep(3)', 'print("x"*100000)',
                     'import sys;print("synthetic secret",file=sys.stderr);sys.exit(1)'):
            self.tools.node.write_text('#!'+sys.executable+'\n'+body+'\n')
            with self.subTest(body=body), patch('pi_toolchain.PROBE_SECONDS',.25), self.assertRaises(ValueError) as err:
                self.resolve()
            self.assertNotIn('synthetic secret',str(err.exception))

    def test_probe_environment_scrubs_loaders_and_ambient_path(self):
        marker=self.base/'probe-environment.json'
        self.tools.node.write_text('#!'+sys.executable+'\nimport json,os\n'
            +f'open({str(marker)!r},"w").write(json.dumps(dict(os.environ)))\nprint("v24.0.0")\n')
        with patch.dict(os.environ,NODE_OPTIONS='synthetic-bad',NODE_PATH='synthetic-bad',
                LD_PRELOAD='synthetic-bad',DYLD_INSERT_LIBRARIES='synthetic-bad'):
            self.assertEqual(policy._probe([str(self.tools.node),'--version'],home=self.runtime),'v24.0.0')
        env=json.loads(marker.read_text())
        self.assertEqual(env['PATH'],'/usr/bin:/bin')
        for key in ('NODE_OPTIONS','NODE_PATH','LD_PRELOAD','DYLD_INSERT_LIBRARIES'):
            self.assertNotIn(key,env)

    def native_host(self,role):
        # A disposable code-owned installation has a synthetic registry, selected
        # by its fixed source location. No production environment override exists.
        host=self.base/('installation-'+role);host.mkdir()
        for name in ('pi_toolchain.py','pi_acp_compat.py','pi_acp_exec.py','pi_coordinator_exec.py',
                     'pi-path-guard.mjs','pi-coordinator-guard.mjs'):
            shutil.copyfile(ROOT/name,host/name)
        source=host/'integrations/pi-acp/node_modules/pi-acp/dist/index.js'
        source.parent.mkdir(parents=True);source.write_text('synthetic original adapter')
        registry=host/'integrations/pi-acp/compatibility.json'
        registry.write_text(json.dumps(self.data))
        adapter=self.runtime/'adapter';adapter.mkdir(mode=0o700)
        # Keep the exact current derived adapter bytes and registered role hash.
        from scripts.derive_pi_acp import derive
        raw=policy.SOURCE.read_text()
        text=derive(raw) if role=='worker' else pi_session_profile.derive_coordinator(raw)
        (adapter/'pi-acp.mjs').write_text(text)
        with patch.multiple(policy,ROOT=host,REGISTRY=registry,SOURCE=source):
            launcher=policy.write_launch(self.runtime,self.project,role,self.tools)
        return launcher

    def test_actual_native_launchers_use_verified_pair_and_scrub_environment(self):
        for role in ('worker','coordinator'):
            with self.subTest(role=role):
                launcher=self.native_host(role)
                env=dict(os.environ,CTASK_AGENT_REPO=str(self.project),CTASK_PI_RUNTIME=str(self.runtime),
                    CTASK_AGENT_SANDBOX='read-only',CTASK_PI_MODEL='synthetic/model',
                    NODE_OPTIONS='poison',NODE_PATH='poison',DYLD_LIBRARY_PATH='poison',LD_PRELOAD='poison',
                    PATH=str(self.project),UNRELATED_SYNTHETIC_SECRET='do not inherit')
                result=subprocess.run([str(launcher),'--mode','rpc','--no-themes'],env=env,
                    capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                record=json.loads((self.runtime/'native-argv.json').read_text())
                self.assertEqual(record['argv'][0],str(self.tools.pi))
                self.assertIn('--no-extensions',record['argv'])
                self.assertEqual(record['environment']['PATH'],'/usr/bin:/bin')
                for key in ('NODE_OPTIONS','NODE_PATH','DYLD_LIBRARY_PATH','LD_PRELOAD','UNRELATED_SYNTHETIC_SECRET'):
                    self.assertNotIn(key,record['environment'])
                self.assertEqual((self.runtime/'trusted/toolchain.json').stat().st_mode&0o777,0o600)
                shutil.rmtree(self.runtime/'trusted');shutil.rmtree(self.runtime/'adapter')

    def test_native_launch_rejects_tampered_tools_description_role_and_rpc_arguments(self):
        launcher=self.native_host('worker')
        env=dict(os.environ,CTASK_AGENT_REPO=str(self.project),CTASK_PI_RUNTIME=str(self.runtime),
                 CTASK_AGENT_SANDBOX='read-only')
        for args in (['--shell'],['--mode','rpc','--no-themes','--session',str(self.tools.pi)]):
            result=subprocess.run([str(launcher),*args],env=env,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
        original=self.tools.node.read_text();self.tools.node.write_text(original+'\n# changed\n')
        result=subprocess.run([str(launcher),'--mode','rpc','--no-themes'],env=env,
            capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.runtime/'native-argv.json').exists())
        self.tools.node.write_text(original)
        path=self.runtime/'trusted/toolchain.json';record=json.loads(path.read_text());record['role']='coordinator'
        path.write_text(json.dumps(record))
        result=subprocess.run([str(launcher),'--mode','rpc','--no-themes'],env=env,
            capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('Traceback',result.stderr)

    def test_os_sandbox_prevents_material_writes_aliases_rename_and_runtime_replacement(self):
        config=dict(providers={'fixture':dict(baseUrl='http://127.0.0.1:43210/v1',
            apiKey='ato-local-dummy',api='openai-completions',models=[dict(id='scripted')])})
        profile=pi_session_profile.LocalPiProfile(config,'fixture/scripted')
        with patch('pi_session_profile.prepare_toolchain',return_value=self.tools):
            profile.launch(self.runtime,self.project,'workspace-write')
        probe='''import json,sys\nfrom pathlib import Path
r=Path(sys.argv[1]);p=Path(sys.argv[2])
for folder in ('trusted','adapter'):
 for path in (r/folder/'new-file',next(x for x in (r/folder).iterdir() if x.is_file())):
  try:path.write_text('forbidden')
  except PermissionError:pass
  else:raise AssertionError('material write allowed')
 try:(r/folder).rename(r/(folder+'-moved'))
 except PermissionError:pass
 else:raise AssertionError('protected directory rename allowed')
alias=r/'alias';alias.symlink_to(r/'trusted',target_is_directory=True)
try:(alias/'new-file').write_text('forbidden')
except PermissionError:pass
else:raise AssertionError('alias write allowed')
alias.unlink()
try:r.rename(p/'runtime-moved')
except PermissionError:pass
else:raise AssertionError('runtime replacement allowed')
(r/'scratch').write_text('allowed');(p/'safe').write_text('allowed')
'''
        for sandbox in (agents.sandbox_profile(self.project,self.runtime,'workspace-write'),
                        (self.runtime/'sandbox.sb').read_text()):
            with self.subTest(sandbox=sandbox[:30]):
                for name in ('trusted','adapter'):
                    self.assertIn('(require-not (subpath '+json.dumps(str(self.runtime/name))+'))',sandbox)
                result=subprocess.run(['/usr/bin/sandbox-exec','-p',sandbox,sys.executable,'-c',probe,
                    str(self.runtime),str(self.project)],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)

    def test_module_is_stdlib_only_and_import_has_no_io_or_process_bootstrap(self):
        tree=ast.parse((ROOT/'pi_toolchain.py').read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.Import): names=[a.name for a in node.names]
            elif isinstance(node,ast.ImportFrom):names=[node.module]
            else:continue
            self.assertTrue(all(n.split('.')[0] in sys.stdlib_module_names for n in names))
        command='import sys;sys.path.insert(0,'+repr(str(ROOT))+');import pi_toolchain;assert "agents" not in sys.modules'
        home=self.base/'empty-home'
        result=subprocess.run([sys.executable,'-I','-c',command],cwd=self.base,
            env=dict(HOME=str(home),ATO_HOME=str(home),PATH=''),capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(home.exists())
