"""Read isolated diagnostic results; never sum nested wall timers as additive."""
import json
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
assert root.parent == Path('/data/hy/Allox/monitor-e2e-20261114')
report = json.loads((root / 'report.json').read_text())
rows = []
for case in report['cases']:
    assert case['passed']
    entries = json.loads((root / case['label'] / 'phases.json').read_text())
    boundary, = [entry for entry in entries if entry['name'] == 'timed_boundary']
    start = boundary['start_ns']
    end = start + case['wall_ns']
    entries = [entry for entry in entries if 'end_ns' in entry and start <= entry['start_ns'] <= end]
    launch, = [entry for entry in entries if entry['name'] == 'core.start']
    drained = min((entry for entry in entries if entry['name'] == 'cgroup.wait_empty'),
                  key=lambda entry: entry['start_ns'])
    metrics = {'wall_ms': case['wall_ns']/1e6,
        'task_ms': case['proof']['wall_ns_including_close']/1e6,
        'pre_start_ms': (launch['start_ns']-start)/1e6,
        'launch_ms': (launch['end_ns']-launch['start_ns'])/1e6,
        'execution_until_root_exit_ms': (drained['start_ns']-launch['end_ns'])/1e6,
        'post_exit_ms': (end-drained['start_ns'])/1e6}
    groups = defaultdict(list)
    for entry in entries:
        groups[entry['name']].append(entry)
    methods = {name: {'calls': len(items),
        'wall_ms': sum(item['end_ns']-item['start_ns'] for item in items)/1e6,
        'thread_cpu_ms': sum(item['cpu_ns'] for item in items)/1e6}
        for name, items in groups.items()}
    syscalls = {}
    for kind in ('observer', 'verifier'):
        paths = list((root / case['label'] / 'audit').glob('*/'+kind+'-syscalls.log'))
        if not paths:
            continue
        calls = defaultdict(list)
        for line in paths[0].read_text().splitlines():
            duration = re.search(r'<([\d.]+)>$', line)
            if not duration:
                continue
            operation = re.search(r'(bpf|fsync|fdatasync)\(([^,]*)', line)
            if operation:
                name = operation[1]+(':'+operation[2] if operation[1] == 'bpf' else '')
                calls[name].append(float(duration[1])*1000)
        syscalls[kind] = {name: {'count': len(values), 'total_ms': sum(values), 'max_ms': max(values)}
                           for name, values in calls.items()}
    rows.append({'label': case['label'], 'mode': case['mode'], 'metrics': metrics,
                 'methods': methods, 'syscalls': syscalls})

summary = {'attempt': str(root), 'passed': report['passed'], 'strace': report['strace'], 'rows': rows,
           'notes': ['method timers are inclusive/nested, NOT additive',
                     'thread_cpu_ms covers only the Python calling thread, not Observer CPU',
                     'strace syscall durations may overlap across threads and add tracing overhead'], 'medians': {}}
for mode in ('off', 'batch'):
    selected = [row for row in rows if row['mode'] == mode]
    metrics = {key: statistics.median(row['metrics'][key] for row in selected) for key in selected[0]['metrics']}
    names = set().union(*(row['methods'] for row in selected))
    methods = {name: {key: statistics.median(row['methods'].get(name, {}).get(key, 0) for row in selected)
                      for key in ('calls', 'wall_ms', 'thread_cpu_ms')} for name in sorted(names)}
    summary['medians'][mode] = {'metrics': metrics, 'methods': methods}
(root / 'lifecycle-summary.json').write_text(json.dumps(summary, indent=2))
print(json.dumps({'passed': report['passed'], 'strace': report['strace'], 'medians': summary['medians']}), flush=True)
