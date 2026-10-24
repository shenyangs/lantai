#!/usr/bin/env python3
"""Preview/apply a reviewed span-classification plan; never classify by regex."""
import argparse
import json
from pathlib import Path

from lantai.agents.memory.classification_repair import apply_classification_repair, rollback_classification_repair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--plan", type=Path)
    operation.add_argument("--rollback", metavar="BATCH_ID")
    parser.add_argument("--agent-id", default="default")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Confirm the target app and all memory writers are quiesced")
    args = parser.parse_args()
    if args.rollback:
        result = rollback_classification_repair(args.workspace, args.rollback, apply=args.apply, offline=args.offline)
    else:
        result = apply_classification_repair(args.workspace, json.loads(args.plan.read_text(encoding="utf-8")), apply=args.apply, offline=args.offline, agent_id=args.agent_id)
    print(json.dumps({key: value for key, value in result.items() if key not in {"backup_hashes", "applied_hashes"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
