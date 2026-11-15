"""Summarize stacks of only the owned Observer during FINISH."""
import json
import re
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
assert root.parent == Path('/data/hy/Allox/monitor-e2e-20261114')
report = json.loads((root / 'report.json').read_text())
assert report['passed']
rows, combined = [], Counter()
for case in report['cases']:
    if case['mode'] == 'off':
        continue
    phases = json.loads((root / case['label'] / 'phases.json').read_text())
    finish, = [entry for entry in phases if entry['name'] == 'observer._receive.FINISHED']
    counts = Counter()
    for stack, count in finish['kernel_stacks'].items():
        names = [re.sub(r'^\[<[^>]+>\]\s*', '', line).split('+')[0] for line in stack.splitlines()]
        counts[' > '.join(names)] += count
    combined.update(counts)
    rows.append({'label': case['label'], 'finish_ms': (finish['end_ns']-finish['start_ns'])/1e6,
                 'samples': dict(counts)})
result = {'attempt': str(root), 'passed': True, 'rows': rows, 'combined_samples': dict(combined),
          'note': '5ms nominal sampling interval; sample frequencies are not exact wall/CPU proportions'}
(root / 'stack-summary.json').write_text(json.dumps(result, indent=2))
print(json.dumps({'finish_ms': [row['finish_ms'] for row in rows], 'top_stacks': combined.most_common(6)}), flush=True)
