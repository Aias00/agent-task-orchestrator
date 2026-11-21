#!/usr/bin/env python3
"""Origin-only bounded actual-record projection. Never run this on real data as a worker.

The origin supplies an exact reviewed, hash-pinned plan; there is no discovery
of private histories, credential lookup, queue submission or live migration.
The executable path uses macOS sandbox-exec plus a Python audit fence. The
library helpers also support synthetic fixture tests, without a real-record CLI
escape hatch. Missing immutable receipt/lineage is a failure, never inferred.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import re
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time
from urllib.parse import unquote, urlparse

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
QUESTION = 'question-9c3eb5e4a646e8ffd1b5'
OLD = 'OPERATOR_PROJECT/vps-picks/.codex/tasks/workflows/20261119-020710-0609b14c'
DELIVERY = 'OPERATOR_PROJECT/vps-picks/.codex/tasks/workflows/20261119-051218-c82c16cf'
RESULTS = {'result-dbf8134af146aa5ff0a2', 'result-8610e94538370b8ce6e5'}
DUPLICATE = 'OPERATOR_EVIDENCE/generic-platform-hub-followthrough-20261119/duplicate-completion-8610e945.json'
PROOF = 'OPERATOR_EVIDENCE/ato-tracking-rectification-20261119/origin-readonly-proof.json'
AUTHORIZATION = '2026-11-19 把未修复的开始启动修复啊; bounded origin read-only tracking qualification'
MAX_FILES, MAX_BYTES = 384, 256 * 1024 * 1024


class FenceViolation(BaseException):
    """Not swallowed by runtime's conservative OSError/ValueError projections."""


def canonical(value, exists=True):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts or any(x.is_symlink() for x in (p, *p.parents)):
        raise ValueError('exact absolute nonsymlink path required')
    if str(p.resolve(strict=exists)) != str(p):
        raise ValueError('canonical path required')
    return p


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def json_read(path):
    p = canonical(path)
    if not p.is_file() or p.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('bounded regular plan/JSON required')
    return json.loads(p.read_text())


