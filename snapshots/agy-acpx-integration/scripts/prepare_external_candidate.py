#!/usr/bin/env python3
"""Prepare/check portable inert source. Never install, activate, or include state."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
DEST = Path('build/external-coordinator')
MANIFEST = str(DEST/'candidate.json')
BASE = 'docs/external-coordinator/provenance/baseline.json'
BASE_SHA = 'c907fe68a40e262831e6a1c32d5b23602cf98f2b6f37a0ddc432655a82652c13'
PATTERNS = ('*.py', '*.mjs', 'README.md', 'AGENTS.md', 'requirements*.lock',
    'bin/*', 'scripts/*.py', 'docs/**/*.md', 'docs/external-coordinator/**/*.json',
    'skills/**/*.md', 'examples/*.json', 'tests/**/*.py', 'tests/**/*.mjs', 'tests/**/*.json',
    'integrations/pi-acp/package.json', 'integrations/pi-acp/package-lock.json')
FORBIDDEN = {'.git', '.codex', '.agents', 'node_modules', '__pycache__', '.aws', '.ssh',
             'private', '.venv', 'auth.json', '.env'}
REQUIRED = {'acpx_profile.py','acpx_worker.py','acpx_bridge.mjs','acpx_native_proxy.mjs',
    'external_coordinator.py','external_operator.py','bin/ato-control',
    'scripts/prepare_external_candidate.py','scripts/external_host_smoke.py',
    'scripts/external_host_readback.py','docs/external-coordinator/demo-contract.json'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)+'\n').encode()


def collect(root=ROOT):
    root = Path(root).resolve()
    result = {}
    for pattern in PATTERNS:
        for path in root.glob(pattern):
            relative = path.relative_to(root)
            if (set(relative.parts) & FORBIDDEN or not path.is_file()
                    or any((root/Path(*relative.parts[:n])).is_symlink()
                           for n in range(1,len(relative.parts)+1))
                    or not path.resolve().is_relative_to(root)):
                raise ValueError('unsafe source input: '+str(relative))
            if path.stat().st_mode & (stat.S_ISUID | stat.S_ISGID):
                raise ValueError('privileged source mode')
            result[relative.as_posix()] = digest(path.read_bytes())
    return dict(sorted(result.items()))


def build(root=ROOT):
    root=Path(root)
    baseline=(root/BASE).read_bytes()
    if digest(baseline) != BASE_SHA:
        raise ValueError('frozen provenance baseline differs')
    base=json.loads(baseline)
    inventory=collect(root)
    if not REQUIRED <= inventory.keys():
        raise ValueError('incomplete integration source')
    for name,sha in base['protected'].items():
        if inventory.get(name) != sha:
            raise ValueError('protected inherited test/fixture changed: '+name)
    return dict(version=1, kind='ato-external-coordinator-source', runtime_activated=False,
        baseline_sha256=BASE_SHA, source_baseline=base['source'],
        files=inventory, file_modes={name:stat.S_IMODE((root/name).stat().st_mode) for name in inventory},
        dependencies=dict(acpx_version='0.22.0', acpx_commit=base['acpx_commit'],
            acpx_archive_sha256=base['acpx_package_sha256'], codex_acp_version='1.1.5',
            native_codex_version='0.144.6', node_version='24.22.1',
            location='private host config; no packages or auth bundled'),
        authority='canonical ATO workflow/task/attempt/STOP/locks/independent acceptance',
        transport='replaceable acpx public runtime; in-memory fresh session, no CLI queue',
        qualification=dict(fixture_evidence='docs/external-coordinator/validation.md',
            host_regression='scheduler independently required',
            native_policy='host model-free gate required', real_model='origin only; not executed by qualification worker'),
        delivery=dict(state_included=False, secrets_included=False, symlinks_included=False,
            dependencies_included=False, activation='origin additive entry only; not performed'))


def write_archive(path,manifest,root=ROOT):
    with Path(path).open('wb') as raw, gzip.GzipFile(filename='',mode='wb',fileobj=raw,mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w',format=tarfile.USTAR_FORMAT) as archive:
            for name in [*manifest['files'], MANIFEST]:
                data=(Path(root)/name).read_bytes() if name in manifest['files'] else encoded(manifest)
                if name in manifest['files'] and digest(data) != manifest['files'][name]:
                    raise ValueError('source changed during prepare: '+name)
                item=tarfile.TarInfo(name);item.size=len(data)
                item.mode=manifest['file_modes'].get(name,0o644)
                item.uid=item.gid=item.mtime=0
                archive.addfile(item,io.BytesIO(data))


def check_archive(path,manifest):
    with tarfile.open(path,'r:gz') as archive:
        members=archive.getmembers()
        if [m.name for m in members] != [*manifest['files'],MANIFEST]:
            raise ValueError('archive inventory differs (duplicates/traversal/additions rejected)')
        for item in members:
            if (not item.isfile() or item.uid or item.gid or item.mtime or
                    item.mode != manifest['file_modes'].get(item.name,0o644)):
                raise ValueError('nonportable archive metadata')
            data=archive.extractfile(item).read()
            expected=manifest['files'].get(item.name)
            if (digest(data) != expected if expected else data != encoded(manifest)):
                raise ValueError('archive content differs: '+item.name)


def prepare(root=ROOT):
    root=Path(root);manifest=build(root);output=root/DEST
    output.mkdir(parents=True,exist_ok=True)
    (root/MANIFEST).write_bytes(encoded(manifest))
    write_archive(output/'candidate.tar.gz',manifest,root)
    check(root)
    (output/'candidate.tar.gz.sha256').write_text(digest((output/'candidate.tar.gz').read_bytes())+'  candidate.tar.gz\n')
    return manifest


def check(root=ROOT,archive=None):
    root=Path(root);expected=build(root)
    if (root/MANIFEST).read_bytes() != encoded(expected):
        raise ValueError('candidate source/manifest/modes changed; prepare after final edits')
    check_archive(archive or root/DEST/'candidate.tar.gz',expected)
    return expected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',choices=['prepare'],default='prepare')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--archive',type=Path,help='original archive to verify after extracting portable source')
    args=parser.parse_args()
    if args.archive and not args.check:raise ValueError('--archive requires --check')
    manifest=check(archive=args.archive) if args.check else prepare()
    print(json.dumps(dict(verified='bundle' if args.check else 'prepared',files=len(manifest['files']),
                         runtime_activated=False,manifest=MANIFEST)))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,tarfile.TarError) as error:
        print(str(error),file=sys.stderr);sys.exit(1)
