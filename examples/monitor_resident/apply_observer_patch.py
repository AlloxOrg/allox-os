"""Apply the GPL native patch only to its byte-identified Observer base."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

PATCHES = Path(__file__).resolve().parents[2] / 'plugins/system-effects/observer'


def apply(observer):
    observer = Path(observer).resolve(strict=True)
    manifest = json.loads((PATCHES / 'resident-v1.json').read_text())
    actual = {item['path']: hashlib.sha256((observer / item['path']).read_bytes()).hexdigest()
              for item in manifest['files']}
    if all(actual[item['path']] == item['result_sha256'] for item in manifest['files']):
        print('RESIDENT_PATCH_ALREADY_MATCHES', flush=True)
        return
    if not all(actual[item['path']] == item['base_sha256'] for item in manifest['files']):
        raise RuntimeError('Observer base differs; use a separate matching source copy')
    subprocess.run(['patch', '--batch', '--fuzz=0', '-p'+str(manifest['strip']),
                    '-i', str(PATCHES / manifest['patch'])], cwd=observer, check=True)
    for item in manifest['files']:
        if hashlib.sha256((observer / item['path']).read_bytes()).hexdigest() != item['result_sha256']:
            raise RuntimeError('Patched Observer file digest differs: '+item['path'])
    print('RESIDENT_PATCH_VERIFIED', flush=True)


if __name__ == '__main__':
    apply(sys.argv[1])
