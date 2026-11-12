"""117: 10 rotated groups, 40 actions; changed profiling durability is explicit."""
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-actions-20261112')
SOURCE = ROOT / 'source'
(ROOT / 'tmp').mkdir(exist_ok=True)
os.environ.update(PYTHONPATH=':'.join(map(str, [SOURCE / 'src', SOURCE / 'tests',
    *list((SOURCE / 'plugins').glob('*/src'))])), TMPDIR=str(ROOT / 'tmp'), PYTHONDONTWRITEBYTECODE='1')


def cpus():
    online = set()
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        first, _, last = part.partition('-')
        online.update(range(int(first), int(last or first) + 1))
    selected = sorted(online)[-24:]
    assert len(selected) == 24
    return ','.join(map(str, selected))


result = {'scope': '40 synthetic actions, each 6 SDK events, socket transport; XFS WAL/FULL',
    'policy_change': 'batch8/time50ms does NOT guarantee durability after each action',
    'all_modes_final_flush_included': True, 'pairs': []}
for index in range(10):
    pair = {}
    modes = ['off', 'background', 'queued', 'actions']
    modes = modes[index % 4:] + modes[:index % 4]
    for mode in modes:
        affinity = cpus()
        command = ['taskset', '-c', affinity, sys.executable,
            str(SOURCE / 'examples/monitoring_benchmark.py'), '--worker', 'off' if mode == 'off' else 'on',
            '--steps', '40', '--batch-size', '32', '--transport', 'socket', '--turns',
            '--delivery', 'sync' if mode == 'off' else ('queued' if mode == 'actions' else mode)]
        if mode == 'actions':
            command += ['--action-batch-size', '8', '--action-flush-interval', '.05']
        row = json.loads(subprocess.check_output(command, text=True))
        row['cpus'] = affinity
        pair[mode] = row
        print(index, mode, round(row['wall_ns']/1e6, 2), flush=True)
    result['pairs'].append(pair)
    (ROOT / 'benchmark.json').write_text(json.dumps(result, indent=2))
result['medians'] = {}
for mode in ('off', 'background', 'queued', 'actions'):
    rows = [p[mode] for p in result['pairs']]
    result['medians'][mode] = {
        **{k.replace('_ns', '_ms'): statistics.median(r[k]/1e6 for r in rows)
           for k in ('wall_ns', 'foreground_ns', 'final_flush_ns', 'step_p95_ns')},
        'cpu_ms': statistics.median(r['cpu_seconds']*1000 for r in rows),
        'transactions': statistics.median(r['collector']['committed_transactions_this_process']-1
                                          for r in rows) if mode != 'off' else 0,
        'all_reopen_oracles': all(r.get('reopen_oracle', True) for r in rows),
        'all_delivery_complete': all(r['monitor']['complete'] for r in rows) if mode != 'off' else True}
(ROOT / 'benchmark.json').write_text(json.dumps(result, indent=2))
print('SUMMARY', json.dumps(result['medians'], indent=2), flush=True)
