"""Run outside the source checkout with only the two installed wheels."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import allox
from allox.runtime.extensions import FeatureManager, WorkspaceFeatureContext

assert "package-env" in str(allox.__file__)
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    store = SimpleNamespace(root=root / "workspace")
    manager = FeatureManager(WorkspaceFeatureContext(store=store, executions=None, cgroups=None),
                             (("recovery-checks", {"root": root / "evidence"}),))
    try:
        assert manager.names == ("recovery-checks",)
        result = manager.dispatch("recovery.admit", {"outcomes": [[["safe"]]], "allowed": [["safe"]]})
        assert result["admitted"] and not result["authorizes_effects"]
        print(json.dumps({"entrypoint": "recovery-checks", "isolated_wheel_install": True,
                          "collector_dependencies": False, "result": result}, sort_keys=True))
    finally:
        manager.close()
