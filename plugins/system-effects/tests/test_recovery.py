"""Protocol-v3 state-machine and policy tests for the recovery runner."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from allox_system_effects.config import MAX_RECOVERY_OUTPUT_BYTES, SystemEffectsConfig
from allox_system_effects.recovery_runner import (
    RECOVERY_PROTOCOL_VERSION,
    RecoverySystemEffectsRun,
)

from allox.workspace.store import WorkspaceError

BOOT_ID = "12345678-1234-1234-1234-123456789abc"
CGROUP_ID = 42
CGROUP_RELATIVE = "/allox/agents/a/sessions/s"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)


def _observer(path: Path, mode: str, verdict: str, helper_success: bool) -> None:
    constants = (
        f"MODE={mode!r}\n"
        f"VERDICT={verdict!r}\n"
        f"HELPER_SUCCESS={helper_success!r}\n"
        f"BOOT_ID={BOOT_ID!r}\n"
        f"CGROUP_ID={CGROUP_ID!r}\n"
    )
    _write_executable(
        path,
        constants
        + r"""
import hashlib,json,os,pathlib,sys

args=sys.argv[1:]
assert len(args)==2 and args[0]=='--config'
cfg=json.loads(pathlib.Path(args[1]).read_bytes())
assert cfg['protocol_version']==3
assert cfg['deployment']['configuration']['require_dedicated_cgroup'] is True
out=pathlib.Path(cfg['output_directory'])
out.mkdir(mode=0o700)
recovery=cfg['recovery']
root=cfg['deployment']['configuration']

def encoded(value, canonical=False):
 return (json.dumps(value,sort_keys=canonical,separators=(',',':'))+'\n').encode()

def write(name,value,canonical=False):
 data=encoded(value,canonical)
 (out/name).write_bytes(data)
 return data,hashlib.sha256(data).hexdigest()