def source_fingerprint(root=ROOT):
    # Exact frozen acceptance algorithm, including docs/provenance.
    patterns = ['*.py', '*.mjs', 'README.md', 'AGENTS.md', 'requirements*.lock',
        'tests/**/*.py', 'tests/**/*.json', 'tests/**/*.mjs', 'docs/**/*.md',
        'docs/**/*.json', 'docs/**/*.py', 'bin/*', 'skills/**/*.md', 'scripts/*.py',
        'integrations/pi-acp/package.json', 'integrations/pi-acp/package-lock.json']
    paths = {p for pattern in patterns for p in root.glob(pattern) if p.is_file() and not p.is_symlink()}
    values = {str(p.relative_to(root)): sha(p) for p in sorted(paths)}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def validate_plan(plan):
    required = {'version', 'authorization', 'candidate_manifest_sha256', 'source_sha256',
        'native_home', 'hub_file', 'question_file', 'policy_file', 'mutes_file',
        'control_db', 'old_workflow', 'delivery_workflow', 'generic_workflows',
        'duplicate_evidence', 'approved_criteria', 'deferred_criteria', 'workspace_bindings',
        'immutable_live_files', 'absent_live_files', 'native_headers', 'operation_receipt'}
    if set(plan) != required or plan['version'] != 1 or plan['authorization'] != AUTHORIZATION:
        raise ValueError('exact version-1 reviewed origin plan required')
    if (plan['old_workflow'] != OLD or plan['delivery_workflow'] != DELIVERY
            or plan['duplicate_evidence'] != DUPLICATE or len(plan['generic_workflows']) != 2
            or len(set(plan['generic_workflows'])) != 2):
        raise ValueError('only the named VPS and exact generic duplicate pair are authorized')
    if Path(plan['question_file']).name != QUESTION+'.json':
        raise ValueError('exact accepted question required')
    pinned = plan['immutable_live_files']
    absent = plan['absent_live_files']
    headers = plan['native_headers']
    if (not isinstance(pinned, dict) or not pinned or len(pinned) > MAX_FILES
            or not isinstance(absent, list) or len(absent) > MAX_FILES
            or not isinstance(headers, dict) or not 2 <= len(headers) <= 4):
        raise ValueError('bounded exact live file/header inventory required')
    if set(pinned) & set(absent) or set(pinned) & set(headers):
        raise ValueError('full files, absent paths and header-only reads must be distinct')
    workflows = {OLD, DELIVERY, *plan['generic_workflows']}
    mandatory = {plan[k] for k in ('hub_file', 'question_file', 'policy_file', 'mutes_file', 'control_db', 'duplicate_evidence')}
    mandatory.update(str(Path(w)/n) for w in workflows for n in ('state.json', 'contract.json', 'origin.json'))
    if not mandatory <= pinned.keys():
        raise ValueError('all named contracts/questions/notice/policy evidence must be pinned')
    for w in workflows:
        for n in ('STOP', 'workflow-lineage.json', 'user-closure.json'):
            if str(Path(w)/n) not in pinned and str(Path(w)/n) not in absent:
                raise ValueError('pin STOP/closure/lineage presence or absence explicitly')
    for w, workspace in plan['workspace_bindings'].items():
        if w not in workflows:
            raise ValueError('workspace registration outside named workflows')
        canonical(workspace)
    if not plan['approved_criteria'] or not plan['deferred_criteria']:
        raise ValueError('explicit production mapping and retained original gaps required')
    if plan['operation_receipt'] is not None and plan['operation_receipt'] not in pinned:
        raise ValueError('existing operation receipt must be pinned')
    total = 0
    home = canonical(plan['native_home'])
    interaction_inputs = {plan[k] for k in ('hub_file', 'question_file', 'policy_file', 'mutes_file', 'control_db')}
    interaction_inputs |= {plan['control_db']+suffix for suffix in ('-wal', '-shm')}
    for name, digest in pinned.items():
        p = canonical(name)
        # An authorization *evidence* filename is not a credential. Reject
        # actual credential/config containers rather than the substring auth.
        sensitive = {'auth.json', 'credentials', 'credentials.json', 'credentials.yaml',
            'token', 'token.json', 'tokens.json', 'secrets.json', 'config.toml',
            'id_rsa', 'id_ed25519', '.netrc', '.npmrc', '.pypirc'}
        credential = (p.name.lower() in sensitive or p.name.lower().startswith('.env')
                      or p.suffix.lower() in ('.pem', '.key'))
        if (not p.is_file() or not isinstance(digest, str) or len(digest) != 64
                or credential):
            raise ValueError('invalid or credential-like live input')
        if (p.is_relative_to(home) and name not in interaction_inputs and
                (p.parent != home or not re.fullmatch(r'state_[0-9]+\.sqlite(?:-wal|-shm)?', p.name))):
            raise ValueError('native state reads are exact indexes and first headers only')
        if p.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('individual live input exceeds bound')
        total += p.stat().st_size
        if p.suffix == '.sqlite':
            for suffix in ('-wal', '-shm'):
                if name+suffix not in pinned and name+suffix not in absent:
                    raise ValueError('pin SQLite WAL/shared-memory presence or absence explicitly')
    if total > MAX_BYTES:
        raise ValueError('total read bound exceeded')
    for name in absent:
        if canonical(name, exists=False).exists():
            raise ValueError('expected absent live path exists')
    for name, item in headers.items():
        if (not canonical(name).is_relative_to(home) or set(item) != {'native_id', 'header_sha256'}):
            raise ValueError('native header must be exact and inside named native home')
    return plan


def snapshot(plan):
    files = {name: sha(name) for name in plan['immutable_live_files']}
    if files != plan['immutable_live_files']:
        raise ValueError('live input hash drift; do not regenerate approval/proof to hide drift')
    for name in plan['absent_live_files']:
        if Path(name).exists() or Path(name).is_symlink():
            raise ValueError('live absence drift')
    for name, expected in plan['native_headers'].items():
        with Path(name).open('rb') as stream:
            header = stream.readline(8 * 1024 * 1024 + 1)
        if (not header.endswith(b'\n') or len(header) > 8 * 1024 * 1024
                or hashlib.sha256(header).hexdigest() != expected['header_sha256']):
            raise ValueError('exact native header changed')
    return files


