"""Codex 0.171.0: native read-only Seatbelt; never nest sandbox-exec."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from ato_review.hub_contract import (HubCapabilities,HubTurnResult,REVIEW_VERDICT_SCHEMA,HUB_TASK_SPEC_SCHEMA,
    failed,require,strict_json,token,validate_review_verdict,validate_task_spec)
from ato_review import hub_sandbox as sandbox


class CodexHubAdapter:
    kind='codex'
    expected_version = '0.171.0'

    def __init__(self, executable=None, *, home=None, proxy=None):
        self.executable=sandbox.executable(self.kind,executable)
        self.home=sandbox.canonical(home if home is not None else Path.home())
        self.proxy=sandbox.validate_loopback_proxy(proxy)

    @staticmethod
    def capabilities():
        return HubCapabilities({name:dict(state='unsupported' if name=='long_lived_stream' else 'supported',
            evidence='H1 Spike S4: Codex 0.171.0 native read-only Seatbelt; post-turn integrity') for name in
            ('structured_output','session_resume','interactive_attach','read_only_cli','os_sandbox_compatible','long_lived_stream')})

    def doctor(self):return sandbox.doctor(self.kind,self.executable,self.expected_version)

    def attach_argv(self, session_id, workspace):
        token(session_id);sandbox.canonical(workspace);require(self.executable is not None,'EXECUTABLE_UNAVAILABLE')
        return [str(self.executable),'resume',session_id]

    def run_turn(self, request):
        private_values=sandbox.secrets(os.environ)
        try:
            require(request.agent.kind==self.kind)
            def invoke():
                doc=self.doctor();require(doc['available'],doc['code'])
                rt,env,_=sandbox.turn_context(request,self.executable,self.home,outer=False,codex=True,proxy=self.proxy)
                schema=REVIEW_VERDICT_SCHEMA if request.role=='review' else HUB_TASK_SPEC_SCHEMA
                sandbox.write_private(rt/'schema.json',json.dumps(schema))
                resumed=request.session_mode!='fresh_only' and request.resume_session_id
                command=[str(self.executable),'exec']
                if resumed:command+=['resume',request.resume_session_id]
                if not request.protected_reads:
                    command+=['-c','sandbox_mode="read-only"'] if resumed else ['--sandbox','read-only']
                command+=['-c','approval_policy="never"','--json','--output-schema',str(rt/'schema.json'),'-o',str(rt/'output.json')]
                if request.agent.model:command+=['--model',request.agent.model]
                if request.agent.effort:command+=['-c','model_reasoning_effort='+json.dumps(request.agent.effort)]
                def run(argv):
                    return sandbox.run(argv+['-'],workspace=request.workspace,runtime=rt,environment=env,
                        prompt=request.prompt,timeout=request.timeout_seconds,stop=request.stop)
                if request.protected_reads:
                    with TemporaryDirectory(prefix='ato-review-tool-') as scratch:
                        temporary=Path(scratch).resolve()
                        env['TMPDIR']=str(temporary)
                        result=run(command+sandbox.guarded_codex_args(request.workspace,temporary,
                            writable=False,denied=request.protected_reads))
                else:
                    result=run(command)
                if result.code!='OK':return failed('AUTH_BLOCKED' if b'not logged in' in result.stderr.lower() else result.code)
                events=[strict_json(line) for line in result.stdout.splitlines() if line.strip()]
                started=[row.get('thread_id') for row in events if isinstance(row,dict) and row.get('type')=='thread.started']
                require(len(started)==1);sid=token(started[0])
                require(not resumed or sid==request.resume_session_id,'SESSION_MISMATCH')
                output=strict_json(sandbox.read_output(rt/'output.json'))
                (validate_review_verdict if request.role=='review' else validate_task_spec)(output)
                require(not sandbox.has_secret([events,output],private_values),'SECRET_IN_REVIEW')
                return HubTurnResult('success','OK',sid,output,dict(session_mode_effective=request.session_mode,
                    provenance=request.provenance,protocol='codex-0.171.0-jsonl-and-output-file'))
            return sandbox.checked_turn(request,invoke)
        except Exception as error:return failed(str(error))
