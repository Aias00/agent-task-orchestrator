"""Claude 2.1.325 restricted reader; real authenticated resume is UNKNOWN."""
import json
import os
from pathlib import Path
from tempfile import mkdtemp
import uuid

from hub_contract import (HubCapabilities,HubTurnResult,REVIEW_VERDICT_SCHEMA,HUB_TASK_SPEC_SCHEMA,
    failed,require,strict_json,token,validate_review_verdict,validate_task_spec)
import hub_sandbox as sandbox


class ClaudeHubAdapter:
    kind='claude'
    expected_version = '2.1.325'

    def __init__(self, executable=None, *, home=None, proxy=None):
        self.executable=sandbox.executable(self.kind,executable)
        self.home=sandbox.canonical(home if home is not None else Path.home())
        self.proxy=sandbox.validate_loopback_proxy(proxy)

    @staticmethod
    def capabilities():
        states=dict(structured_output='unknown',session_resume='unknown',interactive_attach='unknown',
            read_only_cli='supported',os_sandbox_compatible='supported',long_lived_stream='unsupported')
        return HubCapabilities({name:dict(state=value,evidence='H1 Spike S4-Claude: restricted auth_blocked observed; authenticated structured output/resume unqualified') for name,value in states.items()})

    def doctor(self):return sandbox.doctor(self.kind,self.executable,self.expected_version)

    def attach_argv(self, session_id, workspace):
        token(session_id);sandbox.canonical(workspace);require(self.executable is not None,'EXECUTABLE_UNAVAILABLE')
        return [str(self.executable),'--resume',session_id]  # Syntax only; capability remains unknown.

    def run_turn(self, request):
        private_values=sandbox.secrets(os.environ)
        try:
            require(request.agent.kind==self.kind)
            require(request.session_mode!='require_resume','RESUME_NOT_QUALIFIED')
            def invoke():
                doc=self.doctor();require(doc['available'],doc['code'])
                rt,env,prefix=sandbox.turn_context(request,self.executable,self.home,
                    writable_native=(self.home/'.claude',self.home/'.claude.json',Path('/tmp').resolve()/('claude-'+str(os.getuid()))),
                    proxy=self.proxy)
                schema=REVIEW_VERDICT_SCHEMA if request.role=='review' else HUB_TASK_SPEC_SCHEMA
                sid=str(uuid.uuid4())
                command=[str(self.executable),'--safe-mode','--restricted','--disable-slash-commands','-p',
                    '--output-format','stream-json','--verbose','--permission-mode','dontAsk',
                    '--tools','Read,Glob,Grep','--allowedTools','Read,Glob,Grep','--strict-mcp-config',
                    '--mcp-config','{"mcpServers":{}}','--json-schema',json.dumps(schema),
                    '--no-session-persistence','--session-id',sid]
                if request.agent.model:command+=['--model',request.agent.model]
                if request.agent.effort:command+=['--effort',request.agent.effort]
                result=sandbox.run(prefix+command,workspace=request.workspace,runtime=rt,environment=env,
                    prompt=request.prompt,timeout=request.timeout_seconds,stop=request.stop)
                if result.code not in ('OK','PROCESS_ERROR'):return failed(result.code)
                events=[strict_json(line) for line in result.stdout.splitlines() if line.strip()]
                terminal=[row for row in events if isinstance(row,dict) and row.get('type')=='result']
                require(len(terminal)==1)
                final=terminal[0]
                if result.code!='OK' or final.get('is_error') is True:
                    diagnostic=result.stderr.lower()+json.dumps(final).lower().encode()
                    auth=any(marker in diagnostic for marker in (b'not logged in',b'not authenticated',b'login required',b'authentication required'))
                    if not auth and final.get('is_error') is True and final.get('structured_output') is None and final.get('total_cost_usd')==0:
                        probe=Path(mkdtemp(prefix='auth-check-',dir=rt))
                        status=sandbox.run(prefix+[str(self.executable),'auth','status'],workspace=request.workspace,
                            runtime=probe,environment=env,timeout=min(3,request.timeout_seconds),stop=request.stop)
                        auth=status.code=='OK' and strict_json(status.stdout).get('loggedIn') is False
                    return failed('AUTH_BLOCKED' if auth else 'PROCESS_ERROR')
                require(final.get('is_error') is False and final.get('session_id')==sid,'SESSION_MISMATCH')
                output=final.get('structured_output')
                (validate_review_verdict if request.role=='review' else validate_task_spec)(output)
                require(not sandbox.has_secret(events,private_values),'SECRET_IN_REVIEW')
                return HubTurnResult('success','OK',sid,output,dict(session_mode_effective='fresh_only',
                    provenance=request.provenance if request.provenance!='native_resume' else 'ledger_reconstruction',
                    protocol='claude-2.1.325-stream-result',resume_qualification='unknown'))
            return sandbox.checked_turn(request,invoke)
        except Exception as error:return failed(str(error))
