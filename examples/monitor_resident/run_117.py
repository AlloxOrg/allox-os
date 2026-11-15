"""Reuse the owned Kata/Guest launch fixture, not global host configuration."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-resident-20261115')
SOURCE = ROOT / 'source'
spec = importlib.util.spec_from_file_location('owned_launcher', SOURCE / 'examples/monitor_e2e/run_117.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
launcher.SOURCE = SOURCE
original = launcher.output


def output(*args, **kwargs):
    if args[:2] == ('docker', 'create'):
        args = list(args)
        args[2:2] = ['-v', str(ROOT / 'bin')+':/opt/resident-observer:ro',
                    '-e', 'ALLOX_RESIDENT_SMOKE='+('1' if '--smoke' in sys.argv else '0')]
        args = [str(ROOT / 'wheels')+':/dist:ro' if value == str(launcher.ROOT / 'wheels')+':/dist:ro'
                else value for value in args]
        entry = 'safety_guest.py' if '--safety' in sys.argv else 'guest.py'
        args[-1] = args[-1].replace('/monitor_e2e/guest.py', '/monitor_resident/'+entry)
    return original(*args, **kwargs)


launcher.output = output
sys.argv.append('--local-cpu-llm')  # No model requests; existing fixture metadata only.
raise SystemExit(launcher.main())
