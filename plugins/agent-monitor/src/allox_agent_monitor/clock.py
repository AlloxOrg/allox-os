"""Comparable only within one Linux boot/time namespace and CLOCK_MONOTONIC."""
import sys
import time
import uuid
from pathlib import Path


def monotonic_domain():
    try:
        if sys.platform == 'linux' and 'CLOCK_MONOTONIC' in time.get_clock_info('perf_counter').implementation:
            boot = uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()).hex
            namespaces = Path('/proc/self/ns')
            names = {p.name for p in namespaces.iterdir()}
            if 'time' in names:
                namespace = str((namespaces/'time').stat().st_ino)
            elif 'time_for_children' not in names and {'pid', 'mnt', 'user', 'uts'} <= names:
                # Genuine procfs omits both entries when CONFIG_TIME_NS is disabled.
                # There is then only one CLOCK_MONOTONIC domain in this boot.
                namespace = 'no-timens'
            else:
                return uuid.uuid4().hex
            return f'linux-{boot}-{namespace}'
    except (OSError, ValueError):
        pass
    return uuid.uuid4().hex
