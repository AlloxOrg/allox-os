"""117 paired measurements, including final fsync; each worker gets fresh affinity."""
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-async-20261112')
SOURCE = ROOT / 'source'
paths = [SOURCE / 'src', SOURCE / 'tests', *list((SOURCE / 'plugins').glob('*/src'))]
(ROOT / 'tmp').mkdir(exist_ok=True)
os.environ.update(PYTHONPATH=':'.join(map(str, paths)), TMPDIR=str(ROOT / 'tmp'),
                  PYTHONDONTWRITEBYTECODE='1')


def affinity():
    online = set()
    for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
        first, _, last = part.partition('-')
        online.update(range(int(first), int(last or first) + 1))
    cpus = sorted(online)[-24:]
    assert len(cpus) == 24
    return ','.join(map(str, cpus))


result = {'scope': '40 synthetic CPU+1ms-wait calls, batch32; XFS FULL WAL; not real LLM/eBPF',
          'groups': 10, 'scenarios': []}
for transport, turns in [('direct', False), ('direct', True), ('socket', False), ('socket', True)]:
    scenario = {'transport': transport, 'per_action_turn': turns, 'pairs': []}
    for index in range(10):
        pair = {}
        modes = ['off', 'sync', 'background', 'queued']
        modes = modes[index % 4:] + modes[:index % 4]
        for mode in modes:
            cpus = affinity()
            command = ['taskset', '-c', cpus, sys.executable,
                str(SOURCE / 'examples/monitoring_benchmark.py'), '--worker', 'off' if mode == 'off' else 'on',
                '--steps', '40', '--batch-size', '32', '--delivery', 'sync' if mode == 'off' else mode,
                '--transport', transport, *(['--turns'] if turns else [])]
            pair[mode] = json.loads(subprocess.check_output(command, text=True))
            pair[mode]['cpus'] = cpus
            print(transport, turns, index, mode, round(pair[mode]['wall_ns']/1e6, 3), flush=True)
        scenario['pairs'].append(pair)
        (ROOT / 'benchmark-progress.json').write_text(json.dumps(result | {'active': scenario}, indent=2))
    scenario['medians'] = {}
    for mode in ('off', 'sync', 'background', 'queued'):
        rows = [p[mode] for p in scenario['pairs']]
        scenario['medians'][mode] = {
            **{key.replace('_ns', '_ms'): statistics.median(r[key]/1e6 for r in rows)
               for key in ('wall_ns', 'foreground_ns', 'final_flush_ns', 'step_p95_ns')},
            'cpu_ms': statistics.median(r['cpu_seconds']*1000 for r in rows),
            'transactions': statistics.median(r['collector']['committed_transactions_this_process']
                - (1 if transport == 'socket' else 0) for r in rows) if mode != 'off' else 0,
            'all_reopen_oracles': all(r.get('reopen_oracle', True) for r in rows),
            'paired_overhead_percent': statistics.median((p[mode]['wall_ns']/p['off']['wall_ns']-1)*100
                for p in scenario['pairs'])}
    result['scenarios'].append(scenario)
    (ROOT / 'benchmark.json').write_text(json.dumps(result, indent=2))
print('SUMMARY', json.dumps([{'transport': s['transport'], 'per_action_turn': s['per_action_turn'],
                            'medians': s['medians']} for s in result['scenarios']], indent=2), flush=True)
