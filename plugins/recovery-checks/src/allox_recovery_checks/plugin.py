"""Management-only assertions with durable, rollback-external admission fences."""
import json
import os
import sqlite3
import threading
from pathlib import Path

from allox.runtime.extensions import ExecutionContribution
from allox.workspace.session_memory import snapshot
from allox.workspace.store import WorkspaceError
from allox.workspace.store import validate_id as _validate_id
from allox_recovery_checks.assertions import compare, tree_manifest
from allox_recovery_checks.history import check_history

MUTATIONS = frozenset({"checkpoint.create", "checkpoint.delete", "session.rollback",
                       "session.rollback_after_runtime_reset", "memory.restore"})


def validate_id(kind, value):
    if not isinstance(value, str):
        raise WorkspaceError(f"{kind} requires a string identifier")
    return _validate_id(kind, value)


class RecoveryChecksFeature:
    name = "recovery-checks"
    abi = 1
    rpc_prefixes = ("recovery.",)

    def __init__(self, *, context, config):
        self.context = context
        self.store = context.store
        root = Path(config["root"])
        if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
            raise WorkspaceError("recovery evidence root must be absolute and not symlinked")
        workspace = self.store.root.resolve()
        if root.resolve() == workspace or workspace in root.resolve().parents:
            raise WorkspaceError("recovery evidence must be outside workspace rollback storage")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        database = root / "checks.sqlite3"
        if database.is_symlink() or (database.exists() and not database.is_file()):
            raise WorkspaceError("recovery database must be a regular file")
        self.lock = threading.RLock()
        self.local = threading.local()
        self.db = sqlite3.connect(database, check_same_thread=False)
        os.chmod(database, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (agent TEXT, session TEXT, "
                        "kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(agent,session,kind,id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)")
        bound = self.db.execute("SELECT value FROM metadata WHERE name='workspace'").fetchone()
        if bound is not None and bound[0] != str(workspace):
            self.db.close()
            raise WorkspaceError("recovery evidence root is bound to a different workspace store")
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('workspace',?)", (str(workspace),))
        self.db.commit()
        self.fenced = set(self.db.execute("SELECT agent,session FROM records WHERE kind='fence'"))

    def _get(self, key, kind, identity="state"):
        row = self.db.execute("SELECT value FROM records WHERE agent=? AND session=? "
                              "AND kind=? AND id=?", (*key, kind, identity)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, key, kind, value, identity="state"):
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 8 * 1024 * 1024:
            raise WorkspaceError("recovery evidence exceeds bound")
        if kind == "fence":
            self.fenced.add(key)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)",
                            (*key, kind, identity, encoded))

    def _ready(self, key):
        # The management mutation that owns the persistent fence must be able
        # to enter Core's lease boundary; other threads cannot borrow this.
        if getattr(self.local, "owner", None) == key:
            return
        # Do NOT acquire the plugin lock here: Core calls this while holding
        # the registry lock, whereas management holds plugin -> registry.
        if key in self.fenced:
            raise WorkspaceError("recovery verification pending or failed; session remains fenced")

    def assert_session_ready(self, agent_id, session_id):
        self._ready((agent_id, session_id))

    def prepare_execution(self, agent_id, session_id):
        self._ready((agent_id, session_id))
        return ExecutionContribution()

    def _clear(self, key):
        with self.db:
            self.db.execute("DELETE FROM records WHERE agent=? AND session=? AND kind='fence'", key)
        self.fenced.discard(key)

    def _effects(self, key):
        return self._get(key, "effects") or {}

    def _semantic(self, key, baseline=None):
        coverage = self._get(key, "coverage")
        effects = self._effects(key)
        if not coverage or (baseline is not None and coverage != baseline["coverage"]):
            return {"status": "INCONCLUSIVE", "reason": "complete trusted adapter coverage absent"}
        if any(x["state"] == "pending" for x in effects.values()):
            return {"status": "INCONCLUSIVE", "reason": "inflight or uncertain external operation"}
        if baseline is not None:
            committed = {k: v for k, v in effects.items() if v["state"] == "committed"}
            if committed != baseline["committed"]:
                return {"status": "INCONSISTENT_WITHIN_SCOPE",
                        "reason": "rollback would discard a committed external operation"}
        return {"status": "CONSISTENT_WITHIN_SCOPE", "basis": "TRUSTED_ADAPTER_LEDGER",
                "coverage": coverage, "unrecorded_effects_checked": False}

    def _baseline(self, key, checkpoint_id):
        value = self._get(key, "checkpoint", checkpoint_id)
        if value is None:
            raise WorkspaceError("checkpoint has no recovery assertion baseline")
        return value

    def _capture(self, key, result):
        cp = result["checkpoint_id"]
        memory = self.store.memory_read(*key)
        effects = self._effects(key)
        if any(x["state"] == "pending" for x in effects.values()):
            raise WorkspaceError("checkpoint cannot bind an unresolved external operation")
        files = tree_manifest(self.store.checkpoints(*key) / cp)
        scrubbed = {p: row for p, row in files.items()
                    if not (p.startswith(".allox-tmp/") and row["type"] in {"socket-node", "fifo-node"})}
        self._put(key, "checkpoint", {
            "files": files, "scrubbed_files": scrubbed,
            "memory_version": memory["memory_version"],
            "memory_state_sha256": snapshot(*key, memory["state"])[1],
            "coverage": self._get(key, "coverage"),
            "committed": {k: v for k, v in effects.items() if v["state"] == "committed"},
        }, cp)

    def _runtime(self, key):
        cgroups = self.context.cgroups
        if cgroups is None:
            return {"status": "NOT_CHECKED", "reason": "no managed process service"}
        return {"status": "INCONSISTENT_WITHIN_SCOPE" if cgroups.populated(*key)
                else "CONSISTENT_WITHIN_SCOPE", "basis": "SESSION_CGROUP_EMPTY",
                "restores_checkpoint_processes": False}

    def _verify(self, key, checkpoint_id, *, scrub_runtime):
        baseline = self._baseline(key, checkpoint_id)
        files = compare(baseline["scrubbed_files" if scrub_runtime else "files"],
                        tree_manifest(self.store.current(*key)))
        memory = self.store.memory_read(*key)
        memory_ok = (memory["memory_version"] == baseline["memory_version"]
                     and snapshot(*key, memory["state"])[1] == baseline["memory_state_sha256"])
        bound = self.store.describe(*key)["checkpoint_head"] == checkpoint_id
        checks = {"checkpoint_binding": {"status": "CONSISTENT_WITHIN_SCOPE" if bound
                                         else "INCONSISTENT_WITHIN_SCOPE"},
                  "filesystem": files,
                  "memory": {"status": "CONSISTENT_WITHIN_SCOPE" if memory_ok
                             else "INCONSISTENT_WITHIN_SCOPE"},
                  "runtime_reset": self._runtime(key),
                  "semantic": self._semantic(key, baseline)}
        expected = baseline["scrubbed_files" if scrub_runtime else "files"]
        if any(row["type"] in {"socket-node", "fifo-node"} for row in expected.values()):
            checks["runtime_reset"] = {"status": "INCONCLUSIVE",
                                       "reason": "checkpoint runtime nodes require service recreation"}
        statuses = {value["status"] for value in checks.values()}
        status = ("INCONSISTENT_WITHIN_SCOPE" if "INCONSISTENT_WITHIN_SCOPE" in statuses
                  else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses
                  else "CONSISTENT_WITHIN_SCOPE")
        result = {"schema": "allox.recovery-checks/v1", "agent_id": key[0], "session_id": key[1],
                  "checkpoint_id": checkpoint_id, "status": status, "checks": checks,
                  "scope": [k for k, v in checks.items() if v["status"] != "NOT_CHECKED"],
                  "excluded": ["process heap restoration", "external service state",
                               "unrecorded effects", "unmanaged writers", "xattrs/ACLs",
                               "timestamps", "mounts", "workspace root metadata"]}
        self._put(key, "report", result)
        return result

    def management_mutation(self, action, params, operation):
        if action not in MUTATIONS:
            return operation()
        key = (validate_id("agent", params.get("agent_id")),
               validate_id("session", params.get("session_id")))
        with self.lock:
            if action == "memory.restore":
                raise WorkspaceError("recovery-checks v1 requires joint session.rollback, not memory.restore")
            rollback = action.startswith("session.rollback")
            if not rollback:
                self._ready(key)
            if rollback:
                cp = validate_id("checkpoint", params.get("checkpoint_id"))
                if type(params.get("scrub_runtime", True)) is not bool:
                    raise WorkspaceError("scrub_runtime must be a boolean")
                if params.get("num_ancestors") is not None or params.get("restore_memory", True) is not True:
                    raise WorkspaceError("recovery-checks requires an exact joint checkpoint target")
                baseline = self._baseline(key, cp)
                semantic = self._semantic(key, baseline)
                if semantic["status"] != "CONSISTENT_WITHIN_SCOPE":
                    raise WorkspaceError("semantic rollback rejected: " + semantic["reason"])
                # Preflight is BEFORE Core kills any processes or changes files.
                if self.context.cgroups is not None and params.get("kill_processes") is not True:
                    raise WorkspaceError("verified rollback requires explicit kill_processes=true")
                expected = baseline["scrubbed_files" if params.get("scrub_runtime", True)
                                    else "files"]
                actual = tree_manifest(self.store.checkpoints(*key) / cp,
                                       scrub_runtime=params.get("scrub_runtime", True))
                if compare(expected, actual)["status"] != "CONSISTENT_WITHIN_SCOPE":
                    raise WorkspaceError("checkpoint assertion baseline no longer matches snapshot")
            else:
                cp = params.get("checkpoint_id")
                if any(x["state"] == "pending" for x in self._effects(key).values()):
                    raise WorkspaceError("checkpoint mutation has an unresolved external operation")
            self._put(key, "fence", {"action": action, "checkpoint_id": cp,
                                     "scrub_runtime": params.get("scrub_runtime", True)})
            if rollback:
                self._put(key, "report", {"checkpoint_id": cp, "status": "INCONCLUSIVE",
                                          "phase": "AWAITING_POST_RESTORE_VERIFICATION"})
            self.local.owner = key
            try:
                result = operation()
                if action == "checkpoint.create":
                    self._capture(key, result)
                elif rollback:
                    report = self._verify(key, cp, scrub_runtime=params.get("scrub_runtime", True))
                    if report["status"] != "CONSISTENT_WITHIN_SCOPE":
                        raise WorkspaceError("restore verification failed; session remains fenced")
                    result["recovery_checks"] = report
                else:
                    with self.db:
                        self.db.execute("DELETE FROM records WHERE agent=? AND session=? "
                                        "AND kind='checkpoint' AND id=?", (*key, cp))
                self._clear(key)
                return result
            finally:
                # Any exception or daemon crash leaves the persistent fence.
                self.local.owner = None

    def dispatch(self, action, params):
        if action == "recovery.admit":
            return check_history(params.get("outcomes"), params.get("allowed"))
        key = (validate_id("agent", params.get("agent_id")),
               validate_id("session", params.get("session_id")))
        with self.lock:
            self.store.describe(*key)
            if action == "recovery.status":
                return {"enabled": True, "fence": self._get(key, "fence"),
                        "report": self._get(key, "report"), "coverage": self._get(key, "coverage")}
            if action == "recovery.coverage":
                self._ready(key)
                adapter = validate_id("adapter", params.get("adapter_id"))
                # A trusted adapter contract, NOT inferred from traces or eBPF.
                if params.get("complete") is not True:
                    raise WorkspaceError("coverage requires complete=true from a trusted adapter")
                if (self._get(key, "coverage") or self._effects(key)
                        or self.store.list_checkpoints(*key)):
                    raise WorkspaceError("coverage must be fixed before the first effect/checkpoint")
                value = {"adapter_id": adapter, "contract": "complete-effect-ledger/v1"}
                self._put(key, "coverage", value)
                return value
            if action in {"recovery.effect.prepare", "recovery.effect.settle"}:
                self._ready(key)
                identity = validate_id("operation", params.get("operation_id"))
                effects = self._effects(key)
                if action.endswith("prepare"):
                    fingerprint = params.get("fingerprint")
                    if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                            or any(c not in "0123456789abcdef" for c in fingerprint)):
                        raise WorkspaceError("effect fingerprint must be SHA256, not raw arguments")
                    if identity in effects:
                        if effects[identity]["fingerprint"] != fingerprint:
                            raise WorkspaceError("operation identity reused for a different effect")
                        return {"dispatch_new": False, "state": effects[identity]["state"],
                                "requires_provider_reconciliation": True}
                    if len(effects) >= 4096:
                        raise WorkspaceError("effect ledger bound exceeded")
                    effects[identity] = {"fingerprint": fingerprint, "state": "pending"}
                else:
                    if identity not in effects or effects[identity]["state"] != "pending":
                        raise WorkspaceError("settlement requires an existing pending operation")
                    outcome = params.get("outcome")
                    if outcome not in {"committed", "aborted"}:
                        raise WorkspaceError("unknown outcomes must remain pending until reconciled")
                    effects[identity]["state"] = outcome
                self._put(key, "effects", effects)
                return {"recorded": True, "state": effects[identity]["state"],
                        "authorizes_effects": False}
            if action == "recovery.verify":
                fence = self._get(key, "fence")
                if not fence or not fence["action"].startswith("session.rollback"):
                    raise WorkspaceError("only an interrupted rollback can be reverified")
                self.local.owner = key
                try:
                    with self.context.executions.mutation(*key):
                        # Reverification never clears a fence from a different target.
                        report = self._verify(key, fence["checkpoint_id"],
                                              scrub_runtime=fence.get("scrub_runtime", True))
                        if report["status"] == "CONSISTENT_WITHIN_SCOPE":
                            self._clear(key)
                        return report
                finally:
                    self.local.owner = None
        raise WorkspaceError("unknown recovery action")

    def close(self):
        with self.lock:
            self.db.close()
