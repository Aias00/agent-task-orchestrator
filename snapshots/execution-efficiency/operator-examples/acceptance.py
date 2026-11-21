import importlib.util,json,hashlib,sys
from pathlib import Path
OPS=Path(__file__).resolve().parent
R=json.loads((OPS/'continuation.json').read_text())
original=OPS/'original-acceptance.py'
assert hashlib.sha256(original.read_bytes()).hexdigest()==R['original_acceptance_sha256']
spec=importlib.util.spec_from_file_location('predecessor_acceptance',original)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.ROOT=Path('OPERATOR_SOURCE/ato-efficiency-requalification-20261120');m.OPS=OPS
# New explicitly documented host baseline; predecessor files/contract untouched.
m.BASE['installed_skill_sha256']=R['skill_transition']['current_sha256']
original_preserved=m.preserved
def preserved():
 original_preserved()
 for name,digest in R['predecessor_tests'].items():
  assert m.sha(m.ROOT/name)==digest,'predecessor test changed: '+name
m.preserved=preserved
if sys.argv[1]=='drift':
 preserved();m.tests('drift','test_profile_drift_evidence.py',8);preserved()
else:
 assert '--run-origin' not in sys.argv,'No fresh native/model trial authorized by this source-only continuation'
 m.main()
