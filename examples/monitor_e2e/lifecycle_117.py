"""117-only diagnostic launcher; no product runtime or kernel changes."""
import sys

import run_117 as launcher

original_output = launcher.output


def diagnostic_output(*args, **kwargs):
    if args[:2] == ('docker', 'create'):
        args = list(args)
        if '--strace' in sys.argv:
            args[2:2] = ['-v', str(launcher.ROOT / 'diagnostic-bin')+':/opt/diagnostic-tools:ro']
        pairs = '1' if '--one-pair' in sys.argv else ('3' if '--strace' in sys.argv or '--kernel-stacks' in sys.argv else '10')
        args[2:2] = ['-e', 'ALLOX_LIFECYCLE_STRACE='+('1' if '--strace' in sys.argv else '0'),
                     '-e', 'ALLOX_LIFECYCLE_STACKS='+('1' if '--kernel-stacks' in sys.argv else '0'),
                     '-e', 'ALLOX_LIFECYCLE_PAIRS='+pairs]
        args[-1] = args[-1].replace('/monitor_e2e/guest.py', '/monitor_e2e/lifecycle_guest.py')
    return original_output(*args, **kwargs)


launcher.output = diagnostic_output
sys.argv.append('--local-cpu-llm')  # Existing fixture metadata only; NO inference.
raise SystemExit(launcher.main())
