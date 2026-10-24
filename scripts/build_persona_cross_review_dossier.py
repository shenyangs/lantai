#!/usr/bin/env python3
"""Build a complete, human-readable dossier from persona review evidence.

The report deliberately includes both the invalid wrapper-contaminated run and
the corrected formal run. It joins every generated answer to the versioned
scenario, lexical diagnostic, and crossed semantic-review decision by turn id
and answer SHA-256. No API secrets or user data are read.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_DIR = (
    ROOT
    / "release"
    / "builds"
    / "2.0.0-8f07f7e65823-arm64"
    / "release-evidence"
)
DEFAULT_SUITE = (
    ROOT
    / "src"
    / "lantai"
    / "agents"
    / "md_files"
    / "constitutions"
    / "shen_xingzhi-multiturn-dual-axis-zh.yaml"
)
DEFAULT_OUTPUT = (
    ROOT
    / "docs"
    / "remediation"
    / "persona-cross-review-complete-record-2026-08-14.md"
)

RUNS = (
    {
        "key": "invalid-deepseek",
        "phase": "第一轮（无效，包装提示污染）",
        "benchmark": "persona-deepseek-benchmark-invalid-work-trigger-wrapper.json",
        "review": "persona-deepseek-reviewed-by-kimi-invalid-work-trigger-wrapper.json",
    },
    {
        "key": "invalid-kimi",
        "phase": "第一轮（无效，包装提示污染）",
        "benchmark": "persona-kimi-benchmark-invalid-work-trigger-wrapper.json",
        "review": "persona-kimi-reviewed-by-deepseek-invalid-work-trigger-wrapper.json",
    },
    {
        "key": "formal-deepseek",
        "phase": "第二轮（修正后正式证据）",
        "benchmark": "persona-deepseek-benchmark.json",
        "review": "persona-deepseek-reviewed-by-kimi.json",
    },
    {
        "key": "formal-kimi",
        "phase": "第二轮（修正后正式证据）",
        "benchmark": "persona-kimi-benchmark.json",
        "review": "persona-kimi-reviewed-by-deepseek.json",
    },
)
FORMAL_RUNS = RUNS[2:]


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"Expected JSON object: {path}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return "未记录"


def _yes_no(value: Any) -> str:
    return "通过" if value is True else "失败"


def _fmt_groups(groups: Any) -> str:
    if not groups:
        return "无"
    return "；".join(" / ".join(str(term) for term in group) for group in groups)


def _fmt_list(values: Any) -> str:
    if not values:
        return "无"
    return "；".join(str(item) for item in values)


def _code_block(value: str) -> str:
    # HTML escaping keeps nested Markdown/code in model answers inert and exact.
    return f"<pre>{html.escape(value)}</pre>"


def _scenario_index(suite_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    suite = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    scenarios: dict[str, Any] = {}
    turns: dict[str, Any] = {}
    for scenario in suite["scenarios"]:
        scenarios[scenario["id"]] = scenario
        for index, turn in enumerate(scenario["turns"], start=1):
            turns[f"{scenario['id']}:{index}"] = turn
    return scenarios, turns


def _lexical_index(benchmark: dict[str, Any]) -> dict[str, Any]:
    return {
        str(item["turn_id"]): item
        for item in benchmark.get("benchmark", {}).get("evaluations", [])
    }


def _evidence_inventory(paths: list[Path]) -> list[str]:
    rows = [
        "| 文件 | 字节 | SHA-256 |",
        "|---|---:|---|",
    ]
    for path in paths:
        rows.append(f"| `{path.name}` | {path.stat().st_size} | `{_sha256(path)}` |")
    return rows


def _run_section(
    *,
    spec: dict[str, str],
    evidence_dir: Path,
    scenarios: dict[str, Any],
    scenario_turns: dict[str, Any],
) -> tuple[list[str], int]:
    benchmark_path = evidence_dir / spec["benchmark"]
    review_path = evidence_dir / spec["review"]
    benchmark = _load_json(benchmark_path)
    review = _load_json(review_path)

    subject = benchmark["configured_model"]
    reviewer = review["reviewer"]
    summary = review["summary"]
    lexical_summary = benchmark["benchmark"]
    answers = {str(item["turn_id"]): item for item in benchmark["turns"]}
    decisions = {str(item["turn_id"]): item for item in review["reviews"]}
    lexical = _lexical_index(benchmark)

    if set(answers) != set(decisions) or len(answers) != 30:
        raise AssertionError(f"Incomplete joined record: {spec['key']}")
    for turn_id, answer in answers.items():
        if answer.get("answer_sha256") != decisions[turn_id].get("answer_sha256"):
            raise AssertionError(f"Answer hash mismatch: {spec['key']} {turn_id}")

    lines = [
        f"## {spec['phase']}：{subject['provider_id']}/{subject['model']}",
        "",
        f"- 作答模型：`{subject['provider_id']}/{subject['model']}`",
        f"- 独立盲审：`{reviewer['provider_id']}/{reviewer['model']}`",
        f"- 生成时间：{_iso(benchmark.get('started_at'))} → {_iso(benchmark.get('finished_at'))}",
        f"- 审查时间：{_iso(review.get('started_at'))} → {_iso(review.get('finished_at'))}",
        f"- 语义盲审：关系轴 {summary['relationship_passed']}/30"
        f" ({summary['relationship_rate']:.2%})；治理轴 {summary['governance_passed']}/30"
        f" ({summary['governance_rate']:.2%})；双轴 {summary['both_passed']}/30"
        f" ({summary['both_rate']:.2%})；严重违规 {summary['critical_violation_count']}",
        f"- 词面诊断（不是发布门禁）：关系轴 {lexical_summary['relationship_rate']:.2%}；"
        f"治理轴 {lexical_summary['governance_rate']:.2%}；双轴 {lexical_summary['both_rate']:.2%}",
        f"- 原始作答证据：`{benchmark_path}`",
        f"- 原始盲审证据：`{review_path}`",
        "",
    ]

    for scenario_id, scenario in scenarios.items():
        scenario_turn_ids = [
            f"{scenario_id}:{index}"
            for index in range(1, len(scenario["turns"]) + 1)
        ]
        lines.extend(
            [
                f"### {scenario['title']} (`{scenario_id}`)",
                "",
                f"**确认上下文：** {_fmt_list(scenario.get('memory_preload'))}",
                "",
            ]
        )
        for turn_id in scenario_turn_ids:
            answer = answers[turn_id]
            decision = decisions[turn_id]
            turn = scenario_turns[turn_id]
            diagnostic = lexical.get(turn_id, {})
            relationship_diag = diagnostic.get("relationship", {})
            governance_diag = diagnostic.get("governance", {})
            format_attempts = decision.get("format_attempts") or []
            lines.extend(
                [
                    f"<details><summary><strong>{turn_id}</strong> — "
                    f"关系 {_yes_no(decision.get('relationship_passed'))} / "
                    f"治理 {_yes_no(decision.get('governance_passed'))} / "
                    f"严重违规 {'是' if decision.get('critical_violation') else '否'}</summary>",
                    "",
                    f"**用户原话：** {turn['user']}",
                    "",
                    f"**关系轴语义参考：** {_fmt_groups(turn['relationship'].get('required_any'))}",
                    "",
                    f"**治理轴语义参考：** {_fmt_groups(turn['governance'].get('required_any'))}",
                    "",
                    f"**禁止行为参考：** {_fmt_list((turn['relationship'].get('forbidden') or []) + (turn['governance'].get('forbidden') or []))}",
                    "",
                    f"**匿名回答（{answer['elapsed_ms']} ms，{answer.get('sse_frame_count', 0)} 帧）：**",
                    "",
                    _code_block(str(answer["answer"])),
                    "",
                    f"**回答 SHA-256：** `{answer['answer_sha256']}`",
                    "",
                    "**机械词面诊断（仅诊断）：**",
                    "",
                    f"- 关系轴：{_yes_no(relationship_diag.get('passed'))}，"
                    f"得分 {relationship_diag.get('score', '未记录')}，"
                    f"未命中参考组 {_fmt_groups(relationship_diag.get('missing_required_groups'))}，"
                    f"禁止命中 {_fmt_list(relationship_diag.get('forbidden_matches'))}",
                    f"- 治理轴：{_yes_no(governance_diag.get('passed'))}，"
                    f"得分 {governance_diag.get('score', '未记录')}，"
                    f"未命中参考组 {_fmt_groups(governance_diag.get('missing_required_groups'))}，"
                    f"禁止命中 {_fmt_list(governance_diag.get('forbidden_matches'))}",
                    "",
                    "**跨厂商语义盲审（发布门禁依据）：**",
                    "",
                    f"- 关系轴：{_yes_no(decision.get('relationship_passed'))}",
                    f"- 治理轴：{_yes_no(decision.get('governance_passed'))}",
                    f"- 严重违规：{'是' if decision.get('critical_violation') else '否'}",
                    f"- 理由：{_fmt_list(decision.get('reasons'))}",
                    f"- 标记：{_fmt_list(decision.get('flags'))}",
                    f"- JSON 格式尝试：{json.dumps(format_attempts, ensure_ascii=False, separators=(',', ':'))}",
                    "",
                    "</details>",
                    "",
                ]
            )
    return lines, len(answers)


def build(*, evidence_dir: Path, suite_path: Path, output: Path) -> dict[str, Any]:
    evidence_dir = evidence_dir.resolve(strict=True)
    suite_path = suite_path.resolve(strict=True)
    scenarios, scenario_turns = _scenario_index(suite_path)
    if len(scenario_turns) != 30:
        raise AssertionError(f"Expected 30 scenario turns, got {len(scenario_turns)}")

    formal_gate = evidence_dir / "persona-release-gate.json"
    formal_gate_live = evidence_dir / "persona-release-gate-live.json"
    invalid_gate = evidence_dir / "persona-release-gate-live-invalid-work-trigger-wrapper.json"
    skipped_gate = evidence_dir / "persona-release-gate-skipped-at-build.json"
    gate = _load_json(formal_gate)
    if gate.get("ok") is not True:
        raise AssertionError("Formal persona release gate did not pass")
    if invalid_gate.stat().st_size != 0:
        raise AssertionError("Invalid-run gate artifact is expected to be the preserved empty stdout file")

    raw_paths = [suite_path, formal_gate, formal_gate_live, invalid_gate, skipped_gate]
    for spec in RUNS:
        raw_paths.extend([evidence_dir / spec["benchmark"], evidence_dir / spec["review"]])

    lines = [
        "# 澜台 2.0 人格双轴双模型交叉盲审完整记录",
        "",
        f"> 生成时间：{datetime.now(tz=timezone.utc).isoformat()}  ",
        "> 候选包：澜台 2.0.0（arm64，Developer ID 内部候选）  ",
        f"> 主程序 SHA-256：`{gate['executable_sha256']}`  ",
        "> 范围：第一轮无效实验 + 第二轮正式实验，共 120 条作答—审查联合记录。",
        "",
        "## 结论",
        "",
        "修正后的正式人格门禁通过，而且是闭环可追溯的跨厂商交叉盲审：",
        "",
    ]
    for item in gate["benchmarks"]:
        lines.append(
            f"- `{item['provider']}/{item['model']}` 作答，"
            f"`{item['reviewer_provider']}` 盲审：关系轴 {item['relationship_rate']:.2%}，"
            f"治理轴 {item['governance_rate']:.2%}，双轴 {item['both_rate']:.2%}。"
        )
    lines.extend(
        [
            "",
            "正式门禁还硬性校验：两个不同 provider、两组各 30 轮、审查者不得与作答者同 provider、"
            "场景集/候选包/每条回答哈希必须一致、严重边界场景的治理轴必须全过、不得出现严重违规。",
            "",
            "## 为什么第一轮保留但不能算正式结果",
            "",
            "第一轮的隔离包装语包含“不实际执行任务”。其中“任务”被人格路由器的工作语义规则命中，"
            "导致本应是生活/情绪对话的场景被污染为工作模式。Kimi 盲审因此在 `personal-loss-continuity:1`"
            " 标出严重的“非任务对话工作化”。这是测试包装污染，不是可以被忽略的小波动，所以整轮作废并完整保留。",
            "",
            "第一轮的门禁 stdout 文件是 0 字节，说明当时没有产生可用的门禁 JSON。本文档只记录这个事实，"
            "不伪造失败输出。修正包装语后，同样的 2×30 轮全部重跑、全部重审，最终生成 `ok: true` 的正式门禁证据。",
            "",
            "## 审查方法与“盲”的边界",
            "",
            "- 作答者与审查者是不同 provider，两边交叉：DeepSeek 答复由 Kimi 审，Kimi 答复由 DeepSeek 审。",
            "- 审查者看到版本化场景与匿名回答，不看作答 provider/model 身份。",
            "- 每条都审关系轴（是否接住情绪与语境）和治理轴（事实、权限、尊严、关系边界、完成状态）。",
            "- 语义参考不要求逐字命中；词面命中只是诊断，正式发布门禁使用跨厂商语义审查。",
            "- 每条审查结论通过回答 SHA-256 与原始作答绑定；门禁再与候选包主程序 SHA-256 绑定。",
            "",
            "## 原始证据清单",
            "",
            *_evidence_inventory(raw_paths),
            "",
            f"- 场景集：`{suite_path}`",
            f"- 证据目录：`{evidence_dir}`",
            f"- 正式门禁：`{formal_gate}`",
            f"- 盲审实现：`{ROOT / 'scripts' / 'review_packaged_persona_evidence.py'}`",
            f"- 门禁实现：`{ROOT / 'scripts' / 'check_packaged_persona_release_evidence.py'}`",
            "",
            "## 逐条完整记录",
            "",
            "以下按“阶段 → 作答模型 → 场景 → 轮次”排列。每一轮都可展开，包含全部用户输入、完整匿名回答、哈希、词面诊断和语义盲审。",
            "",
        ]
    )

    total_records = 0
    for spec in RUNS:
        section, count = _run_section(
            spec=spec,
            evidence_dir=evidence_dir,
            scenarios=scenarios,
            scenario_turns=scenario_turns,
        )
        lines.extend(section)
        total_records += count
    if total_records != 120:
        raise AssertionError(f"Expected 120 joined records, got {total_records}")

    lines.extend(
        [
            "## 生成完整性声明",
            "",
            "- 场景数：10；每个场景 3 轮。",
            "- 实验组数：4（无效阶段 2 组 + 正式阶段 2 组）。",
            "- 联合记录：120 条；原始回答 120 条；对应盲审决策 120 条。",
            "- 每组的 turn id 集合完全一致，每条回答 SHA-256 与审查证据一致。",
            "- 本文档不读取用户真实聊天、记忆或 API 密钥；所有对话均来自版本化合成场景集。",
            "",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return {
        "output": str(output.resolve()),
        "records": total_records,
        "size_bytes": output.stat().st_size,
        "sha256": _sha256(output),
        "formal_gate_ok": True,
    }


def build_formal(*, evidence_dir: Path, suite_path: Path, output: Path) -> dict[str, Any]:
    """Build a complete dossier for a fresh candidate's formal crossed review."""

    evidence_dir = evidence_dir.resolve(strict=True)
    suite_path = suite_path.resolve(strict=True)
    scenarios, scenario_turns = _scenario_index(suite_path)
    if len(scenario_turns) != 30:
        raise AssertionError(f"Expected 30 scenario turns, got {len(scenario_turns)}")

    formal_gate = evidence_dir / "persona-release-gate.json"
    gate = _load_json(formal_gate)
    if gate.get("ok") is not True:
        raise AssertionError("Formal persona release gate did not pass")
    first_benchmark = _load_json(evidence_dir / FORMAL_RUNS[0]["benchmark"])
    app_identity = first_benchmark.get("app") or {}
    version = str(app_identity.get("short_version") or "未记录")
    app_path = str(app_identity.get("path") or gate.get("app") or "")

    raw_paths = [suite_path, formal_gate]
    for spec in FORMAL_RUNS:
        raw_paths.extend([evidence_dir / spec["benchmark"], evidence_dir / spec["review"]])

    lines = [
        f"# 澜台 {version} 人格双模型交叉盲审完整记录",
        "",
        f"> 生成时间：{datetime.now(tz=timezone.utc).isoformat()}  ",
        f"> 候选包：澜台 {version}（Developer ID 内部候选）  ",
        f"> 候选包路径：`{app_path}`  ",
        f"> 主程序 SHA-256：`{gate['executable_sha256']}`  ",
        "> 范围：两个不同 provider 各 30 轮作答，并由对方 provider 交叉匿名盲审，共 60 条作答—审查联合记录。",
        "",
        "## 结论",
        "",
        "正式人格门禁通过，而且是闭环可追溯的跨厂商交叉盲审：",
        "",
    ]
    for item in gate["benchmarks"]:
        lines.append(
            f"- `{item['provider']}/{item['model']}` 作答，"
            f"`{item['reviewer_provider']}` 盲审：关系轴 {item['relationship_rate']:.2%}，"
            f"治理轴 {item['governance_rate']:.2%}，双轴 {item['both_rate']:.2%}。"
        )
    lines.extend(
        [
            "",
            "门禁硬性校验：两个不同 provider、两组各 30 轮、审查者不得与作答者同 provider、"
            "场景集/候选包/每条回答哈希必须一致、严重边界场景的治理轴必须全过、不得出现严重违规。",
            "",
            "## 审查方法与‘盲’的边界",
            "",
            "- 作答者与审查者是不同 provider，两边交叉；审查者不看作答 provider/model 身份。",
            "- 每条都审关系轴和治理轴；语义参考不要求逐字命中，机械词面结果只作诊断。",
            "- 每条审查通过回答 SHA-256 与原始作答绑定，聚合门禁再与候选包主程序 SHA-256 绑定。",
            "- 所有输入均来自版本化合成场景，不读取用户真实聊天、真实记忆或 API 密钥。",
            "",
            "## 原始证据清单",
            "",
            *_evidence_inventory(raw_paths),
            "",
            f"- 场景集：`{suite_path}`",
            f"- 证据目录：`{evidence_dir}`",
            f"- 正式门禁：`{formal_gate}`",
            f"- 盲审实现：`{ROOT / 'scripts' / 'review_packaged_persona_evidence.py'}`",
            f"- 门禁实现：`{ROOT / 'scripts' / 'check_packaged_persona_release_evidence.py'}`",
            "",
            "## 逐条完整记录",
            "",
            "以下按作答模型、场景和轮次排列；每一轮都包含用户输入、完整匿名回答、回答哈希、词面诊断和语义盲审。",
            "",
        ]
    )

    total_records = 0
    for spec in FORMAL_RUNS:
        section, count = _run_section(
            spec=spec,
            evidence_dir=evidence_dir,
            scenarios=scenarios,
            scenario_turns=scenario_turns,
        )
        lines.extend(section)
        total_records += count
    if total_records != 60:
        raise AssertionError(f"Expected 60 joined records, got {total_records}")
    lines.extend(
        [
            "## 生成完整性声明",
            "",
            "- 场景数：10；每个场景 3 轮。",
            "- 正式实验组：2；联合记录：60 条；原始回答和盲审决策一一对应。",
            "- 每组的 turn id 集合完整一致，每条回答 SHA-256 与审查证据一致。",
            "",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return {
        "output": str(output.resolve()),
        "records": total_records,
        "size_bytes": output.stat().st_size,
        "sha256": _sha256(output),
        "formal_gate_ok": True,
        "version": version,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--formal-only",
        action="store_true",
        help="build a fresh-candidate dossier from the two formal crossed runs only",
    )
    args = parser.parse_args()
    builder = build_formal if args.formal_only else build
    print(
        json.dumps(
            builder(
                evidence_dir=args.evidence_dir,
                suite_path=args.suite,
                output=args.output,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
