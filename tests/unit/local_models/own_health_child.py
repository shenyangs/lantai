"""Owned finite Python process; records identity and sleeps. Not archive code."""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

marker, token, duration, behavior = sys.argv[1:]
if behavior == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
path = Path(marker)
temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
temporary.write_text(json.dumps(dict(pid=os.getpid(), pgid=os.getpgid(0), ppid=os.getppid(), token=token)) + "\n")
os.replace(temporary, path)
time.sleep(float(duration))
