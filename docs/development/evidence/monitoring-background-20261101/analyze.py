"""Compute matched mode differences, keeping flush/teardown in the reported windows."""
import json
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def stats(rows, mode, getter):
    off = [getter(p['off']) for p in rows]
    values = [getter(p[mode]) for p in rows]
    delta = [b-a for a, b in zip(off, values)]
    mean = statistics.mean(delta)
    margin = 2.262157*statistics.stdev(delta)/math.sqrt(len(delta))
    return {'off_mean': statistics.mean(off), 'on_mean': statistics.mean(values),
        'extra_mean': mean, 'extra_95ci': [mean-margin, mean+margin],
        'ratio_of_means_percent': (statistics.mean(values)/statistics.mean(off)-1)*100}


def main():
    guest = json.loads((ROOT/'guest-benchmark.json').read_text())
    assert guest['completed'] and len(guest['pairs']) == 10
    summary = {'guest': {}}
    getters = {'session_ms': lambda r: r['wall_ns']/1e6,
        'worker_ms': lambda r: r['whole_worker_wall_ns']/1e6,
        'tool_loop_including_flush_ms': lambda r: r['workload']['wall_ns']/1e6,
        'loop_before_flush_ms': lambda r: r['workload']['loop_before_flush_ns']/1e6,
        'setup_ms': lambda r: r['setup_ns']/1e6,
        'sdk_import_init_ms': lambda r: r['workload']['sdk_import_init_ns']/1e6,
        'cpu_ms': lambda r: r['cpu_seconds']*1000,
        'daemon_peak_rss_kib': lambda r: r['daemon_peak_rss_kib'],
        'workload_peak_rss_kib': lambda r: r['workload']['peak_rss_kib']}
    for mode in ('legacy', 'sync', 'background', 'background64'):
        rows = guest['pairs']
        summary['guest'][mode] = {name: stats(rows, mode, getter) for name, getter in getters.items()}
        summary['guest'][mode]['all_checks_passed'] = all(all(p[mode]['checks'].values()) for p in rows)
        summary['guest'][mode]['committed_transactions_mean'] = statistics.mean(
            p[mode]['collector']['committed_transactions_this_process'] for p in rows)
        summary['guest'][mode]['events'] = sum(p[mode]['collector']['accepted_events_this_process'] for p in rows)
    for mode in ('sync', 'background'):
        micro = json.loads((ROOT/f'micro-{mode}.json').read_text())
        assert len(micro['pairs']) == 10
        summary['micro_'+mode] = stats(micro['pairs'], 'on', lambda r: r['wall_ns']/1e6)
    summary['direct_background64_minus_legacy'] = {
        name: stats([{'off': p['legacy'], 'on': p['background64']} for p in guest['pairs']], 'on', getter)
        for name, getter in getters.items()}
    summary['direct_background64_minus_background32'] = {
        name: stats([{'off': p['background'], 'on': p['background64']} for p in guest['pairs']], 'on', getter)
        for name, getter in getters.items()}
    (ROOT/'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
