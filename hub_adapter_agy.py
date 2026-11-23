"""AGY 1.5.14 envelope protocol, qualified by H1 Spike S1-S3."""
import json
import os
from pathlib import Path

from hub_contract import (HubCapabilities,HubTurnResult,REVIEW_VERDICT_SCHEMA,HUB_TASK_SPEC_SCHEMA,
    failed,require,strict_json,token,validate_review_verdict,validate_task_spec)
import hub_sandbox as sandbox


class AgyHubAdapter:
    kind='agy'

    def __init__(self, executable=None, *, home=None, proxy=None):
        self.executable=sandbox.executable(self.kind,executable)
        self.home=sandbox.canonical(home if home is not None else Path.home())
        self.proxy=sandbox.validate_loopback_proxy(proxy)

    def capabilities(self):
        states=dict(structured_output='supported',session_resume='supported',interactive_attach='supported',
            read_only_cli='unsupported',os_sandbox_compatible='supported',long_lived_stream='unsupported')
        return HubCapabilities({name:dict(state=value,evidence='H1 Spike S1-S3: AGY 1.5.14, outer Seatbelt and post-turn integrity') for name,value in states.items()})

    def doctor(self):return sandbox.doctor(self.kind,self.executable,'1.5.14')

    def attach_argv(self, session_id, workspace):
        token(session_id);sandbox.canonical(workspace)
        require(self.executable is not None,'EXECUTABLE_UNAVAILABLE')
        return [str(self.executable),'--conversation',session_id]

    def run_turn(self, request):
        private_values=sandbox.secrets(os.environ)
        try:
            require(request.agent.kind==self.kind)
            def invoke():
                require(self.doctor()['available'],'VERSION_MISMATCH')
                persistent=request.session_mode!='fresh_only'
                writable=(self.home/'.gemini/antigravity-cli',) if persistent else ()
                rt,env,prefix=sandbox.turn_context(request,self.executable,self.home,writable_native=writable,proxy=self.proxy)
                schema=REVIEW_VERDICT_SCHEMA if request.role=='review' else HUB_TASK_SPEC_SCHEMA
                sandbox.write_private(rt/'schema.json',json.dumps(schema))
                command=[str(self.executable),'-p',request.prompt,'--output-format','json',
                    '--json-schema',str(rt/'schema.json'),'--disable-slash-commands','--print-timeout',str(request.timeout_seconds)+'s']
                if persistent and request.resume_session_id:command+=['--conversation',request.resume_session_id]
                if request.agent.model:command+=['--model',request.agent.model]
                if request.agent.effort:command+=['--effort',request.agent.effort]
                result=sandbox.run(prefix+command,workspace=request.workspace,runtime=rt,environment=env,
                    timeout=request.timeout_seconds,stop=request.stop)
                if result.code!='OK':
                    return failed('AUTH_BLOCKED' if b'not logged in' in result.stderr.lower() else result.code)
                envelope=strict_json(result.stdout);require(isinstance(envelope,dict) and envelope.get('status')=='SUCCESS')
                sid=token(envelope.get('conversation_id'))
                require(not persistent or not request.resume_session_id or sid==request.resume_session_id,'SESSION_MISMATCH')
                output=envelope.get('structured_output')
                (validate_review_verdict if request.role=='review' else validate_task_spec)(output)
                require(not sandbox.has_secret(envelope,private_values),'SECRET_IN_REVIEW')
                return HubTurnResult('success','OK',sid,output,dict(session_mode_effective=request.session_mode,
                    provenance=request.provenance,protocol='agy-1.5.14-envelope'))
            return sandbox.checked_turn(request,invoke)
        except Exception as error:return failed(str(error))
