"""Summarize paired samples; do not mistake mean ratios for ratios of means."""

import argparse
import json
import math
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def comparison(pairs, getter):
    off = [getter(p['off']) for p in pairs]
    on = [getter(p['on']) for p in pairs]
    delta = [b-a for a, b in zip(off, on)]
    ratios = [(b/a-1)*100 for a, b in zip(off, on)]
    mean_delta = statistics.mean(delta)
    half_width = 2.262157*statistics.stdev(delta)/math.sqrt(len(delta))
    return {'off_mean': statistics.mean(off), 'on_mean': statistics.mean(on),
        'off_median': statistics.median(off), 'on_median': statistics.median(on),
        'mean_paired_delta': mean_delta, 'paired_delta_95ci': [mean_delta-half_width, mean_delta+half_width],
        'ratio_of_means': statistics.mean(on)/statistics.mean(off),
        'median_paired_overhead_percent': statistics.median(ratios),
        'paired_overhead_percent_min_max': [min(ratios), max(ratios)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json-only', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    micro = json.loads((ROOT/'micro-benchmark.json').read_text())
    guest = json.loads((ROOT/'guest-benchmark.json').read_text())
    if len(micro['pairs']) != 10 or len(guest['pairs']) != 10 or not guest.get('completed'):
        raise SystemExit('refuse to summarize incomplete trials')
    result = {}
    for label, data in [('sdk_micro', micro), ('guest_session', guest)]:
        pairs = data['pairs']
        memory_key = 'peak_rss_kib' if label == 'sdk_micro' else 'daemon_peak_rss_kib'
        result[label] = {
            'repetitions': len(pairs),
            'wall_ms': comparison(pairs, lambda r: r['wall_ns']/1e6),
            'cpu_ms': comparison(pairs, lambda r: r['cpu_seconds']*1000),
            'process_peak_rss_kib': comparison(pairs, lambda r: r[memory_key]),
            'all_submitted_events_acknowledged': all(
                (p['on']['monitor'] if label == 'sdk_micro' else p['on']['workload']['sdk_health'])['failed_events'] == 0
                and p['on']['collector']['accepted_events_this_process'] == (160 if label == 'sdk_micro' else 162)
                for p in pairs),
        }
        if label == 'guest_session':
            result[label]['tool_batch_ms'] = comparison(pairs, lambda r: r['workload']['wall_ns']/1e6)
            result[label]['whole_worker_ms'] = comparison(pairs, lambda r: r['whole_worker_wall_ns']/1e6)
            result[label]['setup_ms'] = comparison(pairs, lambda r: r['setup_ns']/1e6)
            result[label]['workload_peak_rss_kib'] = comparison(pairs, lambda r: r['workload']['peak_rss_kib'])
            result[label]['collector_filesystem'] = data['collector_filesystem']
            result[label]['all_monitor_checks_passed'] = all(all(p['on']['checks'].values()) for p in pairs)
            result[label]['total_accepted_events'] = sum(p['on']['collector']['accepted_events_this_process'] for p in pairs)
        if not args.json_only:
            print(label)
            for index, pair in enumerate(pairs, 1):
                print(f"{index:2d} off={pair['off']['wall_ns']/1e6:.3f}ms on={pair['on']['wall_ns']/1e6:.3f}ms overhead={pair['wall_overhead_percent']:.2f}%")
    serialized = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(serialized, encoding='utf-8')
    print(serialized)


if __name__ == '__main__':
    main()