def install_fence(plan, observation_root, source_files):
    """Deny sockets, non-Git subprocesses, unlisted reads and all live writes.

    OS sandbox-exec additionally denies writes by SQLite/native child code and
    all networking. Real SQLite connections must explicitly use mode=ro.
    """
    private = canonical(observation_root)
    # Read source, never an unsealed pre-existing candidate bytecode cache.
    sys.pycache_prefix = str(private/'unused-bytecode')
    allowed = {Path(n) for n in plan['immutable_live_files']} | {Path(n) for n in plan['native_headers']}
    allowed |= set(source_files)
    libraries = {Path(sysconfig.get_path(k)).resolve() for k in ('stdlib', 'platstdlib', 'purelib', 'platlib')}
    repos = {str(Path(w).parents[3]) for w in (OLD, DELIVERY, *plan['generic_workflows'])}
    native = Path(plan['native_home'])
    workflow_parents = {Path(w).parent for w in (OLD, DELIVERY, *plan['generic_workflows'])}
    scan_dirs = {native, *workflow_parents}
    def audit(event, args):
        if event.startswith('socket.') or event in ('os.system', 'os.exec', 'os.posix_spawn', 'os.fork', 'pty.spawn'):
            raise FenceViolation('transport/process operation forbidden: '+event)
        if event in ('sqlite3.enable_load_extension', 'sqlite3.load_extension'):
            raise FenceViolation('SQLite executable extensions forbidden')
        if event == 'os.kill' and args[1] != 0:
            raise FenceViolation('process signals forbidden')
        if event == 'subprocess.Popen':
            argv = args[1]
            if (args[0] != 'git' or not isinstance(argv, (list, tuple)) or len(argv) != 5 or
                    argv[0] != 'git' or argv[1] != '-C' or argv[2] not in repos or
                    argv[3] != 'rev-parse' or argv[4] not in
                    ('--show-toplevel', '--absolute-git-dir', '--git-common-dir')):
                raise FenceViolation('only exact read-only Git identity commands allowed')
        if event == 'sqlite3.connect':
            uri = str(args[0])
            if uri.startswith('file:'):
                path = Path(unquote(urlparse(uri).path))
                if path not in allowed or 'mode=ro' not in uri.split('?', 1)[-1].split('&'):
                    raise FenceViolation('live SQLite must be explicitly pinned and mode=ro')
            elif not Path(uri).absolute().is_relative_to(private):
                # Candidate observation DB is the only writable SQLite.
                raise FenceViolation('live SQLite must be mode=ro')
        if event == 'open':
            name, mode, flags = args
            if isinstance(name, int):
                return
            p = Path(os.path.abspath(os.fsdecode(name)))
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not p.is_relative_to(private):
                raise FenceViolation('write outside private observation ledger')
            if not writing and not (p in allowed or p.is_relative_to(private) or
                    any(p.is_relative_to(lib) for lib in libraries) or str(p) == os.devnull):
                raise FenceViolation('unlisted read forbidden: '+str(p))
        if event in ('os.listdir', 'os.scandir'):
            p = Path(args[0]).absolute()
            if not (p in scan_dirs or p.parent in workflow_parents or p.is_relative_to(private) or p.is_relative_to(ROOT)
                    or any(p.is_relative_to(lib) for lib in libraries)):
                raise FenceViolation('unlisted directory enumeration')
        mutating = {'os.mkdir': (0,), 'os.remove': (0,), 'os.rmdir': (0,),
                    'os.rename': (0, 1), 'os.chmod': (0,), 'os.truncate': (0,),
                    'os.link': (0, 1), 'os.symlink': (0, 1), 'os.utime': (0,)}
        if event in mutating:
            for index in mutating[event]:
                if not Path(os.path.abspath(os.fsdecode(args[index]))).is_relative_to(private):
                    raise FenceViolation('filesystem mutation outside observation ledger')
    sys.addaudithook(audit)


def readonly_rows(db, identifiers):
    with sqlite3.connect(Path(db).as_uri()+'?mode=ro', uri=True, timeout=2) as con:
        actions, claims = [], []
        for identifier in identifiers:
            row = con.execute('select body from actions where id=?', (identifier,)).fetchone()
            if row:
                actions.append(json.loads(row[0]))
            for key, body in con.execute('select key,body from outbox where key like ?', ('followup:'+identifier+':%',)):
                claims.append((key, json.loads(body)))
    return actions, claims


