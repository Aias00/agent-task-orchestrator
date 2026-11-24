"""Versioned hub data contracts. No I/O, subprocesses or vendor execution."""
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Callable, Protocol

KINDS = ('agy', 'claude', 'codex')
SHARED_ENV_LINKS = frozenset(('.venv', 'venv', 'node_modules', 'integrations/pi-acp/node_modules'))
CAPABILITIES = ('structured_output', 'session_resume', 'interactive_attach',
                'read_only_cli', 'os_sandbox_compatible', 'long_lived_stream')
MODES = ('fresh_only', 'resume_if_supported', 'require_resume')
SEVERITIES = ('blocker', 'major', 'minor', 'nit')
CATEGORIES = ('security', 'correctness', 'architecture', 'tests', 'scope', 'docs')


class HubError(ValueError):
    """Only a code-owned error code crosses public boundaries."""


def require(ok, code='INVALID_HUB_CONTRACT'):
    if not ok:
        raise HubError(code)


def token(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value))
    return value


def text(value, maximum=8192):
    require(isinstance(value, str) and 0 < len(value.encode()) <= maximum and '\x00' not in value)
    return value


def relative(value):
    text(value, 512)
    require(not value.startswith(('/', '\\', '-')) and '\\' not in value
            and all(p not in ('', '.', '..') for p in value.split('/')))
    return value


def exact(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields))


def integer(value, low=1, high=3600):
    require(type(value) is int and low <= value <= high)
    return value


def _pairs(rows):
    result = {}
    for name, value in rows:
        require(name not in result, 'DUPLICATE_JSON_KEY')
        result[name] = value
    return result


def strict_json(data):
    try:
        require(isinstance(data, (str, bytes)) and len(data) <= 1024*1024)
        def finite(value):
            result=float(value);require(math.isfinite(result),'NONFINITE_JSON');return result
        return json.loads(data, object_pairs_hook=_pairs,parse_float=finite,
            parse_constant=lambda _: (_ for _ in ()).throw(HubError('NONFINITE_JSON')))
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise HubError('INVALID_JSON') from None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


FINDING_FIELDS = ('id', 'severity', 'file', 'lines', 'category', 'title', 'evidence', 'required_fix')
REVIEW_VERDICT_SCHEMA = dict(type='object', additionalProperties=False,
    required=['verdict','summary','findings','checked_items','residual_risks'], properties={
    'verdict': dict(type='string',enum=['approve','request_changes','blocked']),
    'summary': dict(type='string',minLength=1,maxLength=8192),
    'findings': dict(type='array',maxItems=128,items=dict(type='object',additionalProperties=False,
        required=list(FINDING_FIELDS),properties={name:dict(type='string',minLength=1,maxLength=8192)
            for name in FINDING_FIELDS})),
    'checked_items': dict(type='array',maxItems=128,items=dict(type='string')),
    'residual_risks': dict(type='array',maxItems=128,items=dict(type='string'))})


def validate_review_verdict(value):
    exact(value, REVIEW_VERDICT_SCHEMA['required'])
    require(value['verdict'] in ('approve','request_changes','blocked'))
    text(value['summary'])
    require(isinstance(value['findings'],list) and len(value['findings']) <= 128)
    seen = set()
    for finding in value['findings']:
        exact(finding,FINDING_FIELDS)
        for name in FINDING_FIELDS:text(finding[name])
        token(finding['id']);require(finding['id'] not in seen);seen.add(finding['id'])
        require(finding['severity'] in SEVERITIES and finding['category'] in CATEGORIES)
        relative(finding['file']);require(re.fullmatch(r'L[1-9][0-9]*(?:-L[1-9][0-9]*)?',finding['lines']))
        bounds=[int(n) for n in re.findall(r'[0-9]+',finding['lines'])]
        require(len(bounds)==1 or bounds[0]<=bounds[1])
    for name in ('checked_items','residual_risks'):
        require(isinstance(value[name],list) and len(value[name]) <= 128)
        for item in value[name]:text(item)
    blockers = any(f['severity']=='blocker' for f in value['findings'])
    require(not (value['verdict']=='approve' and blockers), 'APPROVE_WITH_BLOCKER')
    require(not (value['verdict']=='request_changes' and not blockers), 'NO_BLOCKER_TO_FIX')
    return value


