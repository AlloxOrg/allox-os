"""Print diagnostic counters/reasons, not raw event payloads or credentials."""
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
assert root.is_relative_to(Path('/data/hy/Allox/monitor-e2e-20261114'))
def visit(value, path=''):
    if isinstance(value, dict):
        for key, item in value.items():
            visit(item, path+'.'+key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            visit(item, path+'.'+str(index))
    elif any(word in path.lower() for word in ('status', 'reason', 'loss', 'drop', 'fail', 'incomplete', 'unsupported', 'unknown', 'attempted', 'published')):
        print(path, json.dumps(value), flush=True)
for report in root.glob('audit/*/system-effects/deployment-observation-report.json'):
    visit(json.loads(report.read_text()))
