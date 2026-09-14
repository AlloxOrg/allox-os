"""Session-bound generic TCP connector for use as a ProxyCommand."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading

_MAX_CONTROL = 4096


def _control_response(stream: socket.socket) -> dict:
    data = bytearray()
    while not data.endswith(b"\n"):
        chunk = stream.recv(1)
        if not chunk:
            raise ConnectionError("network broker closed during setup")
        data.extend(chunk)
        if len(data) >= _MAX_CONTROL:
            raise ValueError("network broker response is too large")
    return json.loads(data)


def _stdin_to_socket(stream: socket.socket) -> None:
    try:
        while True:
            chunk = os.read(0, 65536)
            if not chunk:
                break
            stream.sendall(chunk)
    except OSError:
        pass
    try:
        stream.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def connect(host: str, port: int, socket_path: str) -> int:
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        stream.connect(socket_path)
        stream.sendall(json.dumps({"host": host, "port": port}).encode() + b"\n")
        if not _control_response(stream).get("ok"):
            print("allox-network: broker could not connect to target", file=sys.stderr)
            return 1
        upload = threading.Thread(target=_stdin_to_socket, args=(stream,), daemon=True)
        upload.start()
        while True:
            chunk = stream.recv(65536)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(1, view)
                view = view[written:]
        return 0
    except (OSError, ValueError) as exc:
        print(f"allox-network: {exc}", file=sys.stderr)
        return 1
    finally:
        stream.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    connector = subparsers.add_parser("connect", help="forward stdin/stdout to a TCP target")
    connector.add_argument("host")
    connector.add_argument("port", type=int)
    connector.add_argument(
        "--socket",
        default=os.environ.get("ALLOX_NETWORK_SOCKET", "/run/allox/network.sock"),
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("allox-network: port must be in 1..65535", file=sys.stderr)
        return 2
    return connect(args.host, args.port, args.socket)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
