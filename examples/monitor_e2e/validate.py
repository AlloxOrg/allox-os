"""Reuse the current 117 regression checks and validate this integration harness."""
import runpy
import sys
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-e2e-20261114')
sys.argv = [sys.argv[0], str(ROOT)]
checks = runpy.run_path(str(Path(__file__).parents[1] / 'monitor_async/validate.py'))
checks['run']([str(checks['BIN'] / 'ruff'), 'check', '--select', 'E4,E7,E9,F',
               'examples/monitor_e2e'])
