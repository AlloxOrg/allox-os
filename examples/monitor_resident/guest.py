"""Same 12 actions; off, same-build one-shot, resident; all original checks."""
import importlib.util
import json
import os
import threading
from pathlib import Path

from allox_system_effects.resident import shutdown_resident_observers, resident_observer_status

spec = importlib.util.spec_from_file_location('e2e_fixture', '/allox/examples/monitor_e2e/guest.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
original_config = fixture.effects_config
resident = False


def config():
    result = original_config()
    result.update(observer_path='/opt/resident-observer/deployment-observe',
                  verifier_path='/opt/resident-observer/deployment-observe-verify',
                  resident_observer=resident)
    return result


fixture.effects_config = config


def run(label, mode, server):
    global resident
    resident = mode == 'resident'
    before = resident_observer_status()
    row = fixture.run(label, 'off' if mode == 'off' else 'batch', server)
    row['observer_lifetime'] = mode
    row['resident_before'] = before
    if resident and row['passed']:
        receipt = row['process']['feature_runs']['system-effects']['resident_observer']
        row['resident'] = receipt
    return row


def main():
    loop = fixture.prepare_btrfs()
    server = fixture.ThreadingHTTPServer(('127.0.0.1', 0), fixture.Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    report = {'passed': False, 'scope': 'same 12 actions; no LLM; idle resident hooks remain in off controls',
              'cases': [], 'cold_warmup': None, 'shutdown': None}
    try:
        smoke = os.environ.get('ALLOX_RESIDENT_SMOKE') == '1'
        if smoke:
            report['cold_warmup'] = run('cold-resident', 'resident', server)
            if not report['cold_warmup']['passed']:
                raise RuntimeError('cold resident probe failed')
        # A resident hook can prevent the last-one-shot ftrace trampoline from
        # being unregistered. Never benchmark one-shot while that hook is idle.
        for phase, on in (('baseline', 'one-shot'), ('resident', 'resident')):
            for index in range(2 if smoke else 10):
                for mode in ((on, 'off') if index % 2 == 0 else ('off', on)):
                    row = run(phase+'-'+str(index)+'-'+mode, mode, server)
                    row['comparison_phase'] = phase
                    report['cases'].append(row)
                    if not row['passed']:
                        raise RuntimeError('comparison failed: '+row['label'])
        identities = [row['resident']['resident_objects'] for row in report['cases'] if 'resident' in row]
        report['objects_unchanged'] = bool(identities) and all(identity == identities[0] for identity in identities)
        report['comparison_valid'] = smoke or all(not row['resident_before']['present']
            for row in report['cases'] if row['comparison_phase'] == 'baseline')
        report['cold_start_included'] = not smoke
        report['passed'] = report['objects_unchanged'] and report['comparison_valid'] and all(row['passed'] for row in report['cases'])
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc)[:400])
    finally:
        report['shutdown'] = shutdown_resident_observers()
        if report['shutdown'] and (report['shutdown']['exit_code'] != 0 or report['shutdown']['poisoned']):
            report['passed'] = False
        (Path('/results') / 'report.json').write_text(json.dumps(report, indent=2))
        print('RESIDENT_REPORT', json.dumps({'passed': report['passed'], 'error': report.get('error'),
                                             'shutdown': report['shutdown']}), flush=True)
        server.shutdown()
        server.server_close()
        fixture.subprocess.run(['umount', '/state'], check=False)
        fixture.subprocess.run(['losetup', '-d', loop], check=False)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
