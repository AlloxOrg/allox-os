"""Small stdlib-only tool, also mounted into Bubblewrap at /run/allox/share.py."""

from __future__ import annotations

import argparse
import base64
import json
import socket
import sys


def call(action, params, endpoint="/run/allox/share.sock"):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(15)
        connection.connect(endpoint)
        connection.sendall(json.dumps({"action": action, "params": params}).encode() + b"\n")
        with connection.makefile("rb") as stream:
            line = stream.readline(512 * 1024 + 1)
        if len(line) > 512 * 1024 or not line.endswith(b"\n"):
            raise ValueError("invalid share response")
        response = json.loads(line)
        if not response.get("ok"):
            raise ValueError(response.get("error", "share request failed"))
        return response["result"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    enable = commands.add_parser("enable")
    enable.add_argument("--scope", default=".")
    enable.add_argument("--permission", choices=("read", "write"), default="read")
    commands.add_parser("disable")
    commands.add_parser("status")
    for action in ("list", "read", "write"):
        sub = commands.add_parser(action)
        sub.add_argument("target", help="target Agent/Session ID, e.g. agent-b/session-1")
        sub.add_argument("path", nargs="?" if action == "list" else None,
                         default=".", help="path relative to the target's shared scope")
    args = parser.parse_args(argv)
    params = {}
    try:
        if args.action == "enable":
            params = {"scope": args.scope, "permission": args.permission}
        if args.action in {"list", "read", "write"}:
            parts = args.target.split("/")
            if len(parts) != 2 or not all(parts):
                raise ValueError("target must be agent_id/session_id")
            params = {"target_agent_id": parts[0], "target_session_id": parts[1], "path": args.path}
        if args.action == "write":
            data = sys.stdin.buffer.read(256 * 1024 + 1)
            if len(data) > 256 * 1024:
                raise ValueError("file exceeds 256 KiB")
            params["data_base64"] = base64.b64encode(data).decode("ascii")
        result = call(args.action, params)
        if args.action == "read":
            sys.stdout.buffer.write(base64.b64decode(result["data_base64"], validate=True))
        else:
            print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
