"""Offline query over existing monitoring records."""

import argparse
import json
import sys
from pathlib import Path

from allox.workspace.store import WorkspaceError
from allox_trace_correlation.correlate import STREAMS, Correlator


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-root", required=True, type=Path)
    parser.add_argument("--audit-root", required=True, type=Path)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--trace-id")
    parser.add_argument("--stream", choices=STREAMS, default=STREAMS[0])
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        result = Correlator(args.trace_root, args.audit_root).run(
            args.agent_id, args.session_id, args.run_id, trace_id=args.trace_id,
            stream=args.stream, offset=args.offset, limit=args.limit,
        )
    except (WorkspaceError, OSError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
