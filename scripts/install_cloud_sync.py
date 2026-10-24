#!/usr/bin/env python3
"""Install the macOS user LaunchAgent for an independent sync checkout."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--observe-source", action="append", default=[])
    parser.add_argument("--state-dir", type=Path,
                        default=Path.home() / "Library/Application Support/LantaiCloudSync")
    parser.add_argument("--no-load", action="store_true", help="write configuration only")
    args = parser.parse_args()
    repo = args.repo.resolve()
    state = args.state_dir.resolve()
    if state == repo or repo in state.parents:
        parser.error("runtime state must be outside the source checkout")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    runner = state / "cloud_sync.py"
    shutil.copyfile(Path(__file__).with_name("cloud_sync.py"), runner)
    os.chmod(runner, 0o600)
    config = state / "config.json"
    config.write_text(json.dumps({
        "repo": str(repo), "state_dir": str(state), "remote": "origin",
        "channel": "codex/cloud-sync", "local_branch": "codex/cloud-sync",
        "follow_main": True,
        "observe_sources": [str(Path(p).resolve()) for p in args.observe_source],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(config, 0o600)
    label = "com.lantai.cloud-sync"
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / (label + ".plist")
    data = {"Label": label, "ProgramArguments": [sys.executable, str(runner), "once",
                                                "--config", str(config)],
            "RunAtLoad": True, "StartInterval": 45,
            "WorkingDirectory": str(state), "ProcessType": "Background",
            "StandardOutPath": str(state / "agent.log"),
            "StandardErrorPath": str(state / "agent-errors.log"),
            # launchd has no interactive shell; Git credential helpers need
            # the same installed executables available to the user's terminal.
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "GIT_TERMINAL_PROMPT": "0"}}
    with plist.open("wb") as handle:
        plistlib.dump(data, handle)
    os.chmod(plist, 0o600)
    if not args.no_load:
        domain = f"gui/{os.getuid()}"
        existing = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                                  capture_output=True)
        if existing.returncode == 0:
            subprocess.run(["launchctl", "bootout", domain, str(plist)], check=True,
                           capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True,
                       capture_output=True)
    print(json.dumps({"installed": True, "loaded": not args.no_load,
                      "repo": str(repo), "config": str(config), "plist": str(plist),
                      "interval_seconds": 45}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
