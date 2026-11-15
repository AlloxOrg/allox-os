"""Explicit protocol fixtures, not a claim of kernel BPF coverage."""
import json
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from allox_system_effects.config import SystemEffectsConfig
from allox_system_effects.resident import ResidentObserver
from allox_system_effects.runner import SystemEffectsRun

from allox.workspace.store import WorkspaceError


def executable(path, mode='ok'):
    source = '''
import json,sys,pathlib,time
assert sys.argv[1:]==['--resident-stdio']
sequence=0
for line in sys.stdin:
 value=json.loads(line)
 if value['command']=='SHUTDOWN':
  print(json.dumps({'resident_protocol':1,'status':'CLOSED'}),flush=True);break
 cfg=json.loads(pathlib.Path(value['config_path']).read_text())
 sequence+=1
 nonce=str(sequence)*64
 ready={'status':'READY','run_id':cfg['run_id'],'epoch':cfg['epoch'],'finish_nonce':nonce}
 print(json.dumps(ready),flush=True)
 command=json.loads(sys.stdin.readline())
 if MODE=='eof':break
 terminal={'status':'FINISHED','run_id':cfg['run_id'],'report_sha256':'a'*64,'artifact_manifest_sha256':'b'*64}
 print(json.dumps(terminal),flush=True)
 if MODE=='missing-ack':continue
 objects=[{'map_ids':[1],'program_ids':[2],'link_ids':[3]},
          {'map_ids':[4],'program_ids':[5],'link_ids':[6]}]
 if MODE=='changed-objects' and sequence>1:objects[0]['map_ids']=[123]
 ack={'resident_protocol':1,'status':'IDLE','run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'finish_nonce':nonce,'report_sha256':'a'*64,'artifact_manifest_sha256':'b'*64,
 'sequence':sequence,'kernel_objects_reused':sequence>1,'resident_resources':2,'resident_objects':objects}
 if MODE=='wrong-nonce':ack['finish_nonce']='wrong'
 if MODE=='wrong-hash':ack['report_sha256']='wrong'
 if MODE=='wrong-epoch':ack['epoch']+=1
 if MODE=='wrong-sequence':ack['sequence']+=1
 if MODE=='wrong-reuse':ack['kernel_objects_reused']=not ack['kernel_objects_reused']
 if MODE=='extra':ack['extra']='bad'
 if MODE=='bool-sequence':ack['sequence']=True
 if MODE=='empty-identity':ack['resident_objects']=[]
 if MODE=='unsolicited':print(json.dumps(ready),flush=True)
 print(json.dumps(ack),flush=True)
'''
    path.write_text('#!'+sys.executable+'\nMODE='+repr(mode)+'\n'+source)
    path.chmod(0o700)
    return SimpleNamespace(observer_path=path)


def begin(worker, root, index=1):
    config = root / ('run-'+str(index)+'.json')
    config.write_text(json.dumps({'run_id': 'run-'+str(index), 'epoch': index}))
    invocation = worker.begin(config, SimpleNamespace(run_id='run-'+str(index), run_epoch=index))
    deadline = time.monotonic()+5
    while invocation.ready is None and not invocation.done.is_set() and time.monotonic() < deadline:
        time.sleep(.005)
    assert invocation.ready is not None
    assert json.loads(invocation.stdout.readline())['status'] == 'READY'
    return invocation


def finish(worker, invocation, **override):
    value = {'protocol_version': 2, 'command': 'FINISH', 'run_id': invocation.run_id,
             'finish_nonce': invocation.ready['finish_nonce'], **override}
    worker.finish(invocation, json.dumps(value).encode()+b'\n')


def test_resident_reuses_process_and_identity_but_not_streams(tmp_path):
    worker = ResidentObserver(executable(tmp_path / 'worker'))
    try:
        first = begin(worker, tmp_path)
        finish(worker, first)
        assert first.done.wait(5) and first.returncode == 0
        terminal = json.loads(first.stdout.readline())
        assert terminal['run_id'] == 'run-1' and first.stdout.readline() == b''
        second = begin(worker, tmp_path, 2)
        assert second.pid == first.pid and worker.process.poll() is None
        finish(worker, second)
        assert second.done.wait(5) and second.returncode == 0
        assert first.evidence['kernel_objects_reused'] is False
        assert second.evidence['kernel_objects_reused'] is True
        assert first.evidence['resident_objects'] == second.evidence['resident_objects']
        assert json.loads(second.stdout.readline())['run_id'] == 'run-2'
        assert first.stdout.readline() == b''
    finally:
        assert worker.close()['exit_code'] == 0


