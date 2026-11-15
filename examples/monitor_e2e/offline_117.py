"""Rejoin sealed evidence after the Guest and all Agent processes have exited."""
import json
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-e2e-20261114')
SOURCE = ROOT / 'source'
sys.path[:0] = [str(SOURCE / 'src'), str(SOURCE / 'plugins/trace-correlation/src')]
from allox_trace_correlation.correlate import Correlator  # noqa: E402

attempt = Path(sys.argv[1]).resolve(strict=True)
assert attempt.parent == ROOT and attempt.name.startswith('attempt-')
report = json.loads((attempt / 'report.json').read_text())
rows = []
for case in report['cases']:
    if case['mode'] == 'off' or not case.get('passed'):
        continue
    path = attempt / case['label']
    join = Correlator(path / 'monitor', path / 'audit')
    for stream, original in case['correlation'].items():
        count, offset = 0, 0
        while True:
            page = join.run(case['proof']['agent'], case['proof']['session'], case['run_id'],
                            stream=stream, offset=offset, limit=200)
            assert page['status'] == 'correlated'
            assert page['observer_evidence_status'] == original['observer_evidence_status'] == 'INCOMPLETE'
            assert page['turn'] == original['turn']
            count += len(page['events'])
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        rows.append({'case': case['label'], 'stream': stream, 'events': count,
                     'turn_status': page['turn']['status'], 'passed': count > 0})
result = {'guest_required': False, 'rows': rows,
          'limited_contract_passed': bool(rows) and all(row['passed'] for row in rows),
          'passed': report['passed'] and bool(rows) and all(row['passed'] for row in rows)}
(attempt / 'offline-report.json').write_text(json.dumps(result, indent=2))
print(json.dumps(result), flush=True)
