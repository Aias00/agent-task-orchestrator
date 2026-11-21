"""Offline subprocess fixture; never invokes an agent provider."""
import json
from pathlib import Path
import sys
import time

mode, output = sys.argv[1:3]
prompt = sys.stdin.read()
if mode == 'planner':
    result = dict(decision='stage', reason='Create the missing fixture file',
                  tasks=[dict(id='result', prompt='create result', depends_on=[], covers=['result'])],
                  retry_after_seconds=1)
elif mode == 'worker':
    if 'sleep-fixture' in prompt:
        Path('fixture-entered').write_text('running')
        time.sleep(30)
    Path('result').write_text('fixture output')
    result = dict(status='complete', summary='fixture complete', next_step='', blocker='', retry_after_seconds=1)
else:
    raise SystemExit('unknown fixture mode')
Path(output).write_text(json.dumps(result))