@pytest.mark.parametrize('mode', ['eof', 'wrong-nonce', 'wrong-hash', 'wrong-epoch',
    'wrong-sequence', 'wrong-reuse', 'extra', 'bool-sequence', 'empty-identity', 'unsolicited'])
def test_invalid_seal_kills_and_poisons_worker(tmp_path, mode):
    worker = ResidentObserver(executable(tmp_path / 'worker', mode))
    try:
        invocation = begin(worker, tmp_path)
        finish(worker, invocation)
        assert invocation.done.wait(5) and invocation.returncode != 0
        assert invocation.stdout.readline() == b''  # No FINISHED without a valid ACK.
        with pytest.raises(WorkspaceError, match='unavailable'):
            begin(worker, tmp_path, 2)
        assert worker.poisoned and worker.process.poll() is not None
    finally:
        worker.close()


def test_changed_object_ids_cannot_claim_reuse(tmp_path):
    worker = ResidentObserver(executable(tmp_path / 'worker', 'changed-objects'))
    try:
        first = begin(worker, tmp_path)
        finish(worker, first)
        assert first.done.wait(5) and first.returncode == 0
        second = begin(worker, tmp_path, 2)
        finish(worker, second)
        assert second.done.wait(5) and second.returncode != 0
        assert worker.poisoned
    finally:
        worker.close()


def test_missing_idle_ack_does_not_release_run(tmp_path):
    worker = ResidentObserver(executable(tmp_path / 'worker', 'missing-ack'))
    try:
        run = begin(worker, tmp_path)
        finish(worker, run)
        assert not run.done.wait(.1) and run.poll() is None
        with pytest.raises(WorkspaceError, match='already owns'):
            begin(worker, tmp_path, 2)
    finally:
        assert worker.close()['poisoned']


def test_replayed_finish_is_rejected(tmp_path):
    worker = ResidentObserver(executable(tmp_path / 'worker'))
    try:
        run = begin(worker, tmp_path)
        with pytest.raises(WorkspaceError, match='binding'):
            finish(worker, run, finish_nonce='old-nonce')
        finish(worker, run)
        assert run.done.wait(5)
        with pytest.raises(WorkspaceError, match='order'):
            finish(worker, run)
    finally:
        worker.close()


def test_parallel_begin_refused_and_owner_fork_guard(tmp_path, monkeypatch):
    worker = ResidentObserver(executable(tmp_path / 'worker'))
    try:
        run = begin(worker, tmp_path)
        with pytest.raises(WorkspaceError, match='already owns'):
            begin(worker, tmp_path, 2)
        with monkeypatch.context() as scoped:
            scoped.setattr(os, 'getpid', lambda: worker.owner_pid+100)
            with pytest.raises(WorkspaceError, match='after fork'):
                worker.begin(tmp_path / 'unused', SimpleNamespace(run_id='child', run_epoch=2))
            assert worker.close() is None
        finish(worker, run)
        assert run.done.wait(5)
    finally:
        worker.close()


def test_failed_independent_verification_poisons_an_idle_worker(tmp_path):
    worker = ResidentObserver(executable(tmp_path / 'worker'))
    try:
        run = begin(worker, tmp_path)
        finish(worker, run)
        assert run.done.wait(5) and run.returncode == 0
        handle = SystemEffectsRun.__new__(SystemEffectsRun)
        handle._resident, handle._phase = worker, 'FAILED'
        handle._lock = threading.RLock()
        handle._expected_shutdown = handle._retired = False
        handle._process, handle._verifier_process = run, None
        handle._reader = handle._errors = None
        retired = []
        handle._on_close = retired.append
        handle._shutdown(preserve_phase=True)
        assert retired == [handle] and worker.poisoned
        assert worker.process.poll() is not None
    finally:
        worker.close()


@pytest.mark.parametrize('value', [1, 'true', None, [], {}])
def test_resident_configuration_requires_boolean(tmp_path, value):
    binary = tmp_path / 'binary'
    executable(binary)
    config = {'observer_path': str(binary), 'verifier_path': str(binary),
              'recorder_object_path': str(binary), 'recorder_object_sha256': 'a'*64,
              'lifecycle_object_path': str(binary), 'lifecycle_object_sha256': 'b'*64,
              'resident_observer': value}
    with pytest.raises(WorkspaceError, match='boolean'):
        SystemEffectsConfig.from_mapping(config)
