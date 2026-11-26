"""Synthetic tool construction for host injection tests; never a live profile."""
import json
from pathlib import Path
import sys

from ato_pi import pi_toolchain as policy


def tools_fixture(base):
    host = Path(base)/'host tools with spaces'
    host.mkdir()
    node = host/'node'
    node.write_text('#!'+sys.executable+'''\nimport json,os,runpy,sys
if sys.argv[1:]==['--version']:
    print('v24.99.0');raise SystemExit(0)
entry=sys.argv.pop(1)
runpy.run_path(entry,run_name='__main__')
''')
    node.chmod(0o700)
    root = host/'pi-package'
    (root/'dist').mkdir(parents=True)
    pi = root/'dist/cli.js'
    pi.write_text('''import json,os,sys
from pathlib import Path
if '--version' in sys.argv:
    print('1.5.2');raise SystemExit(0)
Path(os.environ['CTASK_PI_RUNTIME'],'native-argv.json').write_text(json.dumps({
    'argv':sys.argv,'environment':dict(os.environ)}))
''')
    pi.chmod(0o700)
    (root/'package.json').write_text(json.dumps(dict(name='synthetic-pi',version='1.5.2')))
    release = dict(id='synthetic-pi-1.5.2', name='synthetic-pi', version='1.5.2',
        entry='dist/cli.js', entry_sha256=policy.file_sha256(pi), package_sha256=policy.package_sha256(root))
    data = policy.registry()
    data['pi_releases'] = [release]
    tools = policy.Toolchain(node, pi, root, 'v24.99.0', policy.file_sha256(node),
        release['id'], release['version'], release['entry_sha256'], release['package_sha256'])
    return tools, data
