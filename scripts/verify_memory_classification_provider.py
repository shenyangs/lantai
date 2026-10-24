#!/usr/bin/env python3
"""Opt-in, two-turn ownership smoke using isolated configured credentials.

Only synthetic fixtures are sent. No live memories, model text, or credentials
are printed or modified; the temporary credential copy is removed on exit.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


async def worker():
    from lantai.agents.memory.structured_user_memory import (
        StructuredUserMemoryStore, extract_structured_user_memory,
    )
    from lantai.runtime.converters import LegacyMsg

    store = StructuredUserMemoryStore(Path(os.environ["LANTAI_WORKING_DIR"]) / "workspaces" / "default")
    first = "我是林舟，职业是编辑，长期偏好简洁中文。请记住：桥计划是开源图谱项目，目标是把博物馆藏品以开放接口公开。这些设定属于项目，不是我的身份。"
    second = "补充上文：桥计划的演示文档需要英文技术术语；这是该项目的要求，不是我的全局表达偏好。我的全局偏好仍是简洁中文。"
    results = []
    messages = []
    for text in (first, second):
        messages.append(LegacyMsg(name="user", role="user", content=text))
        candidates = await extract_structured_user_memory("default", messages, store)
        assert candidates, "Configured model returned no usable candidates"
        results.append(candidates)
        for item in candidates:
            store.add_entry(item.content, item.type, subject=item.subject, scope=item.scope, scope_id=item.scope_id)
    assert any(item.subject == "project" for item in results[0]), "Project ownership lost"
    assert any(item.subject == "user" and item.type == "identity" for item in results[0]), "Personal identity missing"
    classifications = [[(item.subject, item.scope, item.type) for item in items] for items in results]
    assert any(item.scope == "project" and item.type in {"style", "instruction"} for item in results[1]), f"Project-specific expression requirement lost: {classifications}"
    assert not any(item.subject == "user" and item.scope == "global" and "英文" in item.content for item in results[1]), "Project style contaminated global preference"
    print(json.dumps({"status": "PASS", "turns": 2, "candidate_counts": [len(items) for items in results], "classifications": classifications, "synthetic_only": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-working-dir", type=Path)
    parser.add_argument("--source-secret-dir", type=Path)
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    if args.worker:
        asyncio.run(worker())
        return
    if not args.source_working_dir or not args.source_secret_dir:
        parser.error("Both source directories are required; this makes real provider calls")
    from source_configured_provider_stream_smoke import _stage_profile

    with tempfile.TemporaryDirectory(prefix="lantai-classification-provider-") as temp:
        working, secret, _ = _stage_profile(source_working_dir=args.source_working_dir, source_secret_dir=args.source_secret_dir, agent_id=args.agent_id, target_root=Path(temp))
        env = dict(os.environ, LANTAI_WORKING_DIR=str(working), LANTAI_SECRET_DIR=str(secret), LANTAI_DISABLE_KEYRING="1")
        completed = subprocess.run([sys.executable, __file__, "--worker"], env=env, timeout=180, check=False)
        raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
