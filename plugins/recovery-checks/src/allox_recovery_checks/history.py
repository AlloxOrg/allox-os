"""Independent finite-word specialization of prefix-robust history admission.

Concept: agent-check-restore-safety/artifact/exact_history_realization.py.
Not imported/vendored; no claim to the upstream Lean proof or full edit model.
Inputs enumerate ALL completions of each declared outcome. This is a model
checker, not evidence that a live Agent implements that model.
"""
from allox.workspace.store import WorkspaceError


def check_history(outcomes, allowed):
    def word(value):
        if (not isinstance(value, list) or len(value) > 8
                or any(not isinstance(x, str) or not 0 < len(x) <= 128 for x in value)):
            raise WorkspaceError("history words require at most 8 bounded identifiers")
        return tuple(value)

    if (not isinstance(outcomes, list) or not 1 <= len(outcomes) <= 16
            or not isinstance(allowed, list) or len(allowed) > 256):
        raise WorkspaceError("history model exceeds outcome/policy bounds")
    families = []
    for outcome in outcomes:
        if not isinstance(outcome, list) or not 1 <= len(outcome) <= 16:
            raise WorkspaceError("each outcome requires 1..16 completions")
        families.append(set(map(word, outcome)))
    complete = set().union(*families)
    if any(a != b and b[:len(a)] == a for a in complete for b in complete):
        raise WorkspaceError("complete words must be prefix-free")
    def prefixes(trace):
        return {trace[:i] for i in range(len(trace) + 1)}
    policy = set().union({()}, *(prefixes(word(x)) for x in allowed))
    compatible = [set().union(*(prefixes(t) for t in f)) for f in families]
    current = {(i, t) for i, f in enumerate(families) for t in f if t in policy}
    chain, causes = [len(current)], []
    while current:
        witnesses = {(i, p) for i, t in current for p in prefixes(t)}
        removed = set()
        for item in sorted(current):
            _, trace = item
            missing = next(((list(p), i) for p in sorted(prefixes(trace))
                            for i, raw in enumerate(compatible)
                            if p in raw and (i, p) not in witnesses), None)
            if missing is not None:
                removed.add(item)
                causes.append({"rank": len(chain), "outcome": item[0],
                               "trace": list(trace), "prefix": missing[0],
                               "missing_outcome": missing[1]})
        if not removed:
            break
        current -= removed
        chain.append(len(current))
    return {"admitted": bool(current), "scope": "DECLARED_FINITE_WORD_MODEL_ONLY",
            "authorizes_effects": False, "descending_chain": chain,
            "survivors": [{"outcome": i, "trace": list(t)} for i, t in sorted(current)],
            "pruning_causes": causes}
