"""Protocol, binding, verification, and concurrency tests for the plugin."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from allox_system_effects.plugin import SystemEffectsFeature

from allox.workspace.store import WorkspaceError

BOOT_ID = "12345678-1234-1234-1234-123456789abc"
CGROUP_ID = 42
CGROUP_RELATIVE = "/allox/agents/a/sessions/s"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)


def _observer(path: Path, mode: str = "success") -> None:
    _write_executable(
        path,
        f"""
import hashlib,json,os,pathlib,sys,time
MODE={mode!r}
BOOT={BOOT_ID!r}
args=sys.argv[1:]
assert len(args)==2 and args[0]=='--config'
cfg=json.loads(pathlib.Path(args[1]).read_bytes())
out=pathlib.Path(cfg['output_directory'])
out.mkdir(mode=0o700)
root=cfg['deployment']['configuration']
cgroup_id={CGROUP_ID}
scope_sha='1'*64
cap={{
 'protocol_version':1,
 'mode':'CGROUP_V2_ANCESTOR_ROOT',
 'membership_authority':'BPF_CURRENT_TASK_UNDER_CGROUP',
 'root_map_type':'BPF_MAP_TYPE_CGROUP_ARRAY',
 'supported_program_families':['BPF_PROG_TYPE_LSM','BPF_PROG_TYPE_TRACEPOINT','BPF_PROG_TYPE_TRACING_TP_BTF'],
 'maximum_roots':8,
 'root_cgroup_ids':[cgroup_id],
 'event_cgroup_id_semantics':'MATCHED_SCOPE_ROOT',
 'leaf_cgroup_id_retained':False,
 'dynamic_descendants_including_ephemeral':True,
 'readiness_basis':'BOTH_BPF_OBJECTS_LOADED_VERIFIER_ACCEPTED_MAP_ABI_VALIDATED_ROOT_FDS_PINNED',
}}
cap_sha=hashlib.sha256(json.dumps(cap,separators=(',',':')).encode()).hexdigest()
mount_namespace_id=os.stat('/proc/'+str(root['root_pid'])+'/ns/mnt').st_ino
net_namespace_id=os.stat('/proc/'+str(root['root_pid'])+'/ns/net').st_ino
if MODE=='namespace-mismatch':
 mount_namespace_id+=1