def digest(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

scope_sha='1'*64
cap={
 'protocol_version':1,
 'mode':'CGROUP_V2_ANCESTOR_ROOT',
 'membership_authority':'BPF_CURRENT_TASK_UNDER_CGROUP',
 'root_map_type':'BPF_MAP_TYPE_CGROUP_ARRAY',
 'supported_program_families':['BPF_PROG_TYPE_LSM','BPF_PROG_TYPE_TRACEPOINT','BPF_PROG_TYPE_TRACING_TP_BTF'],
 'maximum_roots':8,
 'root_cgroup_ids':[CGROUP_ID],
 'event_cgroup_id_semantics':'MATCHED_SCOPE_ROOT',
 'leaf_cgroup_id_retained':False,
 'dynamic_descendants_including_ephemeral':True,
 'readiness_basis':'BOTH_BPF_OBJECTS_LOADED_VERIFIER_ACCEPTED_MAP_ABI_VALIDATED_ROOT_FDS_PINNED',
}
cap_sha=hashlib.sha256(json.dumps(cap,separators=(',',':')).encode()).hexdigest()
pid=root['root_pid']
mnt=os.stat('/proc/'+str(pid)+'/ns/mnt').st_ino
net=os.stat('/proc/'+str(pid)+'/ns/net').st_ino
pidns=os.stat('/proc/'+str(pid)+'/ns/pid').st_ino
uts=os.stat('/proc/'+str(pid)+'/ns/uts').st_ino
ipc=os.stat('/proc/'+str(pid)+'/ns/ipc').st_ino
mount_ids=sorted([mnt,mnt+1]) if MODE=='multi-namespace' else [mnt]
net_ids=sorted([net,net+1]) if MODE=='multi-namespace' else [net]
deployment={
 'protocol_version':2,
 'deployment_kind':'DIRECT',
 'boot_id':BOOT_ID,
 'scope_sha256':scope_sha,
 'root_cgroups':[{'path':root['expected_cgroup_path'],'cgroup_id':CGROUP_ID}],
 'processes':[{
   'pid':pid,'role':'DIRECT_ROOT','cgroup_path':root['expected_cgroup_path'],
   'cgroup_id':CGROUP_ID,'mount_namespace_id':mnt,'net_namespace_id':net,
 }],
}
if MODE=='multi-namespace':
 deployment['processes'].append({
   'pid':pid+1,'role':'DIRECT_DESCENDANT','cgroup_path':root['expected_cgroup_path'],
   'cgroup_id':CGROUP_ID,'mount_namespace_id':mount_ids[-1],
   'net_namespace_id':net_ids[-1],
 })
deployment_bytes,deployment_sha=write('deployment-manifest.json',deployment,True)
_,ready_barrier_sha=write('ready-barrier.json',{'phase':'A'},True)
_,ready_validation_sha=write('ready-validation.json',{'phase':'A'},True)
ready_v2={
 'protocol_version':2,
 'status':'READY',
 'run_id':cfg['run_id'],
 'epoch':cfg['epoch'],
 'finish_nonce':'9'*64,
 'deployment_scope_sha256':scope_sha,
 'registration_cgroup_ids':[CGROUP_ID],
 'cgroup_scope_capability_sha256':cap_sha,
 'cgroup_scope_capability':cap,
 'deployment_manifest_sha256':deployment_sha,
 'ready_barrier_sha256':ready_barrier_sha,
 'ready_validation_sha256':ready_validation_sha,
 'recorder_object_sha256':cfg['recorder_object_sha256'],
 'lifecycle_object_sha256':cfg['lifecycle_object_sha256'],
 'socket_state_enabled':cfg['object_resolution_mode']=='KERNEL_OBJECT_EXPANDED_PARTIAL',
 'bpf_state_scope_sha256':digest(cfg['bpf_state_scope']) if cfg.get('bpf_state_scope') is not None else None,
 'bpf_state_s0_sha256':'5'*64 if cfg.get('bpf_state_scope') is not None else None,
 'filesystem_state_scope_sha256':digest(cfg['filesystem_state_scope']) if cfg.get('filesystem_state_scope') is not None else None,
 'filesystem_state_s0_sha256':'6'*64 if cfg.get('filesystem_state_scope') is not None else None,
 'linux_state_scope_sha256':digest(cfg['linux_state_scope']) if cfg.get('linux_state_scope') is not None else None,
 'linux_state_s0_sha256':'7'*64 if cfg.get('linux_state_scope') is not None else None,
}
write('ready-message.json',ready_v2)

def snapshot(phase,barrier_sha,validation_sha):
 return {
  'schema_version':1,
  'protocol_version':3,
  'phase':phase,
  'run_id':cfg['run_id'],
  'epoch':cfg['epoch'],
  'checkpoint_id':recovery['checkpoint_id'],
  'recovery_id':recovery['recovery_id'],
  'snapshot_id':phase+'-snapshot-id',
  'capture_started_unix_ns':100,
  'capture_completed_unix_ns':101,
  'boot_id':BOOT_ID,
  'deployment_scope_sha256':scope_sha,
  'cgroup_scope_capability':cap,
  'cgroup_scope_capability_sha256':cap_sha,
  'root_cgroups':[{'cgroup_id':CGROUP_ID,'path':root['expected_cgroup_path']}],
  'mount_namespace_ids':mount_ids,
  'pid_namespace_ids':[pidns],
  'net_namespace_ids':net_ids,
  'uts_namespace_ids':[uts],
  'ipc_namespace_ids':[ipc],
  'barrier_sha256':barrier_sha,
  'identity_assessment_sha256':validation_sha,
  'bpf':{} if cfg.get('bpf_state_scope') is not None else None,
  'filesystem':{} if cfg.get('filesystem_state_scope') is not None else None,
  'linux':{} if cfg.get('linux_state_scope') is not None else None,
 }

snapshot_a= snapshot('A',ready_barrier_sha,ready_validation_sha)
snapshot_a_bytes,snapshot_a_sha=write('snapshot-a.json',snapshot_a,True)
ready={
 'protocol_version':3,'status':'READY','run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'command_seq':1,'nonce':'a'*64,'snapshot_a_sha256':snapshot_a_sha,
}
if MODE=='binding-error':
 ready['recovery_id']='wrong-recovery'
write('recovery-ready-message.json',ready)
print(json.dumps(ready,separators=(',',':')),flush=True)
if MODE=='binding-error':
 sys.stdin.read()
 raise SystemExit(0)

agent=json.loads(sys.stdin.readline())
assert agent=={
 'command':'AGENT_STARTED','protocol_version':3,'run_id':cfg['run_id'],
 'epoch':cfg['epoch'],'checkpoint_id':recovery['checkpoint_id'],
 'recovery_id':recovery['recovery_id'],'command_seq':1,'nonce':'a'*64,
}
agent_sha=hashlib.sha256(json.dumps(agent,separators=(',',':')).encode()).hexdigest()
started={
 'protocol_version':3,'status':'AGENT_STARTED','run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'command_seq':2,'nonce':'b'*64,'agent_started_sha256':agent_sha,
}
write('agent-started.json',started)
print(json.dumps(started,separators=(',',':')),flush=True)

pre=json.loads(sys.stdin.readline())
assert pre=={
 'command':'PRE_RESTORE','protocol_version':3,'run_id':cfg['run_id'],
 'epoch':cfg['epoch'],'checkpoint_id':recovery['checkpoint_id'],
 'recovery_id':recovery['recovery_id'],'command_seq':2,'nonce':'b'*64,
}
_,b_barrier_sha=write('pre-restore-barrier.json',{'phase':'B'},True)
_,b_validation_sha=write('pre-restore-validation.json',{'phase':'B'},True)
snapshot_b=snapshot('B',b_barrier_sha,b_validation_sha)
snapshot_b_bytes,snapshot_b_sha=write('snapshot-b.json',snapshot_b,True)
snapshot_b_message={
 'protocol_version':3,'status':'SNAPSHOT_B','run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'command_seq':2,'nonce':'b'*64,'pre_restore_sha256':b_validation_sha,
 'snapshot_b_sha256':snapshot_b_sha,
}
recovery_evidence={
 'schema_version':1,'mechanism':'DIRECT_EXEC_NO_SHELL',
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'executable_path':recovery['executable_path'],'argv':recovery['argv'],
 'timeout_ms':recovery['timeout_ms'],'max_output_bytes':recovery['max_output_bytes'],
 'expected_executable_sha256':recovery['expected_sha256'],'success':HELPER_SUCCESS,
}
recovery_bytes,recovery_sha=write('recovery.json',recovery_evidence,True)
restored={
 'protocol_version':3,'status':'RESTORE_COMPLETE','run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'command_seq':2,'nonce':'b'*64,'recovery_sha256':recovery_sha,
}
_,c_barrier_sha=write('post-restore-barrier.json',{'phase':'C'},True)
_,c_validation_sha=write('post-restore-validation.json',{'phase':'C'},True)
snapshot_c=snapshot('C',c_barrier_sha,c_validation_sha)
snapshot_c_bytes,snapshot_c_sha=write('snapshot-c.json',snapshot_c,True)
snapshot_c_message={
 'protocol_version':3,'status':'SNAPSHOT_C','run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'command_seq':3,'nonce':'c'*64,'snapshot_c_sha256':snapshot_c_sha,
}
if MODE=='out-of-order':
 print(json.dumps(restored,separators=(',',':')),flush=True)
 print(json.dumps(snapshot_b_message,separators=(',',':')),flush=True)
 sys.stdin.read()
 raise SystemExit(0)
print(json.dumps(snapshot_b_message,separators=(',',':')),flush=True)
print(json.dumps(restored,separators=(',',':')),flush=True)
print(json.dumps(snapshot_c_message,separators=(',',':')),flush=True)

finish=json.loads(sys.stdin.readline())
assert finish=={
 'command':'FINISH','protocol_version':3,'run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'command_seq':3,'nonce':'c'*64,
}
assert sys.stdin.read()==''
empty_delta={}
verdict={
 'schema_version':1,'protocol_version':3,'run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'snapshot_a_id':snapshot_a['snapshot_id'],'snapshot_b_id':snapshot_b['snapshot_id'],
 'snapshot_c_id':snapshot_c['snapshot_id'],'snapshot_a_sha256':snapshot_a_sha,
 'snapshot_b_sha256':snapshot_b_sha,'snapshot_c_sha256':snapshot_c_sha,
 'agent_delta':empty_delta,'recovery_delta':empty_delta,'residual_delta':empty_delta,
 'classifications':{},'raw_endpoint_proof_complete':VERDICT!='INCONCLUSIVE',
 'state_complete':VERDICT!='INCONCLUSIVE','external_incompleteness_reasons':[],
 'incompleteness_reasons':[],'verdict':VERDICT,'limitations':[],
}
verdict_bytes,verdict_sha=write('recovery-verdict.json',verdict,True)
artifact_values={
 'recovery-verdict.json':verdict_bytes,
 'snapshot-a.json':snapshot_a_bytes,
 'snapshot-b.json':snapshot_b_bytes,
 'snapshot-c.json':snapshot_c_bytes,
}
artifacts=[
 {'relative_path':name,'size_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
 for name,data in sorted(artifact_values.items())
]
ordered_recovery={
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'executable_path':recovery['executable_path'],'expected_sha256':recovery['expected_sha256'],
 'argv':recovery['argv'],'timeout_ms':recovery['timeout_ms'],
 'max_output_bytes':recovery['max_output_bytes'],
}
manifest={
 'protocol_version':3,'run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'deployment_scope_sha256':scope_sha,
 'recovery_config_sha256':hashlib.sha256(json.dumps(ordered_recovery,separators=(',',':')).encode()).hexdigest(),
 'recovery_verdict_sha256':verdict_sha,'snapshot_a_sha256':snapshot_a_sha,
 'snapshot_b_sha256':snapshot_b_sha,'snapshot_c_sha256':snapshot_c_sha,
 'committed_artifact_total_bytes':sum(item['size_bytes'] for item in artifacts),
 'artifacts':artifacts,
}
manifest_bytes,manifest_sha=write('recovery-artifact-manifest.json',manifest,True)
terminal={
 'protocol_version':3,'status':'FINISHED','run_id':cfg['run_id'],'epoch':cfg['epoch'],
 'checkpoint_id':recovery['checkpoint_id'],'recovery_id':recovery['recovery_id'],
 'command_seq':3,'nonce':'c'*64,'verdict_sha256':verdict_sha,
 'artifact_manifest_sha256':manifest_sha,
}
print(json.dumps(terminal,separators=(',',':')),flush=True)
""",
    )


def _recovery_verifier(path: Path) -> None:
    _write_executable(
        path,
        r"""
import hashlib,json,pathlib,sys
args=sys.argv[1:]
assert len(args)==4 and args[0]=='--artifacts' and args[2]=='--expectation'
artifacts=pathlib.Path(args[1])
expect=json.loads(pathlib.Path(args[3]).read_bytes())
manifest_bytes=(artifacts/'recovery-artifact-manifest.json').read_bytes()
verdict_bytes=(artifacts/'recovery-verdict.json').read_bytes()
assert hashlib.sha256(manifest_bytes).hexdigest()==expect['expected_artifact_manifest_sha256']
assert hashlib.sha256(verdict_bytes).hexdigest()==expect['expected_recovery_verdict_sha256']
manifest=json.loads(manifest_bytes)
verdict=json.loads(verdict_bytes)
cfg=expect['expected_config']
result={
 'schema_version':1,'protocol_version':3,'verified':True,'run_id':cfg['run_id'],
 'epoch':cfg['epoch'],'checkpoint_id':cfg['recovery']['checkpoint_id'],
 'recovery_id':cfg['recovery']['recovery_id'],'artifact_count':len(manifest['artifacts']),
 'committed_artifact_total_bytes':manifest['committed_artifact_total_bytes'],
 'verdict':verdict['verdict'],'endpoint_replay_verified':True,
 'recorder_wal_replay_verified':True,
}
print(json.dumps(result,sort_keys=True))
""",
    )


class _Store:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.cleanup_calls: list[tuple[str, str, str]] = []

    def current(self, agent_id: str, session_id: str) -> Path:
        return self.root / "agents" / agent_id / "workspace" / "sessions" / session_id / "current"

    def agent_shared(self, agent_id: str) -> Path:
        return self.root / "agents" / agent_id / "workspace" / "shared"

    def complete_deferred_rollback(self, agent_id: str, session_id: str, recovery_id: str) -> dict:
        self.cleanup_calls.append((agent_id, session_id, recovery_id))
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "recovery_id": recovery_id,
            "cleanup_status": "completed",
            "cleanup_completed": True,
            "cleanup_pending": False,
        }


def _scope() -> dict:
    return {
        "protocol_version": 1,
        "roots": [
            {"path_bytes_hex": b"/agent/shared".hex()},
            {"path_bytes_hex": b"/workspace".hex()},
        ],
        "capture": {
            "regular_file_content": "SHA256",
            "symlinks": "TARGET_BYTES",
            "xattrs": "NAMES_AND_VALUE_SHA256",
            "cross_filesystems": "SAME_DEVICE_ONLY",
        },
        "limits": {
            "max_entries": 100,
            "max_total_path_bytes": 10000,
            "max_file_content_bytes": 10000,
            "max_total_content_bytes": 10000,
            "max_xattr_list_bytes": 1000,
            "max_xattrs_per_entry": 10,
            "max_total_xattrs": 100,
            "max_xattr_value_bytes": 1000,
            "max_total_xattr_bytes": 10000,
            "max_symlink_target_bytes": 1000,
            "max_depth": 10,
            "deadline_ms": 1000,
        },
    }


def _fixture(
    tmp_path: Path,
    *,
    mode: str = "success",
    verdict: str = "CONSISTENT_WITHIN_SCOPE",
    helper_success: bool = True,
    require_consistent: bool = True,
    filesystem_scope: bool = True,
    recovery_max_output_bytes: int = 4096,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    observer = tmp_path / "deployment-observe"
    verifier = tmp_path / "deployment-observe-verify"
    recovery_verifier = tmp_path / "deployment-observe-recovery-verify"
    helper = tmp_path / "allox-system-effects-recover"
    recorder = tmp_path / "system_effects.bpf.o"
    lifecycle = tmp_path / "process_lifecycle.bpf.o"
    _observer(observer, mode, verdict, helper_success)
    _recovery_verifier(recovery_verifier)
    _write_executable(verifier, "raise SystemExit('v2 verifier must not run')\n")
    _write_executable(helper, "raise SystemExit('fake Observer must not exec helper')\n")
    recorder.write_bytes(b"recorder")
    lifecycle.write_bytes(b"lifecycle")
    recorder.chmod(0o600)
    lifecycle.chmod(0o600)

    store_root = tmp_path / "store"
    store = _Store(store_root)
    store.current("a", "s").mkdir(parents=True, mode=0o700)
    store.agent_shared("a").mkdir(parents=True, exist_ok=True, mode=0o700)
    config_mapping = {
        "observer_path": str(observer),
        "verifier_path": str(verifier),
        "recovery_verifier_path": str(recovery_verifier),
        "recovery_helper_path": str(helper),
        "recovery_helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "recovery_timeout_ms": 1000,
        "recovery_max_output_bytes": recovery_max_output_bytes,
        "recorder_object_path": str(recorder),
        "recorder_object_sha256": hashlib.sha256(recorder.read_bytes()).hexdigest(),
        "lifecycle_object_path": str(lifecycle),
        "lifecycle_object_sha256": hashlib.sha256(lifecycle.read_bytes()).hexdigest(),
        "profile": "balanced",
        "object_resolution_mode": "KERNEL_OBJECT_EXPANDED_PARTIAL",
        "host_lock_path": str(tmp_path / "system-effects.lock"),
        "ready_timeout": 2,
        "finalize_timeout": 3,
    }
    if filesystem_scope:
        scope_path = tmp_path / "filesystem-scope.json"
        scope_path.write_text(json.dumps(_scope()))
        scope_path.chmod(0o600)
        config_mapping["filesystem_state_scope_file"] = str(scope_path)
    config = SystemEffectsConfig.from_mapping(config_mapping)
    audit = tmp_path / "audit"
    cgroup = tmp_path / "cgroup"
    audit.mkdir(mode=0o700)
    cgroup.mkdir(mode=0o700)
    failures: list[str] = []
    context = SimpleNamespace(
        agent_id="a",
        session_id="s",
        run_id="run-1",
        run_epoch=7,
        root_pid=os.getpid(),
        cgroup_path=str(cgroup),
        cgroup_relative_path=CGROUP_RELATIVE,
        cgroup_id=CGROUP_ID,
        mount_namespace_id=os.stat("/proc/self/ns/mnt").st_ino,
        network_namespace_id=os.stat("/proc/self/ns/net").st_ino,
        audit_directory=str(audit),
        boot_id=BOOT_ID,
        on_failure=failures.append,
        recovery=SimpleNamespace(
            checkpoint_id="checkpoint-1",
            recovery_id="recovery-1",
            scrub_runtime=True,
            require_consistent=require_consistent,
            baseline_created=False,
        ),
    )
    closed: list[RecoverySystemEffectsRun] = []
    return config, context, store, failures, closed


def _quiescence(context: SimpleNamespace) -> dict:
    return {
        "status": "QUIESCENT_WITHIN_CGROUP",
        "basis": "CGROUP_V2_EMPTY_FROZEN_VERIFIED",
        "cgroup_path": context.cgroup_path,
        "cgroup_id": context.cgroup_id,
    }


def test_recovery_happy_path_maps_store_roots_and_verifies(tmp_path: Path) -> None:
    config, context, store, failures, closed = _fixture(tmp_path)
    handle = RecoverySystemEffectsRun(config, context, closed.append, store)
    observer_config = json.loads((tmp_path / "audit/system-effects-config.json").read_bytes())
    assert observer_config["protocol_version"] == RECOVERY_PROTOCOL_VERSION
    assert observer_config["filesystem_namespace_mode"] == "OBSERVER"
    assert observer_config["deployment"]["configuration"]["require_dedicated_cgroup"] is True
    assert [
        bytes.fromhex(root["path_bytes_hex"])
        for root in observer_config["filesystem_state_scope"]["roots"]
    ] == sorted([os.fsencode(store.current("a", "s")), os.fsencode(store.agent_shared("a"))])
    assert observer_config["recovery"]["argv"] == [
        "--store-root",
        str(store.root),
        "--agent-id",
        "a",
        "--session-id",
        "s",
        "--checkpoint-id",
        "checkpoint-1",
        "--recovery-id",
        "recovery-1",
        "--scrub-runtime",
        "true",
    ]

    handle.agent_started()
    assert handle.status()["status"] == "AGENT_RUNNING"
    result = handle.finish(outcome="completed", quiescence=_quiescence(context))

    assert result["offline_verified"] is True
    assert result["recovery_verdict"] == "CONSISTENT_WITHIN_SCOPE"
    assert result["release_allowed"] is True
    assert result["fence_required"] is False
    assert store.cleanup_calls == [("a", "s", "recovery-1")]
    assert failures == []
    assert closed == [handle]
    assert handle.finish(outcome="completed", quiescence=_quiescence(context)) == result


def test_recovery_without_filesystem_scope_uses_deployment_namespace(tmp_path: Path) -> None:
    config, context, store, _failures, closed = _fixture(tmp_path, filesystem_scope=False)
    handle = RecoverySystemEffectsRun(config, context, closed.append, store)
    observer_config = json.loads((tmp_path / "audit/system-effects-config.json").read_bytes())
    assert observer_config["filesystem_state_scope"] is None
    assert observer_config["filesystem_namespace_mode"] == "DEPLOYMENT"
    handle.agent_started()
    assert (
        handle.finish(outcome="completed", quiescence=_quiescence(context))["release_allowed"]
        is True
    )


def test_recovery_output_bound_matches_observer_contract(tmp_path: Path) -> None:
    config, *_rest = _fixture(
        tmp_path / "maximum",
        recovery_max_output_bytes=MAX_RECOVERY_OUTPUT_BYTES,
    )
    assert config.recovery_max_output_bytes == MAX_RECOVERY_OUTPUT_BYTES

    with pytest.raises(WorkspaceError, match="recovery_max_output_bytes"):
        _fixture(
            tmp_path / "too-large",
            recovery_max_output_bytes=MAX_RECOVERY_OUTPUT_BYTES + 1,
        )


def test_recovery_accepts_bound_root_with_multiple_namespaces(tmp_path: Path) -> None:
    config, context, store, failures, closed = _fixture(
        tmp_path,
        mode="multi-namespace",
    )
    handle = RecoverySystemEffectsRun(config, context, closed.append, store)
    handle.agent_started()

    result = handle.finish(outcome="completed", quiescence=_quiescence(context))

    assert result["release_allowed"] is True
    assert failures == []


def test_verified_inconsistent_policy_requires_fence(tmp_path: Path) -> None:
    config, context, store, failures, closed = _fixture(
        tmp_path, verdict="INCONSISTENT_WITHIN_SCOPE", require_consistent=True
    )
    handle = RecoverySystemEffectsRun(config, context, closed.append, store)
    handle.agent_started()
    result = handle.finish(outcome="completed", quiescence=_quiescence(context))

    assert result["offline_verified"] is True
    assert result["recovery_verdict"] == "INCONSISTENT_WITHIN_SCOPE"
    assert result["release_allowed"] is False
    assert result["fence_required"] is True
    assert store.cleanup_calls == [("a", "s", "recovery-1")]
    assert failures == []


def test_failed_helper_keeps_valid_inconclusive_receipt_without_cleanup(tmp_path: Path) -> None:
    config, context, store, _failures, closed = _fixture(
        tmp_path,
        verdict="INCONCLUSIVE",
        helper_success=False,
        require_consistent=False,
    )
    handle = RecoverySystemEffectsRun(config, context, closed.append, store)
    handle.agent_started()
    result = handle.finish(outcome="completed", quiescence=_quiescence(context))

    assert result["recovery_verdict"] == "INCONCLUSIVE"
    assert result["release_allowed"] is True
    assert result["deferred_rollback_cleanup"]["cleanup_status"] == "NOT_APPLICABLE"
    assert store.cleanup_calls == []


def test_out_of_order_restore_message_is_rejected(tmp_path: Path) -> None:
    config, context, store, _failures, closed = _fixture(tmp_path, mode="out-of-order")
    handle = RecoverySystemEffectsRun(config, context, closed.append, store)
    handle.agent_started()

    with pytest.raises(WorkspaceError, match="expected SNAPSHOT_B"):
        handle.finish(outcome="completed", quiescence=_quiescence(context))

    assert store.cleanup_calls == []
    assert closed == [handle]


def test_ready_recovery_binding_error_is_rejected(tmp_path: Path) -> None:
    config, context, store, _failures, closed = _fixture(tmp_path, mode="binding-error")

    with pytest.raises(WorkspaceError, match="READY binding mismatch"):
        RecoverySystemEffectsRun(config, context, closed.append, store)

    assert store.cleanup_calls == []
    assert len(closed) == 1
