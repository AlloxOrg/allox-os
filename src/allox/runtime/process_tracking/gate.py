"""Stopped exec gate used to seed eBPF attribution before target code runs."""

from __future__ import annotations

import json
import os
import signal
import sys


def main() -> int:
    if len(sys.argv) != 1:
        return 125
    # The trusted parent moves this already-stopped process into the Session
    # cgroup and seeds the BPF map before allowing the target to execute.
    os.kill(os.getpid(), signal.SIGSTOP)
    config = json.load(sys.stdin)
    stdin = os.open(os.devnull, os.O_RDONLY)
    os.dup2(stdin, 0)
    os.close(stdin)
    os.chdir(config["cwd"])
    stdout = os.open(config["stdout"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    stderr = os.open(config["stderr"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(stdout, 1)
    os.dup2(stderr, 2)
    os.close(stdout)
    os.close(stderr)
    os.execvpe(config["argv"][0], config["argv"], config["env"])
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
