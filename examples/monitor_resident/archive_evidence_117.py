"""Package compact, non-secret reports; raw WALs remain on the owned 117 run."""
import hashlib
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path('/data/hy/Allox/monitor-resident-20261115')
OLD = Path('/data/hy/Allox/monitor-e2e-20261114')
ATTEMPT = OLD / 'attempt-be59046548'
SAFETY = Path(sys.argv[1]).resolve(strict=True) if len(sys.argv) > 1 else OLD / 'attempt-5fbbc47c6a'
assert SAFETY.parent == OLD and SAFETY.name.startswith('attempt-')

files = [(ATTEMPT / name, name) for name in ('report.json', 'runtime.json',
    'resident-summary.json', 'offline-report.json', 'postverification.json')]
files += [(SAFETY / 'report.json', 'safety-report.json'),
          (SAFETY / 'runtime.json', 'safety-runtime.json')]
files += [(ROOT / name, name) for name in ('build.log', 'validation-final.log')]
identities = {}
for path in (ROOT / 'bin/deployment-observe', ROOT / 'bin/deployment-observe-verify',
             OLD / 'bin/system_effects.bpf.o', OLD / 'bin/process_lifecycle.bpf.o'):
    if path.is_file():
        identities[str(path)] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                 'size': path.stat().st_size}
metadata = {'benchmark': str(ATTEMPT), 'safety': str(SAFETY), 'files': identities,
            'raw_artifacts_retained_on_117': True}
(ROOT / 'build-identities.json').write_text(json.dumps(metadata, indent=2))
files.append((ROOT / 'build-identities.json', 'build-identities.json'))
with tarfile.open(ROOT / 'evidence.tar.gz', 'w:gz') as archive:
    for path, name in files:
        archive.add(path, arcname=name, recursive=False)
print('EVIDENCE_PACKAGED', len(files), flush=True)
