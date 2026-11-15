"""Summarize completed integration and paired measurements without raw payloads."""
import hashlib
import json
import statistics
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-e2e-20261114')
paths = [Path(value).resolve(strict=True) for value in sys.argv[1:]]
assert len(paths) == 2 and all(path.parent == ROOT for path in paths)
integration, benchmark = [json.loads((path / 'report.json').read_text()) for path in paths]
assert integration['passed'] and benchmark['passed']
agent, = [case for case in integration['cases'] if case['real_llm']]
assert len(agent['model_requests']) >= 2 and agent['agent_command_count'] == 1
rows = []
for case in benchmark['cases']:
    assert case['passed'] and case['proof']['steps'] == 12 and not case['real_llm']
    rows.append({'label': case['label'], 'mode': case['mode'], 'wall_ms': case['wall_ns']/1e6,
                 'task_including_sdk_close_ms': case['proof']['wall_ns_including_close']/1e6,
                 'sdk_events': case['proof']['health']['acknowledged_events'],
                 'transactions': case.get('collector', {}).get('committed_transactions_this_process', 0)})
assert len(rows) == 20
runtime = json.loads((paths[1] / 'runtime.json').read_text())
assert runtime['gateway_requests'] == []
statistics_rows = {}
for metric in ('wall_ms', 'task_including_sdk_close_ms'):
    off = [row[metric] for row in rows if row['mode'] == 'off']
    on = [row[metric] for row in rows if row['mode'] == 'batch']
    pairs = [next(row[metric] for row in rows if row['label'] == 'pair-'+str(index)+'-batch')
             - next(row[metric] for row in rows if row['label'] == 'pair-'+str(index)+'-off')
             for index in range(10)]
    statistics_rows[metric] = {'off_median': statistics.median(off), 'on_median': statistics.median(on),
        'paired_delta_median': statistics.median(pairs), 'on_min': min(on), 'on_max': max(on),
        'ratio_of_medians': statistics.median(on)/statistics.median(off)}
summary = {'integration_attempt': str(paths[0]), 'benchmark_attempt': str(paths[1]),
    'passed': True, 'model': 'Qwen3.5-4B-CPU-Test', 'prompt': 'test-only compact system prompt',
    'llm_http_statuses': [row['http_status'] for row in agent['model_requests']],
    'agent_commands': agent['agent_command_count'], 'spans': agent['profile']['spans'],
    'sdk_events': agent['proof']['health']['acknowledged_events'],
    'effects': agent['correlation']['system-effects.jsonl']['event_count'],
    'lifecycle': agent['correlation']['process-lifecycle.jsonl']['event_count'],
    'global_evidence_status': agent['evidence_health']['overall_status'],
    'lifecycle_status': agent['evidence_health']['lifecycle']['status'],
    'effects_dropped': agent['evidence_health']['effects']['event_dropped'],
    'lifecycle_dropped': agent['evidence_health']['lifecycle']['event_dropped'],
    'kernel_sha256': hashlib.sha256((ROOT / 'linux-6.12.107/arch/x86/boot/bzImage').read_bytes()).hexdigest(),
    'benchmark': {'pairs': 10, 'model_calls': 0, 'statistics': statistics_rows, 'rows': rows},
    'timing_scope': 'wall: Core start through observer finalization and framework close; task: action loop through SDK close; excludes VM boot, DB init and posthoc queries',
    'caveats': ['partial lifecycle enrollment, not whole-tree completeness',
                'shared 117 disk/cache contention is not controlled',
                'full monitoring off/on, not an isolated eBPF or SDK microbenchmark']}
(ROOT / 'summary.json').write_text(json.dumps(summary, indent=2))
print(json.dumps({key: value for key, value in summary.items() if key != 'benchmark'}), flush=True)
print(json.dumps(statistics_rows), flush=True)