def blocker_fingerprint(tree_sha, verdict):
    validate_review_verdict(verdict)
    require(isinstance(tree_sha,str) and re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',tree_sha))
    return digest([tree_sha, sorted((f['file'],f['category'],' '.join(f['title'].casefold().split()))
        for f in verdict['findings'] if f['severity']=='blocker')])


@dataclass(frozen=True)
class HubCapabilities:
    values: dict

    def __post_init__(self):
        exact(self.values,CAPABILITIES)
        for row in self.values.values():
            exact(row,('state','evidence'))
            require(row['state'] in ('supported','unsupported','unknown'));text(row['evidence'])

    def supported(self, name):
        return self.values[name]['state']=='supported'


@dataclass(frozen=True)
class HubAgentSpec:
    kind: str
    model: str | None = None
    effort: str | None = None

    def __post_init__(self):
        require(self.kind in KINDS)
        if self.model is not None:
            require(isinstance(self.model,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}',self.model))
        require(self.effort in (None,'minimal','low','medium','high','xhigh','max'))


@dataclass(frozen=True)
class HubTurnRequest:
    agent: HubAgentSpec
    workspace: object
    runtime_dir: object
    prompt: str = field(repr=False)
    snapshot: Callable = field(repr=False)
    stop: Callable = field(repr=False)
    role: str = 'review'
    resume_session_id: str | None = None
    session_mode: str = 'resume_if_supported'
    timeout_seconds: int = 300
    provenance: str = 'fresh_only'
    protected_reads: tuple = field(repr=False, default=())

    def __post_init__(self):
        require(type(self.agent) is HubAgentSpec and self.role in ('review','plan'))
        text(self.prompt,512*1024);require(callable(self.snapshot) and callable(self.stop))
        require(self.session_mode in MODES);integer(self.timeout_seconds)
        if self.resume_session_id is not None:token(self.resume_session_id)
        require(self.provenance in ('fresh_only','native_resume','ledger_reconstruction','ledger_rollover',
            'ledger_reconstruction_from_agy','ledger_reconstruction_from_claude','ledger_reconstruction_from_codex'))


@dataclass(frozen=True)
class HubTurnResult:
    status: str
    code: str
    session_id: str | None = None
    output: dict | None = None
    metadata: dict = field(default_factory=dict)


def failed(code):
    allowed={'AUTH_BLOCKED','TIMEOUT','STOPPED','CLEANUP_BLOCKED','OUTPUT_LIMIT','PROCESS_ERROR',
        'INVALID_PROTOCOL','SESSION_MISMATCH','EXECUTABLE_UNAVAILABLE','VERSION_MISMATCH','REVIEW_PROMPT_TOO_LARGE',
        'RESUME_NOT_QUALIFIED','REVIEWER_MUTATION_VIOLATION','SECRET_IN_REVIEW'}
    code=code if code in allowed else 'INVALID_PROTOCOL'
    return HubTurnResult('auth_blocked' if code=='AUTH_BLOCKED' else 'timeout' if code=='TIMEOUT'
        else 'stopped' if code=='STOPPED' else 'cleanup_blocked' if code=='CLEANUP_BLOCKED' else 'blocked',code)


class HubAdapter(Protocol):
    kind: str
    def capabilities(self) -> HubCapabilities: ...
    def doctor(self) -> dict: ...
    def run_turn(self, request: HubTurnRequest) -> HubTurnResult: ...
    def attach_argv(self, session_id: str, workspace: object) -> list[str]: ...


HUB_TASK_SPEC_SCHEMA = dict(type='object',additionalProperties=False,
    required=['schema_version','task_id','repo','base_ref','branch','worker_contract','review_policy','worktree_policy'],
    properties={name:dict(type=kind) for name,kind in (
        ('schema_version','integer'),('task_id','string'),('repo','string'),('base_ref','string'),
        ('branch','string'),('worker_contract','object'),('review_policy','object'),('worktree_policy','object'))})


def validate_task_spec(value):
    exact(value,HUB_TASK_SPEC_SCHEMA['required']);require(type(value['schema_version']) is int and value['schema_version']==1)
    require(isinstance(value['task_id'],str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',value['task_id']))
    text(value['repo'],4096);require(value['repo'].startswith('/'))
    for key in ('base_ref','branch'):
        text(value[key],255);require(not value[key].startswith('-') and '\n' not in value[key])
    require(value['branch'] not in ('main','master'))
    policy=value['review_policy']
    exact(policy,('enabled','agent','session_mode','max_review_rounds','timeout_seconds','checklist','design_docs'))
    require(type(policy['enabled']) is bool and policy['session_mode'] in MODES)
    exact(policy['agent'],('kind','model','effort'));HubAgentSpec(**policy['agent'])
    integer(policy['max_review_rounds'],1,20);integer(policy['timeout_seconds'])
    for key in ('checklist','design_docs'):
        require(isinstance(policy[key],list) and len(policy[key])<=128)
        for item in policy[key]:relative(item) if key=='design_docs' else text(item)
    worker=value['worker_contract'];require(isinstance(worker,dict))
    require(set(worker)<={'objective','scope','authorization','acceptance','stages','limits','memory','sandbox',
                         'model','agent','permissions','host_handoff','budget','execution_policy'})
    for key in ('objective','scope','authorization'):text(worker.get(key))
    require(worker.get('memory',False) is False, 'H1_MEMORY_MUST_BE_DISABLED')
    require(isinstance(worker.get('acceptance'),list) and worker['acceptance'])
    manual=False
    for criterion in worker['acceptance']:
        require(isinstance(criterion,dict) and not set(criterion)-{'id','description','kind','checks'})
        token(criterion.get('id'));text(criterion.get('description'))
        require(criterion.get('kind','command') in ('manual','command'))
        if criterion.get('kind','command')=='manual':manual=True
        else:
            require(isinstance(criterion.get('checks'),list) and criterion['checks'])
            for command in criterion['checks']:
                require(isinstance(command,list) and command)
                for argument in command:text(argument)
    require(not policy['enabled'] or manual,'FINAL_MANUAL_GATE_REQUIRED')
    worktree=value['worktree_policy'];exact(worktree,('parent_dir','shared_env_links'))
    require(worktree['parent_dir'] is None or isinstance(worktree['parent_dir'],str) and worktree['parent_dir'].startswith('/'))
    require(isinstance(worktree['shared_env_links'],list) and len(set(worktree['shared_env_links']))==len(worktree['shared_env_links']))
    require(set(worktree['shared_env_links'])<=SHARED_ENV_LINKS)
    return value
