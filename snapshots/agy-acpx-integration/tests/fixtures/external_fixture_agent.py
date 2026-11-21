"""Real subprocess performing a small local file edit, no provider/network."""
import json
from pathlib import Path
import sys
import time

prompt = sys.stdin.read()
if 'sleep-fixture' in prompt:
    Path('fixture-entered').write_text('entered')
    time.sleep(30)
with Path('fixture-admissions').open('a') as stream:
    stream.write('one\n')
Path('result').write_text('wrong fixture output' if 'wrong-fixture' in prompt else 'fixture output')
Path(sys.argv[1]).write_text(json.dumps(dict(status='complete', summary='fixture edit returned',
    next_step='', blocker='', retry_after_seconds=1)))