def projection(plan, private):
    """Run real candidate validators/projections; never run a controller tick."""
    import approved_delivery as approved
    import ato_config
    import completion_delivery
    import completion_notifications
    import control
    import interaction
    import runner
    import session_router
    import workspace_association
    # Consume the trusted adapter's proof; no vendor header/index parser is
    # duplicated in this orchestration script. Snapshot treats header bytes as
    # opaque and checks only their exact bounded hash.
    adapter = session_router.legacy_workspace_adapter()
    for name, item in plan['native_headers'].items():
        actual = adapter.workspace_identity(item['native_id'])
        if actual['rollout'] != name or actual['header_sha256'] != item['header_sha256']:
            raise ValueError('trusted native metadata differs from exact reviewed header pins')
    # Pin source bytes again after all projections. Only private observation
    # files are copied; live paths inside contracts/evidence remain live reads.
    folder = interaction.directory()
    for key, name in (('hub_file', 'hub.json'), ('policy_file', 'notification-policy.json'),
                      ('mutes_file', 'notification-project-mutes.json')):
        (folder/name).write_bytes(Path(plan[key]).read_bytes())
    hub = interaction.config()
    if os.environ.get('CODEX_THREAD_ID') != hub['thread_id']:
        raise ValueError('probe must execute in the actually bound hub; do not impersonate another actor')
    if str(ato_config.codex_home()) != plan['native_home']:
        raise ValueError('native account context mismatch')
    (folder/'questions').mkdir()
    (folder/'questions'/(QUESTION+'.json')).write_bytes(Path(plan['question_file']).read_bytes())
    for w, workspace in plan['workspace_bindings'].items():
        inspected = workspace_association.inspect(w, workspace)
        proof = private/('workspace-'+runner.digest(w)+'.json')
        runner.save(proof, inspected['expected_evidence'])
        workspace_association.register(w, workspace, proof, AUTHORIZATION)
    # This is the actual formerly failing binding, with original native header.
    interaction.binding(OLD, require_open=False)
    inspected = approved.inspect(OLD, QUESTION, DELIVERY)['expected_evidence']
    inspected.update(approved_criteria=plan['approved_criteria'], deferred_criteria=plan['deferred_criteria'])
    proof = private/'approved-scope.json'; runner.save(proof, inspected)
    approved.register(OLD, QUESTION, DELIVERY, proof, AUTHORIZATION)
    question = interaction.read_question(QUESTION)
    observed = question['approved_delivery']
    if observed['validation'] != 'valid' or observed['approved_scope_status'] != 'delivered':
        raise ValueError('accepted scope not independently delivered')
    state = runner.read(Path(OLD)/'state.json')
    scope = approved.diagnostic_scope(OLD, state)
    if (state['status'] != 'blocked' or not set(scope['diagnostic_criteria'])
            or set(scope['diagnostic_criteria']) & set(plan['approved_criteria'])
            or not set(scope['diagnostic_criteria']) <= set(plan['deferred_criteria'])):
        raise ValueError('old unresolved residuals not retained independently')
    # Actual controller question reconciliation, no tick/wakeup/repair/send.
    control.reconcile_questions(dict(hub_thread=hub['thread_id']))
    action = next(a for a in control.rows(True) if a['id'] == QUESTION)
    if (action['state'] != 'resolved' or action.get('resolution_basis') != 'approved_delivery'
            or action.get('presented_at') or action.get('notice')):
        raise ValueError('controller did not resolve only the already-delivered scope')
    left, right = plan['generic_workflows']
    identifiers = {'result-'+runner.digest([w, runner.read(Path(w)/'state.json')['contract_hash']])[:20]
                   for w in (left, right)}
    if identifiers != RESULTS:
        raise ValueError('generic workflows are not the exact original duplicate result IDs')
    # The named report is a provenance pin, not authority to infer equivalence.
    report_bytes = Path(plan['duplicate_evidence']).read_bytes()
    if not all(identifier.encode() in report_bytes for identifier in RESULTS):
        raise ValueError('named duplicate evidence does not identify both exact results')
    receipt = plan['operation_receipt']
    if receipt:
        inspected = completion_delivery.inspect(left, right, receipt)['expected_evidence']
        proof = private/'same-operation.json'; runner.save(proof, inspected)
        completion_delivery.register(left, right, receipt, proof, AUTHORIZATION)
    identity = completion_delivery.identity(right)
    if (identity['validation'] != 'valid' or
            not {left, right} <= {member['workflow'] for member in identity['members']}):
        raise ValueError('existing immutable compatible lineage/shared operation proof required; do not fabricate it')
    # Import only exact old completion rows into the disposable DB. Preserve
    # uncertain claims verbatim; never synthesize a presentation or a receipt.
    actions, claims = readonly_rows(plan['control_db'], RESULTS)
    if not actions and not claims:
        raise ValueError('original completion notice/unknown claim evidence missing')
    with control.database() as con, con:
        for a in actions:
            if a.get('kind') != 'workflow_result' or a.get('workflow') not in (left, right):
                raise ValueError('original completion row binding changed')
            con.execute('insert into actions values(?,?)', (a['id'], json.dumps(a)))
        for key, claim in claims:
            con.execute('insert into outbox values(?,?)', (key, json.dumps(claim)))
    prior_verified = []
    for w in (left, right):
        d, s, c, origin = interaction.binding(w, require_open=False)
        projected = dict(id='result-'+runner.digest([w, s['contract_hash']])[:20], kind='workflow_result',
            workflow=w, origin=origin, hub=hub['thread_id'], state='unpresented',
            baseline_generation=__import__('recovery').generation(d, s))
        before = dict(projected)
        result = completion_notifications.observation(projected)
        if projected != before or 'presented_at' in projected or 'notice' in projected:
            raise ValueError('duplicate projection manufactured presentation')
        if result.get('duplicate_of'):
            if completion_notifications.eligible(projected):
                raise ValueError('duplicate completion remains eligible')
            prior_verified.append(projected['id'])
    if not prior_verified:
        raise ValueError('no original physical-delivery claim barrier found')
    if control.settings() is not None:
        raise ValueError('probe must never enable a controller')
    return dict(workspace_binding_verified=True, accepted_scope_delivery_verified=True,
        old_residuals_preserved=True, no_duplicate_approved_scope_diagnostic=True,
        same_physical_delivery_dedup_verified=True, raw_old_workflow_status=state['status'],
        real_delivery_status=runner.read(Path(DELIVERY)/'state.json')['status'],
        retained_diagnostic_criteria=scope['diagnostic_criteria'], deduplicated_result_ids=prior_verified,
        native_identity_source='trusted adapter exact index ID and first header only',
        projection='actual read_question, reconcile_questions, diagnostic_scope and completion observation; no tick')


