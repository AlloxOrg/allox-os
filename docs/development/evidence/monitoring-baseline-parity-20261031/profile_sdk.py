"""Profile one post-batching SDK trial on 117, separate from paired timings."""
import cProfile
import io
import json
import os
import pstats
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitoring-parity-20261031-v1/source')
ids = []
for part in Path('/sys/devices/system/cpu/online').read_text().strip().split(','):
    bounds = list(map(int, part.split('-')))
    ids.extend(range(bounds[0], bounds[-1]+1))
os.sched_setaffinity(0, sorted(set(ids))[-24:])
sys.path[:0] = [str(ROOT/'examples'), str(ROOT/'src'), str(ROOT/'plugins/agent-monitor/src')]
from monitoring_benchmark import worker

profiler = cProfile.Profile()
profiler.enable()
result = worker(True, 40, 32)
profiler.disable()
out = ROOT/'final-validation'
profiler.dump_stats(str(out/'batch-sdk.prof'))
text = io.StringIO()
pstats.Stats(profiler, stream=text).sort_stats('cumulative').print_stats(35)
(out/'batch-sdk-profile.txt').write_text(text.getvalue())
(out/'batch-sdk-profile-metrics.json').write_text(json.dumps(result, indent=2))
print(text.getvalue())
