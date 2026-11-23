"""Private diagnostics from actual local ACP subprocesses; no Pi/provider use."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import acp_bridge
import pi_acp_worker
import runner
import task_trace
from pi_toolchain_support import tools_fixture

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'SYNTHETIC_CREDENTIAL_MUST_REMAIN_PRIVATE_abc123'


class ACPStderrTests(unittest.TestCase):
    def setUp(self):
        logging_context = patch.object(logging.getLogger(), 'handlers', list(logging.getLogger().handlers))
        logging_context.start()
        self.addCleanup(logging_context.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        env = patch.dict(os.environ, ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'native'))
        env.start()
        self.addCleanup(env.stop)
        plan = self.repo/'plan.json'
        runner.save(plan, dict(objective='private diagnostic fixture', memory=False,
            tasks=[dict(id='one', prompt='synthetic only', checks=[[sys.executable, '-c', 'assert True']])]))
        self.directory = runner.create(self.repo, plan)
        self.label = 'one-attempt-1-abcdef'
        self.runtime = self.directory/'checkpoints'/(self.label+'.acp-runtime')
        self.schema = self.directory/'schema.json'
        runner.save(self.schema, runner.SCHEMA)
        self.output = self.directory/'checkpoints'/(self.label+'.json')

    def command(self, payload, protocol=None):
        script = self.root/'diagnostic-agent.py'
        suffix = ('import runpy\nrunpy.run_path('+repr(str(ROOT/'tests/fixtures/acp_fixture_agent.py'))+',run_name="__main__")\n'
                  if protocol is None else protocol)
        script.write_text('import sys\nsys.stderr.buffer.write('+repr(payload)+')\nsys.stderr.buffer.flush()\n'+suffix)
        return [sys.executable, str(script)]

    def invoke(self, command, prompt='fixture success'):
        return asyncio.run(asyncio.wait_for(acp_bridge.invoke(command, self.directory, self.label,
            self.schema, self.output, prompt, stderr_path=self.runtime/'stderr.log'), timeout=10))

    def test_large_stderr_is_capped_private_and_drained_without_blocking_protocol(self):
        self.runtime.mkdir(mode=0o700)
        output, error = io.StringIO(), io.StringIO()
        with redirect_stderr(error), redirect_stdout(output):
            self.invoke(self.command(SECRET.encode()+b'x'*(2*acp_bridge.STDERR_MAX_BYTES)))
        log = self.runtime/'stderr.log'
        self.assertEqual(log.stat().st_size, acp_bridge.STDERR_MAX_BYTES)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.runtime.stat().st_mode & 0o777, 0o700)
        self.assertTrue(log.read_bytes().startswith(SECRET.encode()))
        self.assertTrue(self.output.is_file())
        self.assertNotIn(SECRET, output.getvalue()+error.getvalue()+json.dumps(task_trace.query(self.directory)))
        self.assertNotIn(SECRET.encode(), (self.directory/'trace.sqlite3').read_bytes())
        self.invoke(self.command(b'second invocation only'))
        self.assertEqual(log.read_bytes(), b'second invocation only')

    def test_failed_native_outcome_retains_private_diagnostic_without_parent_leak(self):
        self.runtime.mkdir(mode=0o700)
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaisesRegex(ValueError, 'outcome failed'):
            self.invoke(self.command(SECRET.encode()), 'fixture-native-error')
        self.assertEqual((self.runtime/'stderr.log').read_text(), SECRET)
        self.assertNotIn(SECRET, error.getvalue()+json.dumps(task_trace.query(self.directory)))
        self.assertFalse(self.output.exists())

    def test_insecure_directory_symlinks_and_hardlinks_are_rejected_without_truncating_target(self):
        self.runtime.mkdir(mode=0o700)
        self.runtime.chmod(0o755)  # mkdir applies the process umask.
        log = self.runtime/'stderr.log'
        with self.assertRaisesRegex(ValueError, 'private runtime'):
            with acp_bridge.private_stderr(log, self.directory): pass
        self.runtime.chmod(0o700)
        outside = self.root/'outside'
        outside.write_text('keep intact')
        log.symlink_to(outside)
        with self.assertRaises(OSError):
            with acp_bridge.private_stderr(log, self.directory): pass
        log.unlink()
        os.link(outside, log)
        with self.assertRaisesRegex(ValueError, 'hard links'):
            with acp_bridge.private_stderr(log, self.directory): pass
        self.assertEqual(outside.read_text(), 'keep intact')

    def test_pi_worker_passes_private_log_and_only_reports_type_and_diagnostic_path(self):
        command = self.command(SECRET.encode())
        script = Path(command[1])
        script.write_text('import os\nassert os.environ["PATH"] == "/usr/bin:/bin"\n'
            'assert not any(k in os.environ for k in ("NODE_OPTIONS","NODE_PATH","LD_PRELOAD","DYLD_INSERT_LIBRARIES"))\n'
            +script.read_text())
        pinned = self.root/'pinned-placeholder'
        pinned.write_text('synthetic; never executed')
        tools, _ = tools_fixture(self.root)
        real_main = acp_bridge.main
        def bridge(_command, args, **kwargs):
            self.assertEqual(kwargs['stderr_path'], self.runtime/'stderr.log')
            self.assertEqual(kwargs['environment']['PATH'], '/usr/bin:/bin')
            return real_main(command, args, environment=kwargs['environment'], stderr_path=kwargs['stderr_path'])
        argv = ['pi_acp_worker', '--directory', str(self.directory), '--label', self.label,
                '--schema', str(self.schema), '--output', str(self.output), '--sandbox', 'workspace-write']
        error, output = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO('fixture-native-error')), \
                patch.dict(os.environ, NODE_OPTIONS='synthetic poison', NODE_PATH='synthetic poison', \
                    LD_PRELOAD='synthetic poison', DYLD_INSERT_LIBRARIES='synthetic poison'), \
                patch.multiple(pi_acp_worker, PI=pinned, NODE=pinned, SANDBOX=pinned), \
                patch('pi_acp_worker.prepare_toolchain', return_value=tools), \
                patch('acp_bridge.main', side_effect=bridge), \
                redirect_stderr(error), redirect_stdout(output):
            self.assertEqual(pi_acp_worker.main(), 1)
        self.assertEqual((self.runtime/'stderr.log').read_text(), SECRET)
        self.assertIn('ACP Pi worker failed: ValueError', error.getvalue())
        self.assertIn(str(self.runtime/'stderr.log'), error.getvalue())
        self.assertNotIn(SECRET, error.getvalue()+output.getvalue()+json.dumps(task_trace.query(self.directory)))

    def test_pi_worker_does_not_print_exception_body(self):
        argv = ['pi_acp_worker', '--directory', str(self.directory), '--label', self.label,
                '--schema', str(self.schema), '--output', str(self.output), '--sandbox', 'workspace-write']
        error = io.StringIO()
        with patch.object(sys, 'argv', argv), patch('pi_acp_worker.invoke', side_effect=ValueError(SECRET)), redirect_stderr(error):
            self.assertEqual(pi_acp_worker.main(), 1)
        self.assertEqual(error.getvalue(), 'ACP Pi worker failed: ValueError\n')

    def test_diagnostic_path_failure_does_not_expose_original_exception_chain(self):
        argv = ['pi_acp_worker', '--directory', str(self.directory), '--label', self.label,
                '--schema', str(self.schema), '--output', str(self.output), '--sandbox', 'workspace-write']
        error = io.StringIO()
        with patch.object(sys, 'argv', argv), patch('pi_acp_worker.invoke', side_effect=ValueError(SECRET)), \
                patch('pi_acp_worker.Path.resolve', side_effect=OSError(SECRET)), redirect_stderr(error):
            self.assertEqual(pi_acp_worker.main(), 1)
        self.assertEqual(error.getvalue(), 'ACP Pi worker failed: ValueError\n')

    def test_malformed_protocol_diagnostics_do_not_expose_raw_payload_in_parent_stderr(self):
        self.runtime.mkdir(mode=0o700)
        command = self.command(b'private child stderr', 'print('+repr(SECRET)+',flush=True)\n')
        error = io.StringIO()
        with redirect_stderr(error):
            with self.assertRaises(Exception):
                self.invoke(command)
        self.assertNotIn(SECRET, error.getvalue())

    def test_sdk_notification_errors_cannot_log_untrusted_method_or_payload(self):
        self.runtime.mkdir(mode=0o700)
        protocol = ('import json,runpy\nprint(json.dumps(dict(jsonrpc="2.0",method="session/update",'
            'params=dict(sessionId="fixture",update=dict(sessionUpdate='+repr(SECRET)+')))),flush=True)\n'
            'runpy.run_path('+repr(str(ROOT/'tests/fixtures/acp_fixture_agent.py'))+',run_name="__main__")\n')
        error = io.StringIO()
        with redirect_stderr(error):
            self.invoke(self.command(b'private stderr', protocol))
        self.assertNotIn(SECRET, error.getvalue())

    def test_sdk_filter_normalizes_symlink_venv_record_pathname(self):
        import acp
        alias=self.root/'linked-venv-package'
        alias.symlink_to(Path(acp.__file__).resolve().parent,target_is_directory=True)
        error=io.StringIO()
        handler=logging.StreamHandler(error)
        logger=logging.getLogger();logger.addHandler(handler)
        self.addCleanup(logger.removeHandler,handler)
        record=logging.LogRecord('root',logging.ERROR,str(alias/'connection.py'),1,
            SECRET,(),None)
        with acp_bridge.safe_protocol_logging():logger.handle(record)
        self.assertNotIn(SECRET,error.getvalue())
        self.assertIn('ACP SDK diagnostic:',error.getvalue())
