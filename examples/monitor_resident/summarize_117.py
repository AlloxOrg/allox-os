"""Summarize complete staged comparisons; include cold run and shutdown cost."""
import json
import statistics
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
assert root.parent == Path('/data/hy/Allox/monitor-e2e-20261114')
report = json.loads((root / 'report.json').read_text())
runtime = json.loads((root / 'runtime.json').read_text())
assert report['passed'] and report['comparison_valid'] and report['cold_start_included']
assert len(report['cases']) == 40 and runtime['gateway_requests'] == []
rows, groups = [], {}
for row in report['cases']:
    assert row['passed'] and row['proof']['steps'] == 12 and not row['real_llm']
    rows.append({'label': row['label'], 'phase': row['comparison_phase'],
        'lifetime': row['observer_lifetime'], 'wall_ms': row['wall_ns']/1e6,
        'task_ms': row['proof']['wall_ns_including_close']/1e6,
        'sdk_events': row['proof']['health']['acknowledged_events'],
        'resident_before': row['resident_before'], 'resident': row.get('resident')})
for phase, on in (('baseline', 'one-shot'), ('resident', 'resident')):
    selected = [row for row in rows if row['phase'] == phase]
    for lifetime in ('off', on):
        sample = [row for row in selected if row['lifetime'] == lifetime]
        assert len(sample) == 10
        groups[phase+'/'+lifetime] = {metric: {'median': statistics.median(row[metric] for row in sample),
            'mean': statistics.mean(row[metric] for row in sample),
            'min': min(row[metric] for row in sample), 'max': max(row[metric] for row in sample)}
            for metric in ('wall_ms', 'task_ms')}
    pairs = [next(row['wall_ms'] for row in selected if row['label'] == phase+'-'+str(index)+'-'+on)
             - next(row['wall_ms'] for row in selected if row['label'] == phase+'-'+str(index)+'-off')
             for index in range(10)]
    groups[phase+'/paired-added-ms'] = {'median': statistics.median(pairs),
                                       'mean': statistics.mean(pairs), 'samples': pairs}
resident = [row for row in rows if row['lifetime'] == 'resident']
assert [row['resident']['kernel_objects_reused'] for row in resident] == [False]+[True]*9
assert len({row['resident']['worker_pid'] for row in resident}) == 1
assert all(row['resident']['resident_objects'] == resident[0]['resident']['resident_objects'] for row in resident)
assert not any(row['resident_before']['present'] for row in rows if row['phase'] == 'baseline')
assert all(row['resident_before']['present'] for row in rows if row['phase'] == 'resident' and row['lifetime'] == 'off')
original = groups['baseline/paired-added-ms']['median']
optimized = groups['resident/paired-added-ms']['median']
summary = {'attempt': str(root), 'passed': True, 'pairs_per_phase': 10, 'groups': groups,
    'paired_added_latency_reduction_percent': (original-optimized)/original*100,
    'direct_on_median_reduction_percent': (1-groups['resident/resident']['wall_ms']['median']
        /groups['baseline/one-shot']['wall_ms']['median'])*100,
    'cold_run_ms': resident[0]['wall_ms'],
    'warm_run_median_ms': statistics.median(row['wall_ms'] for row in resident[1:]),
    'shutdown': report['shutdown'],
    'resident_mean_including_shutdown_ms': (sum(row['wall_ms'] for row in resident)
        +report['shutdown']['shutdown_ns']/1e6)/10,
    'rows': rows, 'notes': ['baseline is measured BEFORE loading any resident probes',
        'resident off means SDK/collection disabled, resident hooks idle; not unloaded',
        'first cold run is INCLUDED among ten resident on samples; final teardown separately amortized',
        'host disk/cache/other workloads are not fully controlled; no tail-latency guarantee']}
(root / 'resident-summary.json').write_text(json.dumps(summary, indent=2))
print(json.dumps({key: value for key, value in summary.items() if key != 'rows'}), flush=True)
