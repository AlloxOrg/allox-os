"""Real kernel reuse/identity checks and an intentionally rejected stale FINISH."""
import hashlib
import json
from pathlib import Path

import guest as benchmark
from allox_system_effects import resident

from allox.workspace.daemon import WorkspaceService
from allox.workspace.store import WorkspaceError, WorkspaceStore

fixture = benchmark.fixture


def committed_audit(audit):
    manifest = json.loads((audit / 'artifact-manifest.json').read_text())
    return {entry['relative_path']: hashlib.sha256((audit / entry['relative_path']).read_bytes()).hexdigest()
            for entry in manifest['artifacts']}


def committed(path):
    audit, = path.glob('audit/*/system-effects')
    return committed_audit(audit)


def main():
    loop = fixture.prepare_btrfs()
    server = fixture.ThreadingHTTPServer(('127.0.0.1', 0), fixture.Fixture)
    fixture.threading.Thread(target=server.serve_forever, daemon=True).start()
    benchmark.resident = True
    report = {'passed': False, 'cases': [], 'checks': {}, 'scope': 'real kernel; no LLM; deliberate stale nonce fault'}
    service = None
    try:
        snapshots = {}
        for agent, session in (('agent-a', 's1'), ('agent-b', 's1'), ('agent-a', 's2')):
            label = agent+'-'+session
            row = fixture.run(label, 'batch', server, agent=agent, session=session)
            report['cases'].append(row)
            if not row['passed']:
                raise RuntimeError('identity workload failed')
            snapshots[label] = committed(Path('/results') / label)
            assert all(committed(Path('/results') / old) == snapshot for old, snapshot in snapshots.items())
        checks = report['checks']
        proofs = [row['process']['feature_runs']['system-effects']['resident_observer'] for row in report['cases']]
        checks['same_worker'] = len({proof['worker_pid'] for proof in proofs}) == 1
        checks['same_kernel_objects'] = all(proof['resident_objects'] == proofs[0]['resident_objects'] for proof in proofs)
        checks['old_committed_files_unchanged'] = True
        checks['distinct_run_ids'] = len({row['run_id'] for row in report['cases']}) == 3

        root = Path('/state/fault-store')
        root.mkdir()
        store = WorkspaceStore(root)
        store.initialize()
        store.create_session('fault-agent', 's')
        store.create_session('other-agent', 's')
        service = WorkspaceService(store, monitor_root=Path('/results/fault-monitor'), monitor_async_write=True,
            process_audit_root=Path('/results/fault-audit'), process_cgroup_root=Path('/sys/fs/cgroup/allox-resident-fault'),
            kill_session_processes_on_rollback=True, system_effects_config=benchmark.config())
        consecutive = []
        sequential_artifacts = {}
        for index in (1, 2):
            row = service.processes.start('fault-agent', 's', ['python3', '-c',
                'from pathlib import Path;Path("epoch-value").write_text("'+str(index)+'")'], timeout=60)
            assert service.processes._runs[row['run_id']]['done'].wait(90)
            row = service.processes.get('fault-agent', 's', row['run_id'])
            assert row['state'] == 'completed' and row['root_exit_code'] == 0
            assert (store.current('fault-agent', 's') / 'epoch-value').read_text() == str(index)
            proof = row['feature_runs']['system-effects']['resident_observer']
            consecutive.append(proof)
            audit = Path(row['audit_directory']) / 'system-effects'
            sequential_artifacts[str(audit)] = committed_audit(audit)
            assert all(committed_audit(Path(path)) == hashes for path, hashes in sequential_artifacts.items())
        report['consecutive_same_session'] = consecutive
        checks['same_session_epoch_advances'] = consecutive[1]['epoch'] == consecutive[0]['epoch']+1
        checks['same_session_new_run_binding'] = consecutive[0]['run_id'] != consecutive[1]['run_id']
        checks['same_session_same_worker_and_objects'] = all(proof['worker_pid'] == proofs[0]['worker_pid']
            and proof['resident_objects'] == proofs[0]['resident_objects'] for proof in consecutive)
        row = service.processes.start('fault-agent', 's', ['python3', '-c', 'import time; time.sleep(30)'], timeout=60)
        worker = resident._worker
        assert worker is not None and worker.active is not None
        old_nonce = consecutive[-1]['finish_nonce']
        assert old_nonce != worker.active.ready['finish_nonce']
        # Test-only corruption bypassing the Python FINISH guard. Native v2
        # must reject the old challenge too, and no run lease may be released.
        worker._send(json.dumps({'protocol_version': 2, 'command': 'FINISH',
            'run_id': row['run_id'], 'finish_nonce': old_nonce}).encode()+b'\n')
        assert service.processes._runs[row['run_id']]['done'].wait(60)
        failed = service.processes.get('fault-agent', 's', row['run_id'])
        checks['stale_nonce_failed_and_fenced'] = failed['state'] == 'failed' and failed['session_fenced']
        checks['worker_poisoned_and_exited'] = bool(worker.poisoned) and worker.process.poll() is not None
        checks['no_success_receipt'] = not (Path(failed['audit_directory']) / 'system-effects-receipt.json').exists()
        checks['fault_processes_cleared'] = not service.processes.cgroups.populated('fault-agent', 's')
        checks['new_run_refused_until_explicit_reset'] = False
        try:
            service.processes.start('other-agent', 's', ['python3', '-c',
                'from pathlib import Path;Path("should-not-exist").write_text("bad")'])
        except WorkspaceError:
            checks['new_run_refused_until_explicit_reset'] = True
        checks['refused_command_not_executed'] = not (store.current('other-agent', 's') / 'should-not-exist').exists()
        report['expected_failure'] = failed
        checks['previous_evidence_still_unchanged_after_fault'] = all(
            committed(Path('/results') / label) == snapshot for label, snapshot in snapshots.items())
        checks['same_session_evidence_unchanged_after_fault'] = all(
            committed_audit(Path(path)) == hashes for path, hashes in sequential_artifacts.items())
        report['passed'] = all(checks.values())
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc)[:500])
    finally:
        if service:
            service.close()
        report['fault_shutdown'] = resident.shutdown_resident_observers()
        (Path('/results') / 'report.json').write_text(json.dumps(report, indent=2))
        print('RESIDENT_SAFETY', json.dumps({'passed': report['passed'], 'checks': report['checks'],
                                            'error': report.get('error')}), flush=True)
        server.shutdown()
        server.server_close()
        fixture.subprocess.run(['umount', '/state'], check=False)
        fixture.subprocess.run(['losetup', '-d', loop], check=False)
    return 0 if report['passed'] else 1


raise SystemExit(main())
