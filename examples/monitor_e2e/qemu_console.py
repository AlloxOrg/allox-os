#!/usr/local/bin/python3.10
"""Per-Guest diagnostic wrapper; preserve Kata devices and add a private UART log."""
import os
import sys
from pathlib import Path

REAL = '/data/hy/sync/agentos/agent-sandboxes/kata-3.10.0/extracted/opt/kata/bin/qemu-system-x86_64'
args = sys.argv[1:]
if '-kernel' in args:
    log = 'file:'+str(Path(__file__).resolve().parent / 'serial.log')
    if '-serial' in args:
        args[args.index('-serial')+1] = log
    else:
        args += ['-serial', log]
    pos = args.index('-append')+1
    args[pos] += ' console=ttyS0 earlyprintk=serial,ttyS0,115200'
os.execv(REAL, [REAL, *args])
