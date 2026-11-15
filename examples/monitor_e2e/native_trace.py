#!/usr/local/bin/python3
"""Opt-in syscall timing wrapper for only our owned Observer / verifier."""
import os
import sys
from pathlib import Path

kind = 'verifier' if Path(__file__).name.endswith('verifier') else 'observer'
target = '/opt/observer/deployment-observe'+('-verify' if kind == 'verifier' else '')
flag = '--expectation' if kind == 'verifier' else '--config'
config = Path(sys.argv[sys.argv.index(flag)+1])
log = config.parent / (kind+'-syscalls.log')
os.environ['LD_LIBRARY_PATH'] = '/opt/diagnostic-tools/lib'
os.execv('/opt/diagnostic-tools/strace', ['strace', '-f', '-qq', '-ttt', '-T', '-s', '64',
    '-e', 'trace=bpf,fsync,fdatasync,close,clock_nanosleep,nanosleep,poll,ppoll,epoll_wait',
    '-o', str(log), target, *sys.argv[1:]])