def child(plan, private):
    sys.path.insert(0, str(ROOT))
    sys.pycache_prefix = str(private/'unused-bytecode')
    # Import code before installing the narrow file fence (imports do no live
    # state I/O); OS write/network restrictions already apply to this process.
    import agents, approved_delivery, completion_delivery, completion_notifications
    import control, interaction, workspace_association, workflow_lineage
    import prepare_tracking_candidate as package
    source = {ROOT/n for n in json_read(ROOT/package.MANIFEST)['files']}
    source.add(ROOT/package.MANIFEST)
    os.environ['ATO_HOME'] = str(private)
    os.environ['CODEX_HOME'] = plan['native_home']
    for key in ('ATO_ACTOR_FILE', 'ATO_SESSION_REF', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
        os.environ.pop(key, None)
    # No event path is allowed to reach a transport, provider, executor or tick.
    def forbidden(*args, **kwargs):
        raise FenceViolation('transport/model/business operation forbidden')
    import transport, session_router, codex_session_adapter, appserver
    for module, names in ((control, ('send', 'tick', 'start', 'watch')),
            (session_router, ('send', 'send_completion', 'load_or_resume_v2')),
            (codex_session_adapter, ('queue', '_queue', 'create_session', 'activity', '_events')),
            (appserver, ('Client',)), (transport, ('dispatch_managed', 'prepare_managed',
                'managed_bus', 'dispatch_one', 'dispatch_registered'))):
        for name in names:
            if hasattr(module, name):
                setattr(module, name, forbidden)
    original_run = subprocess.run
    def identity_run(argv, **kwargs):
        # Even the older compatible-lineage validator gets the same isolated
        # Git configuration boundary as the new identity validators.
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS='0')
        kwargs['env'] = env
        kwargs['timeout'] = min(kwargs.get('timeout', 10), 10)
        return original_run(argv, **kwargs)
    subprocess.run = identity_run
    install_fence(plan, private, source)
    before = snapshot(plan)
    if source_fingerprint() != plan['source_sha256'] or sha(ROOT/package.MANIFEST) != plan['candidate_manifest_sha256']:
        raise ValueError('exact candidate/source seal differs')
    evidence = projection(plan, private)
    after = snapshot(plan)
    if after != before or source_fingerprint() != plan['source_sha256']:
        raise ValueError('real records or source changed during qualification')
    return dict(evidence, version=1, manifest_sha256=plan['candidate_manifest_sha256'],
        source_sha256=plan['source_sha256'], immutable_live_files=after,
        immutable_absent_files=plan['absent_live_files'], native_headers=plan['native_headers'],
        live_records_unchanged=True, live_activation_performed=False,
        business_or_model_requests=0, user_notifications_sent=0,
        os_write_and_network_sandbox=True, runtime_activated=False, observation_root=str(private))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--observation-root', required=True, type=Path)
    parser.add_argument('--proof', required=True, type=Path)
    parser.add_argument('--_sandbox-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    started = time.monotonic()
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('120s origin probe deadline')))
    signal.alarm(120)
    plan = validate_plan(json_read(args.plan))
    if (sha(ROOT/'build/tracking-rectification/candidate.json') != plan['candidate_manifest_sha256']
            or source_fingerprint() != plan['source_sha256']):
        raise ValueError('reviewed candidate seal differs before any projection import')
    private = canonical(args.observation_root, exists=False)
    proof = canonical(args.proof, exists=False)
    if args._sandbox_child:
        if os.environ.pop('ATO_TRACKING_READONLY_CHILD', '') != str(private):
            raise ValueError('internal child requires origin sandbox launcher')
        data = child(plan, private)
        print(json.dumps(data, ensure_ascii=False))
        return 0
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file():
        raise ValueError('origin OS write/network sandbox required; do not run an unfenced probe')
    temp_roots = (Path('/private/tmp'), Path(tempfile.gettempdir()).resolve())
    if (str(proof) != PROOF or not proof.parent.is_dir() or
            not any(private.is_relative_to(root) and private != root for root in temp_roots)
            or private.is_relative_to(Path(plan['native_home'])) or private.is_relative_to(ROOT)):
        raise ValueError('use a private OS-temp observation root and the exact authorized external proof path')
    if private.exists() or proof.exists() or private == proof.parent or proof.is_relative_to(private):
        raise ValueError('fresh separate observation root and external proof path required')
    private.mkdir(mode=0o700, parents=False)
    # SQLite may need an existing -shm for mode=ro WAL; OS denies any real
    # shared-memory writes. An incompatible host read fails, never relaxes it.
    profile = '(version 1) (allow default) (deny network*) (deny file-write*) (allow file-write* (subpath '+json.dumps(str(private))+'))'
    env = dict(os.environ, ATO_TRACKING_READONLY_CHILD=str(private), PYTHONDONTWRITEBYTECODE='1')
    proc = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-B', str(Path(__file__).resolve()),
        '--plan', str(args.plan), '--observation-root', str(private), '--proof', str(proof), '--_sandbox-child'],
        env=env, capture_output=True, text=True, timeout=max(1, 115-(time.monotonic()-started)))
    if proc.returncode:
        raise ValueError('read-only projection failed; keep evidence, do not relax fence: '+proc.stderr[-3000:])
    result = json.loads(proc.stdout)
    result.update(elapsed_seconds=round(time.monotonic()-started, 3), authorization=AUTHORIZATION,
                  plan_sha256=sha(args.plan))
    with proof.open('x') as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False); stream.write('\n')
    print(json.dumps(dict(passed=True, proof=str(proof), live_activation_performed=False)))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Exception, FenceViolation) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