manifest={{
 'protocol_version':2,
 'deployment_kind':'DIRECT',
 'boot_id':BOOT,
 'scope_sha256':scope_sha,
 'root_cgroups':[{{'path':root['expected_cgroup_path'],'cgroup_id':cgroup_id}}],
 'processes':[{{'pid':root['root_pid'],'role':'DIRECT_ROOT','cgroup_path':root['expected_cgroup_path'],'cgroup_id':cgroup_id,'mount_namespace_id':mount_namespace_id,'net_namespace_id':net_namespace_id}}],
}}
manifest_bytes=(json.dumps(manifest,sort_keys=True,separators=(',',':'))+'\\n').encode()
(out/'deployment-manifest.json').write_bytes(manifest_bytes)
manifest_sha=hashlib.sha256(manifest_bytes).hexdigest()
def scope_digest(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest() if value is not None else None
ready={{
 'protocol_version':2,
 'status':'READY',
 'run_id':cfg['run_id'],
 'epoch':cfg['epoch'],
 'finish_nonce':'2'*64,
 'deployment_scope_sha256':scope_sha,
 'registration_cgroup_ids':[cgroup_id],
 'cgroup_scope_capability_sha256':cap_sha,
 'cgroup_scope_capability':cap,
 'deployment_manifest_sha256':manifest_sha,
 'ready_barrier_sha256':'3'*64,
 'ready_validation_sha256':'4'*64,
 'recorder_object_sha256':cfg['recorder_object_sha256'],
 'lifecycle_object_sha256':cfg['lifecycle_object_sha256'],
 'socket_state_enabled':cfg['object_resolution_mode']=='KERNEL_OBJECT_EXPANDED_PARTIAL',
 'bpf_state_scope_sha256':scope_digest(cfg.get('bpf_state_scope')),
 'bpf_state_s0_sha256':'5'*64 if cfg.get('bpf_state_scope') is not None else None,
 'filesystem_state_scope_sha256':scope_digest(cfg.get('filesystem_state_scope')),
 'filesystem_state_s0_sha256':'6'*64 if cfg.get('filesystem_state_scope') is not None else None,
 'linux_state_scope_sha256':scope_digest(cfg.get('linux_state_scope')),
 'linux_state_s0_sha256':'7'*64 if cfg.get('linux_state_scope') is not None else None,
}}
if MODE=='ready-mismatch':
 ready['registration_cgroup_ids']=[999]
print(json.dumps(ready,separators=(',',':')),flush=True)
if MODE=='crash':
 time.sleep(.15)
 raise SystemExit(7)
finish=json.loads(sys.stdin.readline())
assert finish=={{'protocol_version':2,'command':'FINISH','run_id':cfg['run_id'],'finish_nonce':'2'*64}}
report={{
 'protocol_version':2,
 'run_id':cfg['run_id'],
 'epoch':cfg['epoch'],
 'deployment':manifest,
 'cgroup_scope_capability_sha256':cap_sha,
 'recorder_object_sha256':cfg['recorder_object_sha256'],
 'lifecycle_object_sha256':cfg['lifecycle_object_sha256'],
 'registration_cgroup_ids':[cgroup_id],
 'overall_evidence_status':'INCOMPLETE',
}}
report_bytes=(json.dumps(report,sort_keys=True,separators=(',',':'))+'\\n').encode()
(out/'deployment-observation-report.json').write_bytes(report_bytes)
report_sha=hashlib.sha256(report_bytes).hexdigest()
artifact={{
 'protocol_version':2,
 'run_id':cfg['run_id'],
 'epoch':cfg['epoch'],
 'deployment_scope_sha256':scope_sha,
 'cgroup_scope_capability_sha256':cap_sha,
 'evidence_status':'INCOMPLETE',
 'artifacts':[{{'relative_path':'deployment-observation-report.json','size_bytes':len(report_bytes),'sha256':report_sha}}],
}}
artifact_bytes=(json.dumps(artifact,sort_keys=True,separators=(',',':'))+'\\n').encode()
(out/'artifact-manifest.json').write_bytes(artifact_bytes)
artifact_sha=hashlib.sha256(artifact_bytes).hexdigest()
if MODE=='bad-report-digest':
 report_sha='f'*64
terminal={{
 'protocol_version':2,
 'status':'FINISHED',
 'run_id':cfg['run_id'],
 'evidence_status':'INCOMPLETE',
 'report_sha256':report_sha,
 'artifact_manifest_sha256':artifact_sha,
}}
print(json.dumps(terminal,separators=(',',':')),flush=True)
""",
    )


def _verifier(path: Path, *, verified: bool = True) -> None:
    _write_executable(
        path,
        f"""
import hashlib,json,pathlib,sys
VERIFIED={verified!r}
args=sys.argv[1:]
assert len(args)==4 and args[0]=='--artifacts' and args[2]=='--expectation'
artifacts=pathlib.Path(args[1])
expect=json.loads(pathlib.Path(args[3]).read_bytes())
cfg=expect['expected_config']
report=json.loads((artifacts/'deployment-observation-report.json').read_bytes())
assert hashlib.sha256((artifacts/'deployment-observation-report.json').read_bytes()).hexdigest()==expect['expected_report_sha256']
assert hashlib.sha256((artifacts/'artifact-manifest.json').read_bytes()).hexdigest()==expect['expected_artifact_manifest_sha256']
def scope_digest(value):
 return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest() if value is not None else None
result={{
 'protocol_version':2,
 'verified':VERIFIED,
 'run_id':cfg['run_id'],
 'epoch':cfg['epoch'],
 'deployment_scope_sha256':report['deployment']['scope_sha256'],
 'artifact_count':1,
 'committed_artifact_total_bytes':(artifacts/'deployment-observation-report.json').stat().st_size,
 'system_effect_wal_event_count':3,
 'system_effect_jsonl_event_count':3,
 'process_lifecycle_wal_event_count':2,
 'process_lifecycle_jsonl_event_count':2,
 'evidence_status':report['overall_evidence_status'],
 'socket_state_status':'COMPLETE' if cfg['object_resolution_mode']=='KERNEL_OBJECT_EXPANDED_PARTIAL' else None,
 'socket_state_event_count':0 if cfg['object_resolution_mode']=='KERNEL_OBJECT_EXPANDED_PARTIAL' else None,
 'bpf_state_scope_sha256':scope_digest(cfg.get('bpf_state_scope')),
 'bpf_state_verdict':None,
 'filesystem_state_scope_sha256':scope_digest(cfg.get('filesystem_state_scope')),
 'filesystem_state_verdict':None,
 'linux_state_scope_sha256':scope_digest(cfg.get('linux_state_scope')),
 'linux_state_evidence_status':None,
}}
print(json.dumps(result,sort_keys=True))
""",
    )


def _fixture(
    tmp_path: Path,
    *,
    observer_mode: str = "success",
    verified: bool = True,
    linux_scope: dict | None = None,
):
    observer = tmp_path / "deployment-observe"
    verifier = tmp_path / "deployment-observe-verify"
    recorder = tmp_path / "system_effects.bpf.o"
    lifecycle = tmp_path / "process_lifecycle.bpf.o"
    _observer(observer, observer_mode)
    _verifier(verifier, verified=verified)
    recorder.write_bytes(b"recorder")
    lifecycle.write_bytes(b"lifecycle")
    recorder.chmod(0o600)
    lifecycle.chmod(0o600)
    config = {
        "observer_path": str(observer),
        "verifier_path": str(verifier),
        "recorder_object_path": str(recorder),
        "recorder_object_sha256": hashlib.sha256(recorder.read_bytes()).hexdigest(),
        "lifecycle_object_path": str(lifecycle),
        "lifecycle_object_sha256": hashlib.sha256(lifecycle.read_bytes()).hexdigest(),
        "profile": "balanced",
        "object_resolution_mode": "KERNEL_OBJECT_EXPANDED_PARTIAL",
        "host_lock_path": str(tmp_path / "system-effects.lock"),
        "ready_timeout": 1,
        "finalize_timeout": 2,
    }
    if linux_scope is not None:
        scope_path = tmp_path / "linux-state-scope.json"
        scope_path.write_text(json.dumps(linux_scope))
        scope_path.chmod(0o600)
        config["linux_state_scope_file"] = str(scope_path)
    failures: list[str] = []
    audit = tmp_path / "audit"
    cgroup = tmp_path / "cgroup"
    audit.mkdir(mode=0o700)
    cgroup.mkdir(mode=0o700)
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
    )
    feature = SystemEffectsFeature(context=SimpleNamespace(), config=config)
    return feature, context, config, failures


def _quiescence(context: SimpleNamespace) -> dict:
    return {
        "status": "QUIESCENT_WITHIN_CGROUP",
        "basis": "CGROUP_V2_EMPTY_FROZEN_VERIFIED",
        "cgroup_path": context.cgroup_path,
        "cgroup_id": context.cgroup_id,
    }


def test_success_requires_terminal_digests_and_offline_verification(tmp_path):
    feature, context, _config, failures = _fixture(tmp_path)
    handle = feature.start_execution(context)

    status = handle.status()
    assert status["status"] == "READY"
    assert status["registration_cgroup_ids"] == [CGROUP_ID]
    assert status["offline_verified"] is False
    observer_config = json.loads((tmp_path / "audit/system-effects-config.json").read_bytes())
    assert observer_config["protocol_version"] == 2
    assert observer_config["deployment"]["configuration"] == {
        "root_pid": os.getpid(),
        "expected_cgroup_path": CGROUP_RELATIVE,
        "require_dedicated_cgroup": True,
        "limits": {
            "max_proc_entries": 131072,
            "max_cgroup_nodes": 4096,
            "max_cgroup_depth": 64,
            "max_open_fds_per_process": 8192,
        },
    }
    assert (tmp_path / "audit/system-effects-config.json").stat().st_mode & 0o777 == 0o600

    result = handle.finish(outcome="completed", quiescence=_quiescence(context))

    assert result["status"] == "FINISHED"
    assert result["observer_evidence_status"] == "INCOMPLETE"
    assert result["offline_verification"] == "VERIFIED"
    assert (
        json.loads((tmp_path / "audit/system-effects-verification.json").read_bytes())["verified"]
        is True
    )
    expectation = json.loads(
        (tmp_path / "audit/system-effects-verification-expectation.json").read_bytes()
    )
    assert expectation["expected_config"] == observer_config
    assert json.loads((tmp_path / "audit/system-effects-receipt.json").read_bytes()) == result
    assert feature.status()["active_runs"] == 0
    assert feature.status()["observation_slot_busy"] is False
    assert failures == []
    assert handle.finish(outcome="completed", quiescence=_quiescence(context)) == result


def test_linux_state_namespaces_are_bound_to_the_frozen_run(tmp_path):
    template = {
        "protocol_version": 1,
        "mounts": {
            "namespace": {"pid": 0, "expected_namespace_inode": 11},
            "template_marker": "mount",
        },
        "network": {
            "namespace": {"pid": 0, "expected_namespace_inode": 12},
            "template_marker": "network",
        },
    }
    feature, context, _config, _failures = _fixture(tmp_path, linux_scope=template)
    handle = feature.start_execution(context)

    observer_config = json.loads((tmp_path / "audit/system-effects-config.json").read_bytes())
    scope = observer_config["linux_state_scope"]
    assert scope["mounts"]["namespace"] == {
        "pid": context.root_pid,
        "expected_namespace_inode": context.mount_namespace_id,
    }
    assert scope["network"]["namespace"] == {
        "pid": context.root_pid,
        "expected_namespace_inode": context.network_namespace_id,
    }
    assert scope["mounts"]["template_marker"] == "mount"
    assert scope["network"]["template_marker"] == "network"
    assert template["mounts"]["namespace"]["expected_namespace_inode"] == 11

    handle.finish(outcome="completed", quiescence=_quiescence(context))


def test_ready_cgroup_binding_mismatch_is_rejected_and_slot_released(tmp_path):
    feature, context, _config, _failures = _fixture(tmp_path, observer_mode="ready-mismatch")

    with pytest.raises(WorkspaceError, match="READY binding mismatch"):
        feature.start_execution(context)

    assert feature.status()["active_runs"] == 0
    assert feature.status()["observation_slot_busy"] is False


def test_manifest_namespace_binding_mismatch_is_rejected(tmp_path):
    feature, context, _config, _failures = _fixture(
        tmp_path, observer_mode="namespace-mismatch"
    )

    with pytest.raises(WorkspaceError, match="exact Allox root process"):
        feature.start_execution(context)

    assert feature.status()["active_runs"] == 0
    assert feature.status()["observation_slot_busy"] is False


def test_unexpected_observer_exit_notifies_core_and_fails_health(tmp_path):
    feature, context, _config, failures = _fixture(tmp_path, observer_mode="crash")
    handle = feature.start_execution(context)
    deadline = time.monotonic() + 2
    while not failures and time.monotonic() < deadline:
        time.sleep(0.01)

    assert failures == ["deployment-observe exited unexpectedly"]
    with pytest.raises(WorkspaceError, match="not live and healthy"):
        handle.assert_healthy()
    handle.close_unverified()
    assert feature.status()["observation_slot_busy"] is False


def test_terminal_digest_mismatch_fails_before_offline_receipt(tmp_path):
    feature, context, _config, _failures = _fixture(tmp_path, observer_mode="bad-report-digest")
    handle = feature.start_execution(context)

    with pytest.raises(WorkspaceError, match="report digest mismatch"):
        handle.finish(outcome="completed", quiescence=_quiescence(context))

    assert not (tmp_path / "audit/system-effects-receipt.json").exists()
    assert not (tmp_path / "audit/system-effects-verification.json").exists()


def test_unverified_offline_result_is_rejected_even_with_valid_terminal(tmp_path):
    feature, context, _config, _failures = _fixture(tmp_path, verified=False)
    handle = feature.start_execution(context)

    with pytest.raises(WorkspaceError, match="did not verify the exact run"):
        handle.finish(outcome="completed", quiescence=_quiescence(context))

    assert not (tmp_path / "audit/system-effects-receipt.json").exists()


def test_preexisting_evidence_directory_is_never_reused(tmp_path):
    feature, context, _config, _failures = _fixture(tmp_path)
    evidence = tmp_path / "audit/system-effects"
    evidence.mkdir(mode=0o700)
    sentinel = evidence / "sentinel"
    sentinel.write_text("keep")

    with pytest.raises(WorkspaceError, match="already exists"):
        feature.start_execution(context)

    assert sentinel.read_text() == "keep"
    assert feature.status()["observation_slot_busy"] is False


def test_plugin_rejects_unknown_trusted_configuration(tmp_path):
    _feature, _context, config, _failures = _fixture(tmp_path)
    config["workload_override"] = "forbidden"

    with pytest.raises(WorkspaceError, match="unknown system-effects configuration"):
        SystemEffectsFeature(context=SimpleNamespace(), config=config)


def test_second_run_cannot_overlap_global_endpoint_snapshots(tmp_path):
    feature, context, config, _failures = _fixture(tmp_path)
    handle = feature.start_execution(context)
    config["ready_timeout"] = 0.1
    feature._config = feature._config.__class__.from_mapping(config)
    second_audit = tmp_path / "audit-2"
    second_cgroup = tmp_path / "cgroup-2"
    second_audit.mkdir(mode=0o700)
    second_cgroup.mkdir(mode=0o700)
    second = SimpleNamespace(
        **{
            **vars(context),
            "run_id": "run-2",
            "audit_directory": str(second_audit),
            "cgroup_path": str(second_cgroup),
        }
    )

    with pytest.raises(WorkspaceError, match="overlapping global snapshots"):
        feature.start_execution(second)

    assert feature.status()["global_run_serialization"] == "HOST_WIDE_FLOCK"
    handle.close_unverified()


def test_separate_daemon_instances_share_the_host_wide_lock(tmp_path):
    first, context, config, _failures = _fixture(tmp_path)
    first_handle = first.start_execution(context)

    second_config = dict(config)
    second_config["ready_timeout"] = 0.1
    second = SystemEffectsFeature(context=SimpleNamespace(), config=second_config)
    second_audit = tmp_path / "audit-2"
    second_cgroup = tmp_path / "cgroup-2"
    second_audit.mkdir(mode=0o700)
    second_cgroup.mkdir(mode=0o700)
    second_context = SimpleNamespace(
        **{
            **vars(context),
            "run_id": "run-2",
            "audit_directory": str(second_audit),
            "cgroup_path": str(second_cgroup),
        }
    )

    with pytest.raises(WorkspaceError, match="host lock timed out"):
        second.start_execution(second_context)

    assert second.status()["observation_slot_busy"] is False
    assert second.status()["host_lock_path"] == config["host_lock_path"]
    first_handle.close_unverified()

    second_handle = second.start_execution(second_context)
    assert second_handle.status()["status"] == "READY"
    second_handle.close_unverified()


def test_host_lock_contends_with_a_separate_process(tmp_path):
    feature, context, config, _failures = _fixture(tmp_path)
    config["ready_timeout"] = 0.1
    feature._config = feature._config.__class__.from_mapping(config)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl,os,sys; "
                "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); "
                "fcntl.flock(fd,fcntl.LOCK_EX); "
                "print('LOCKED',flush=True); "
                "sys.stdin.buffer.read(1)"
            ),
            config["host_lock_path"],
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    assert holder.stdout is not None
    assert holder.stdout.readline() == b"LOCKED\n"
    try:
        with pytest.raises(WorkspaceError, match="host lock timed out"):
            feature.start_execution(context)
    finally:
        assert holder.stdin is not None
        holder.stdin.write(b"x")
        holder.stdin.close()
        assert holder.wait(timeout=2) == 0

    handle = feature.start_execution(context)
    handle.close_unverified()


@pytest.mark.parametrize("kind", ["symlink", "directory", "public-file"])
def test_host_lock_rejects_unsafe_file_types_and_permissions(tmp_path, kind):
    _feature, _context, config, _failures = _fixture(tmp_path)
    lock = tmp_path / f"unsafe-{kind}.lock"
    if kind == "symlink":
        target = tmp_path / "lock-target"
        target.write_text("")
        target.chmod(0o600)
        lock.symlink_to(target)
    elif kind == "directory":
        lock.mkdir(mode=0o700)
    else:
        lock.write_text("")
        lock.chmod(0o644)
    config["host_lock_path"] = str(lock)

    with pytest.raises(WorkspaceError, match="host lock must be a private singly-linked"):
        SystemEffectsFeature(context=SimpleNamespace(), config=config)
