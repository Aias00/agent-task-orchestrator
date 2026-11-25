import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import hub_contract as contract
import hub_ledger as ledger
import hub_sandbox as sandbox
import hub_worktree as worktree
from ato_execution_host import CommandHost, HostProfile
from pi_toolchain import locate
from hub_support import git,repository,runtime,spec,verdict


class HubFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ato-h1-foundation-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.repo=repository(self.root)
        context=patch.dict(os.environ,dict(ATO_HOME=str(self.root/'ato'),CODEX_HOME=str(self.root/'native')))
        context.start();self.addCleanup(context.stop)

    def test_O1_strict_JSON_verdict_and_blocker_consistency(self):
        self.assertEqual(contract.validate_review_verdict(verdict()),verdict())
        for value in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}','{"x":1} trailing','{'):
            with self.subTest(value=value),self.assertRaises(contract.HubError):contract.strict_json(value)
        for value in (verdict('approve','blocker'),verdict('request_changes','minor'),dict(verdict(),unknown=True)):
            with self.assertRaises(contract.HubError):contract.validate_review_verdict(value)
        a=verdict('request_changes','blocker');b=copy.deepcopy(a);b['findings'][0]['title']='  FIX THE   fixture '
        self.assertEqual(contract.blocker_fingerprint('a'*40,a),contract.blocker_fingerprint('a'*40,b))
        for path in ('../escape','/absolute','x/../y','-flag'):
            value=copy.deepcopy(a);value['findings'][0]['file']=path
            with self.assertRaises(contract.HubError):contract.validate_review_verdict(value)

    def test_hub_list_empty_is_read_only_and_rejects_indirect_or_nonprivate_roots(self):
        self.assertEqual(ledger.HubLedger.list_hubs(),[])
        self.assertFalse((self.root/'ato').exists())
        base=self.root/'ato';base.mkdir(mode=0o700)
        target=self.root/'outside';target.mkdir(mode=0o700)
        hubs = base/'hubs'
        hubs.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):ledger.HubLedger.list_hubs()
        hubs.unlink()
        hubs.mkdir(mode=0o700)
        hubs.chmod(0o755)  # mkdir applies the process umask.
        with self.assertRaises(ValueError):ledger.HubLedger.list_hubs()

    def test_hub_list_sorted_private_summaries_filter_busy_and_never_create_lock(self):
        books=[ledger.HubLedger.create(self.repo,contract.HubAgentSpec(kind)) for kind in ('agy','codex')]
        second=self.root/'project-more';second.mkdir();other=repository(second)
        books.append(ledger.HubLedger.create(other,contract.HubAgentSpec('claude')))
        books[0].create_task(spec(self.repo))
        secret='synthetic-list-secret-must-not-escape'
        row=books[0].hub();row['api_key']=secret;row['active_agent']['api_key']=secret
        row['native_sessions']['agy']=dict(session_id=secret,state='active',transcript=secret)
        ledger.save(books[0].root/'hub.json',row)
        for book in books:(book.root/'.hub.lock').unlink()
        def snapshot():
            return {str(p): (p.stat().st_mode,p.stat().st_mtime_ns,p.read_bytes() if p.is_file() else None)
                    for p in (self.root/'ato').rglob('*')}
        before=snapshot();result=ledger.HubLedger.list_hubs()
        self.assertEqual(before,snapshot())
        self.assertEqual([r['hub_id'] for r in result],sorted(b.identifier for b in books))
        self.assertNotIn(secret,json.dumps(result))
        filtered=ledger.HubLedger.list_hubs(self.repo)
        self.assertEqual({r['hub_id'] for r in filtered},{b.identifier for b in books[:2]})
        self.assertEqual(set(filtered[0]),{'hub_id','repo','active_agent','hub_busy','active_session_state','task_count'})
        first=next(r for r in filtered if r['hub_id']==books[0].identifier)
        self.assertEqual((first['task_count'],first['active_session_state']),(1,'active'))
        with books[0].locked():
            self.assertTrue(next(r for r in ledger.HubLedger.list_hubs() if r['hub_id']==books[0].identifier)['hub_busy'])
        alias=self.root/'alias';alias.symlink_to(self.repo,target_is_directory=True)
        with self.assertRaises(ValueError):ledger.HubLedger.list_hubs(alias)
        link=self.root/'ato/hubs/hub-link';link.symlink_to(books[0].root,target_is_directory=True)
        with self.assertRaises(ValueError):ledger.HubLedger.list_hubs()

    def test_multilanguage_shared_dependencies_ignore_tracking_tripwire_and_cleanup(self):
        self.assertEqual(worktree.SHARED,contract.SHARED_ENV_LINKS)
        (self.repo/'.gitignore').write_text('.codex/\nnode_modules/\nvenv/\n')
        git(self.repo,'add','.gitignore');git(self.repo,'commit','-qm','ignore development dependencies')
        for name in ('node_modules','venv'):
            dependency=self.repo/name;dependency.mkdir();(dependency/'marker').write_text('dependency')
        value=spec(self.repo);value['worktree_policy']['shared_env_links']=['node_modules','venv']
        contract.validate_task_spec(value)
        binding=worktree.provision(self.repo,'poly','feat/poly','main',shared_env_links=['node_modules','venv'])
        path=Path(binding['path']);before=worktree.snapshot_integrity(path)
        for name in ('node_modules','venv'):
            self.assertTrue((path/name).is_symlink());(self.repo/name/'marker').write_text('updated dependency')
        self.assertEqual(before,worktree.snapshot_integrity(path))
        link=path/'venv';original=os.readlink(link);link.unlink();link.symlink_to(self.repo/'venv',target_is_directory=True)
        self.assertNotEqual(before,worktree.snapshot_integrity(path))
        with self.assertRaisesRegex(ValueError,'SHARED_DEPENDENCY_CHANGED'):worktree.cleanup(binding)
        link.unlink();link.symlink_to(original,target_is_directory=True)
        worktree.cleanup(binding);self.assertFalse(path.exists())
        self.assertEqual((self.repo/'venv/marker').read_text(),'updated dependency')
        for name in ('node_modules','venv'):
            git(self.repo,'add','-f',name);git(self.repo,'commit','-qm','vendored '+name)
            old=worktree.snapshot_integrity(self.repo);(self.repo/name/'marker').write_text('vendored change')
            self.assertNotEqual(old,worktree.snapshot_integrity(self.repo))
            with self.assertRaisesRegex(ValueError,'SHARED_DEPENDENCY_NOT_IGNORED'):
                worktree.provision(self.repo,'tracked-'+name,'feat/tracked-'+name,'main',shared_env_links=[name])

    def test_nested_shared_dependency_is_removed_and_restored_on_git_failure(self):
        names = ['venv', 'integrations/pi-acp/node_modules']
        (self.repo/'.gitignore').write_text('.codex/\nvenv/\nnode_modules/\n')
        git(self.repo, 'add', '.gitignore')
        git(self.repo, 'commit', '-qm', 'ignore fixture dependencies')
        for name in names:
            dependency = self.repo/name
            dependency.mkdir(parents=True)
            (dependency/'marker').write_text(name)
        binding = worktree.provision(self.repo, 'nested', 'feat/nested', 'main', shared_env_links=names)
        path = Path(binding['path'])
        before = worktree.snapshot_integrity(path)
        run_git = worktree.git

        def refuse_remove(repo, *args):
            if args[:2] == ('worktree', 'remove'):
                self.assertTrue(all(not os.path.lexists(path/name) for name in names))
                raise ValueError('GIT_OPERATION_REFUSED')
            return run_git(repo, *args)

        with patch.object(worktree, 'git', side_effect=refuse_remove):
            with self.assertRaisesRegex(ValueError, 'GIT_OPERATION_REFUSED'):
                worktree.cleanup(binding)
        self.assertEqual(worktree.snapshot_integrity(path), before)
        for name in names:
            self.assertEqual((path/name).resolve(), self.repo/name)
        worktree.cleanup(binding)
        self.assertFalse(path.exists())
        for name in names:
            self.assertEqual((self.repo/name/'marker').read_text(), name)

    def test_multilanguage_nonignored_or_symlink_dependency_is_refused(self):
        for name in ('node_modules','venv'):
            path=self.repo/name;path.mkdir()
            with self.assertRaisesRegex(ValueError,'SHARED_DEPENDENCY_NOT_IGNORED'):
                worktree.provision(self.repo,name,'feat/'+name,'main',shared_env_links=[name])
            path.rmdir();target=self.root/name;target.mkdir();path.symlink_to(target,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'SHARED_DEPENDENCY_NOT_IGNORED'):
                worktree.provision(self.repo,name,'feat/'+name,'main',shared_env_links=[name])

    def test_multilanguage_dependency_cannot_overwrite_an_older_base_tree_target(self):
        (self.repo/'.gitignore').write_text('.codex/\nnode_modules/\nvenv/\n')
        git(self.repo,'add','.gitignore');git(self.repo,'commit','-qm','ignored development roots')
        for name in ('node_modules','venv'):
            source=self.repo/name;source.mkdir();(source/'file').write_text('original dependency')
            git(self.repo,'add','-f',name);git(self.repo,'commit','-qm','old tracked dependency')
            old=git(self.repo,'rev-parse','HEAD')
            git(self.repo,'rm','-r','--cached',name);git(self.repo,'commit','-qm','untrack dependency')
            with self.assertRaisesRegex(ValueError,'SHARED_DEPENDENCY_COLLISION'):
                worktree.provision(self.repo,'collision-'+name,'feat/collision-'+name,old,shared_env_links=[name])
            target=self.root/('project-hub-collision-'+name)/name
            self.assertFalse(target.is_symlink());self.assertEqual((target/'file').read_text(),'original dependency')

    def test_O1_spec_manual_gate_and_no_flag_injection(self):
        value=spec(self.repo);self.assertIs(contract.validate_task_spec(value),value)
        for change in (dict(schema_version=True),dict(unknown=True),dict(branch='main')):
            with self.assertRaises(contract.HubError):contract.validate_task_spec(dict(value,**change))
        value['worker_contract']['acceptance']=value['worker_contract']['acceptance'][:1]
        with self.assertRaisesRegex(ValueError,'FINAL_MANUAL'):contract.validate_task_spec(value)
        for kwargs in (dict(kind='agy',model='--model'),dict(kind='other'),dict(kind='agy',effort='--flag')):
            with self.assertRaises(ValueError):contract.HubAgentSpec(**kwargs)

    def test_task_id_matches_worktree_identifier_before_provision(self):
        for value in ('a','A0_-','x'*128):
            with self.subTest(value=value):
                self.assertEqual(contract.validate_task_spec(spec(self.repo,task=value))['task_id'],value)
        for value in ('task.one','.', '..','a/b','-start','_start','x'*129,'',None,True):
            with self.subTest(value=value),self.assertRaises(contract.HubError):
                contract.validate_task_spec(dict(spec(self.repo),task_id=value))
        self.assertEqual(contract.token('finding.1'),'finding.1')

    def test_O4_provision_fixed_commit_dependency_and_collision_refusals(self):
        (self.repo/'.venv').mkdir();(self.repo/'.venv/marker').write_text('synthetic dependency')
        base=git(self.repo,'rev-parse','HEAD')
        binding=worktree.provision(self.repo,'one','feat/one','main',shared_env_links=['.venv'])
        path=Path(binding['path'])
        self.assertEqual(git(path,'rev-parse','HEAD'),base);self.assertEqual(binding['base_commit'],base)
        self.assertTrue((path/'.venv').is_symlink());self.assertEqual((path/'.venv/marker').read_text(),'synthetic dependency')
        for task,branch in (('one','feat/two'),('two','feat/one')):
            with self.assertRaises(ValueError):worktree.provision(self.repo,task,branch,'main')
        for links in (['result.txt'],['.venv','.venv'],['integrations/pi-acp/node_modules']):
            with self.assertRaises(ValueError):worktree.provision(self.repo,'bad','feat/bad','main',shared_env_links=links)
        worktree.cleanup(binding);self.assertFalse(path.exists())
        self.assertEqual(git(self.repo,'rev-parse','refs/heads/feat/one'),base)

    def test_O4_tracked_dependency_refuses_before_creation(self):
        (self.repo/'.venv').mkdir();(self.repo/'.venv/file').write_text('not a shared dependency')
        git(self.repo,'add','-f','.venv/file');git(self.repo,'commit','-qm','tracked dependency')
        with self.assertRaises(ValueError):worktree.provision(self.repo,'bad','feat/bad','main',shared_env_links=['.venv'])
        self.assertFalse((self.root/'project-hub-bad').exists())

    def test_O3_tripwire_dirty_content_new_file_and_HEAD(self):
        before=worktree.snapshot_integrity(self.repo)
        (self.repo/'result.txt').write_text('changed')
        first=worktree.snapshot_integrity(self.repo);(self.repo/'result.txt').write_text('changed twice')
        second=worktree.snapshot_integrity(self.repo)
        self.assertEqual(first['status_porcelain'],second['status_porcelain'])
        for value in (first,second):
            with self.assertRaisesRegex(ValueError,'REVIEWER_MUTATION'):worktree.assert_integrity(before,value)
        git(self.repo,'checkout','--','result.txt');(self.repo/'new.txt').touch()
        with self.assertRaises(ValueError):worktree.assert_integrity(before,worktree.snapshot_integrity(self.repo))
        (self.repo/'new.txt').unlink();git(self.repo,'commit','--allow-empty','-qm','head moved')
        with self.assertRaises(ValueError):worktree.assert_integrity(before,worktree.snapshot_integrity(self.repo))

    def test_O4_cleanup_refuses_dirty_locked_and_mismatched_bindings(self):
        binding=worktree.provision(self.repo,'one','feat/one','main');path=Path(binding['path'])
        (path/'new').touch()
        with self.assertRaisesRegex(ValueError,'DIRTY'):worktree.cleanup(binding)
        (path/'new').unlink();lock=path/'.codex/tasks/.worker.lock';lock.parent.mkdir(parents=True)
        with lock.open('w') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError,'BUSY'):worktree.cleanup(binding)
        with self.assertRaises(ValueError):worktree.cleanup(dict(binding,branch='wrong'))
        worktree.cleanup(binding)

    def test_O3_real_macos_OS_denies_all_authority_writes_and_allows_scratch(self):
        binding=worktree.provision(self.repo,'one','feat/one','main');path=Path(binding['path']);rt=runtime(self.root)
        alias=rt/'tmp/alias';alias.symlink_to(self.repo/'result.txt')
        targets=[path/'probe',self.repo/'probe',self.repo/'.git/probe',rt/'trusted/probe',alias]
        code="import json,pathlib,sys\nr=[]\nfor p in sys.argv[1:-1]:\n try:pathlib.Path(p).write_text('bad');r.append(False)\n except PermissionError:r.append(True)\npathlib.Path(sys.argv[-1]).write_text('allowed')\nprint(json.dumps(r))\n"
        profile=sandbox.readonly_profile(rt,protected=(self.repo,path))
        result=sandbox.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-c',code,*map(str,targets),str(rt/'tmp/allowed')],
            workspace=path,runtime=rt,environment=sandbox.clean_environment(rt/'home',rt/'tmp'),timeout=5)
        self.assertEqual(result.code,'OK',result.stderr);self.assertEqual(json.loads(result.stdout),[True]*5)
        self.assertTrue(all(not p.exists() for p in targets[:-1]));self.assertEqual((self.repo/'result.txt').read_text(),'initial')
        self.assertEqual((rt/'tmp/allowed').read_text(),'allowed');self.assertEqual((rt/'stderr.log').stat().st_mode&0o777,0o600)

    def test_O3_stop_and_TERM_ignoring_real_owned_process_are_bounded(self):
        rt=runtime(self.root);started=time.monotonic()
        code='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'
        result=sandbox.run([sys.executable,'-c',code],workspace=self.repo,runtime=rt,
            environment=sandbox.clean_environment(rt/'home',rt/'tmp'),timeout=10,stop=lambda:time.monotonic()-started>.4)
        self.assertEqual(result.code,'STOPPED');self.assertTrue(result.cleaned)
        self.assertLess(time.monotonic()-started,5);self.assertEqual(result.returncode,-signal.SIGKILL)

    def test_batch2_own_group_terminates_orphan_after_unreaped_leader_exits(self):
        leader = subprocess.Popen(["/bin/sh", "-c", "sleep .25; sleep 30 </dev/null >/dev/null 2>&1 &"],
                                  start_new_session=True)
        self.addCleanup(leader.wait, 5)
        birth = sandbox.identity(leader.pid)
        self.assertIsNotNone(birth)
        group = sandbox.OwnedGroup(leader, birth, {leader.pid: birth})
        self.addCleanup(group.close)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            rows = sandbox.group_members(leader.pid)
            if rows and leader.pid not in rows:
                break
            time.sleep(.03)
        self.assertTrue(rows and leader.pid not in rows)
        self.assertIsNone(leader.returncode)
        self.assertTrue(group.close())
        self.assertEqual(sandbox.group_members(leader.pid), {})
        self.assertEqual(leader.wait(timeout=3), 0)

    def test_fixup_unreaped_child_without_a_birth_can_reap_its_group(self):
        leader = subprocess.Popen(["/bin/sh", "-c", "sleep 30 </dev/null >/dev/null 2>&1 &"],
                                  start_new_session=True)
        self.addCleanup(leader.wait, 5)
        group = sandbox.OwnedGroup(leader, None)
        self.addCleanup(group.close)
        end = time.monotonic() + 3
        rows = {}
        while time.monotonic() < end:
            rows = sandbox.group_members(leader.pid)
            if rows and leader.pid not in rows:
                break
            time.sleep(.03)
        self.assertTrue(rows and leader.pid not in rows)
        self.assertIsNone(leader.returncode)
        self.assertTrue(group.close())
        self.assertEqual(sandbox.group_members(leader.pid), {})

    def test_batch2_review_diff_refuses_a_fifo_without_blocking(self):
        os.mkfifo(self.repo / "pipe")
        binding = {"path": str(self.repo), "base_commit": git(self.repo, "rev-parse", "HEAD").strip()}
        with self.assertRaisesRegex(ValueError, "UNSAFE_REVIEW_FILE"):
            worktree.diff_bundle(binding)

    def test_fixup_real_node_check_can_use_loopback_but_cannot_read_credentials_or_reach_external_ip(self):
        node = locate("node")
        home = self.root / "home"
        home.mkdir(mode=0o700)
        secret = home / ".ssh" / "fixture"
        secret.parent.mkdir(mode=0o700)
        secret.write_text("synthetic-secret")
        gh = home / ".config/gh/hosts.yml"
        gh.parent.mkdir(parents=True, mode=0o700)
        gh.write_text("synthetic-token")
        config = home / ".config/application.json"
        config.write_text("{}")
        store_home = self.root / "private-store"
        store_home.mkdir(mode=0o700)
        rt = store_home / "turn"
        rt.mkdir(mode=0o700)
        tmp = rt / "tmp"
        tmp.mkdir(mode=0o700)
        proof = self.root / "proof"
        proof.mkdir(mode=0o700)
        (rt / "schema.json").write_text("{}")
        worker_home = self.root / "worker-home"
        worker_home.mkdir(mode=0o700)
        profile = HostProfile(str(node), str(worker_home), proof_roots=(str(proof),))
        store = SimpleNamespace(home=store_home, native_homes=())
        driver = SimpleNamespace(store=store, task={"worktree": dict(
            repo=str(self.repo), shared_link_identities={})})
        host = CommandHost(driver, profile, store_home)
        js = """
const fs = require('node:fs'), net = require('node:net');
const [secret, credential, config, schema, port] = process.argv.slice(1);
const can = (fn) => { try { fn(); return true; } catch (_) { return false; } };
const observed = {
    tmp: fs.realpathSync(process.env.TMPDIR),
    secret: can(() => fs.readFileSync(secret)),
    credential: can(() => fs.readFileSync(credential)),
    config: can(() => fs.readFileSync(config)),
    schema: can(() => fs.writeFileSync(schema, 'tampered')),
    scratch: can(() => fs.writeFileSync(process.env.TMPDIR + '/writable', 'ok'))
};
const connect = (host, port) => new Promise((resolve) => {
    const client = net.connect(port, host);
    client.once('connect', () => { client.end(); resolve('connected'); });
    client.once('error', (err) => resolve(err.code));
    client.setTimeout(1000, () => { client.destroy(); resolve('timeout'); });
});
const server = net.createServer((client) => client.end());
server.on('error', (err) => console.log(JSON.stringify({...observed, bind: err.code})));
server.listen(0, '127.0.0.1', async () => {
    observed.inbound = await connect('127.0.0.1', server.address().port);
    observed.loopback = await connect('127.0.0.1', Number(port));
    observed.external = await connect('192.0.2.1', 443);
    server.close();
    console.log(JSON.stringify(observed));
});
"""
        with self.enterContext(socket.socket()) as listener, patch.dict(os.environ, HOME=str(home)):
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            args = [str(node), "-e", js, str(secret), str(gh), str(config),
                    str(rt / "schema.json"), str(listener.getsockname()[1])]
            result = sandbox.run(host._fence(args, rt, self.repo, write=False), workspace=self.repo, runtime=rt,
                environment=sandbox.clean_environment(tmp, tmp), timeout=8)
        self.assertEqual(result.code, "OK", result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual(facts["tmp"], str(tmp))
        self.assertTrue(facts["scratch"])
        self.assertFalse(facts["secret"] or facts["credential"] or facts["schema"])
        self.assertTrue(facts["config"])
        self.assertEqual((facts["inbound"], facts["loopback"]), ("connected", "connected"))
        self.assertEqual(facts["external"], "EPERM")

    def test_O7_private_ledger_switch_rollover_and_immutable_spec(self):
        book=ledger.HubLedger.create(self.repo,contract.HubAgentSpec('agy'),max_turns=1)
        with book.locked():
            path=book.create_task(spec(self.repo));book.record_session('agy-one','fresh_only')
            caps=contract.HubCapabilities({name:dict(state='supported',evidence='synthetic') for name in contract.CAPABILITIES})
            sid,mode,provenance=book.session(caps,'resume_if_supported')
            self.assertIsNone(sid);self.assertEqual(provenance,'ledger_rollover')
            book.record_session('agy-two',provenance);book.switch_agent(contract.HubAgentSpec('claude'),'fixture switch')
            self.assertIsNone(book.session(caps,'resume_if_supported')[0]);self.assertIn('agy',book.hub()['native_sessions'])
            book.record_session('claude-one','ledger_reconstruction_from_agy')
            book.switch_agent(contract.HubAgentSpec('codex'),'fixture switch')
            self.assertIsNone(book.session(caps,'resume_if_supported')[0])
            self.assertIn('Synthetic offline change',book.brief());self.assertTrue(book.busy())
        self.assertFalse(book.busy())
        for file in book.root.rglob('*'):
            self.assertEqual(file.stat().st_mode&0o777,0o700 if file.is_dir() else 0o600)
        value=json.loads((path/'spec.json').read_text());value['branch']='tampered';(path/'spec.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'FROZEN_SPEC'):book.read_task('task-one')

    def test_O7_unknown_resume_falls_back_and_required_resume_refuses(self):
        book=ledger.HubLedger.create(self.repo,contract.HubAgentSpec('claude'))
        book.record_session('claude-one','fresh_only')
        caps=contract.HubCapabilities({name:dict(state='unknown',evidence='not qualified') for name in contract.CAPABILITIES})
        self.assertEqual(book.session(caps,'resume_if_supported')[:2],(None,'fresh_only'))
        with self.assertRaisesRegex(ValueError,'RESUME_NOT_QUALIFIED'):book.session(caps,'require_resume')

    def test_O7_symlink_ledger_and_worktree_paths_never_adopt(self):
        book=ledger.HubLedger.create(self.repo,contract.HubAgentSpec('agy'))
        actual=book.root/'hub.json';backup=book.root/'backup.json';actual.rename(backup);actual.symlink_to(backup)
        with self.assertRaises(ValueError):book.hub()
        alias=self.root/'alias';alias.symlink_to(self.repo,target_is_directory=True)
        with self.assertRaises(ValueError):worktree.repository(alias)

    def test_O7_fresh_sessions_bound_retired_history_and_diff_inline_cap(self):
        book=ledger.HubLedger.create(self.repo,contract.HubAgentSpec('claude'))
        for index in range(260):
            book.record_session(f'claude-{index}','fresh_only')
        self.assertEqual(len(book.hub()['native_sessions']['claude']['retired_sessions']),256)
        binding=worktree.provision(self.repo,'cap','feat/cap','main')
        (Path(binding['path'])/'result.txt').write_text('x'*(260*1024))
        with self.assertRaisesRegex(ValueError,'REVIEW_DIFF_BOUND_EXCEEDED'):
            worktree.diff_bundle(binding)

    def test_O8_loopback_proxy_opt_in_rejects_remote_or_credentialed_urls(self):
        self.assertEqual(sandbox.validate_loopback_proxy('http://127.0.0.1:7890'),'http://127.0.0.1:7890')
        self.assertEqual(sandbox.validate_loopback_proxy('socks5h://localhost:7890'),'socks5h://localhost:7890')
        for bad in ('http://evil.example:8080','http://@127.0.0.1:7890','http://:@127.0.0.1:7890',
                    'http://user@127.0.0.1:7890','http://:pass@127.0.0.1:7890','http://@localhost:7890',
                    'http://@[::1]:7890','http://user:pass@127.0.0.1:7890','http://127.0.0.1:7890/path',
                    'http://127.0.0.1','ftp://127.0.0.1:21','http://127.0.0.1:7890?a=1'):
            with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,'INVALID_LOOPBACK_PROXY'):
                sandbox.validate_loopback_proxy(bad)
        rt=runtime(self.root)
        env=sandbox.clean_environment(rt/'home',rt/'tmp',proxy='http://127.0.0.1:7890')
        self.assertEqual(env['HTTPS_PROXY'],'http://127.0.0.1:7890')
        self.assertNotIn('HTTPS_PROXY',sandbox.clean_environment(rt/'home',rt/'tmp'))
