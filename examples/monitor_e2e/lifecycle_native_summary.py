"""Join native syscall elapsed times to the Python FINISH interval."""
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
assert root.parent == Path('/data/hy/Allox/monitor-e2e-20261114')
report = json.loads((root / 'report.json').read_text())
assert report['passed']
rows = []
for case in report['cases']:
    if case['mode'] == 'off':
        continue
    phases = json.loads((root / case['label'] / 'phases.json').read_text())
    finish, = [entry for entry in phases if entry['name'] == 'observer._receive.FINISHED']
    lower = finish['wall_epoch_ns']/1e9
    upper = lower+(finish['end_ns']-finish['start_ns'])/1e9
    log, = (root / case['label'] / 'audit').glob('*/observer-syscalls.log')
    pending, descriptors, calls = {}, {}, []
    for line in log.read_text().splitlines():
        match = re.match(r'^(\d+)\s+([\d.]+)\s+(.*)$', line)
        if not match:
            continue
        tid, timestamp, text = int(match[1]), float(match[2]), match[3]
        if '<unfinished ...>' in text:
            pending[tid] = (timestamp, text.split('<unfinished ...>')[0])
            continue
        if text.startswith('<... '):
            resumed = re.match(r'<\.\.\. \w+ resumed>(.*)', text)
            if not resumed or tid not in pending:
                continue
            timestamp, prefix = pending.pop(tid)
            text = prefix+resumed[1]
        duration = re.search(r'<([\d.]+)>$', text)
        operation = re.match(r'(\w+)\(([^,)]*)', text)
        if not duration or not operation:
            continue
        name, arg = operation.groups()
        elapsed = float(duration[1])*1000
        kind = name
        if name == 'bpf':
            kind = 'bpf:'+arg
            returned = re.search(r'\)\s+=\s+(\d+)\s+<', text)
            if returned and (arg in ('BPF_MAP_CREATE', 'BPF_PROG_LOAD', 'BPF_BTF_LOAD',
                                     'BPF_LINK_CREATE', 'BPF_RAW_TRACEPOINT_OPEN') or '_GET_FD_BY_ID' in arg):
                descriptors[int(returned[1])] = arg
        if name == 'close' and arg.isdigit():
            kind += ':'+descriptors.pop(int(arg), 'other')
        calls.append({'thread': tid, 'start_epoch_s': timestamp, 'duration_ms': elapsed,
                      'kind': kind, 'in_finish': lower <= timestamp < upper})
    grouped = defaultdict(list)
    for call in calls:
        if call['in_finish']:
            grouped[call['kind']].append(call['duration_ms'])
    statistics_rows = {kind: {'count': len(values), 'total_ms': sum(values), 'max_ms': max(values)}
                       for kind, values in grouped.items()}
    observation, = (root / case['label'] / 'audit').glob('*/system-effects/deployment-observation-report.json')
    native_telemetry = json.loads(observation.read_text())['diagnostic_performance']
    rows.append({'label': case['label'], 'python_finish_wait_ms': (upper-lower)*1000,
                 'finish_syscalls': statistics_rows,
                 'slowest_finish_calls': sorted([call for call in calls if call['in_finish']],
                    key=lambda call: call['duration_ms'], reverse=True)[:12],
                 'native_telemetry': native_telemetry})
keys = set().union(*(row['finish_syscalls'] for row in rows))
medians = {key: statistics.median(row['finish_syscalls'].get(key, {}).get('total_ms', 0) for row in rows)
           for key in sorted(keys)}
result = {'attempt': str(root), 'rows': rows, 'median_finish_syscall_total_ms': medians,
          'caveat': 'syscall durations include blocking, not CPU; across-thread totals can overlap'}
(root / 'native-summary.json').write_text(json.dumps(result, indent=2))
print(json.dumps({'median_finish_syscall_total_ms': medians,
                 'finish_wait_ms': [row['python_finish_wait_ms'] for row in rows]}), flush=True)
