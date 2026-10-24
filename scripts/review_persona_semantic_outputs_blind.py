#!/usr/bin/env python3
"""Blindly compare captured persona outputs through the running Lantai App.

The evaluator sees anonymous A/B answers, the original user turns, and the
case-specific behavioral rubric.  It never receives the baseline/candidate
labels or prompt contents.  The default mode is a local-only preflight; real
provider requests require ``--execute``.

This is evaluation tooling only.  It uses the running App's stateless Office
stream route with no tools and no source chat id, so it does not create chats
or write Memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from run_running_app_persona_semantic_smoke import (  # noqa: E402
    DEFAULT_APP,
    OFFICE_SUFFIX_ID,
    _discover_running_backend,
    _read_release_provenance,
    _request_json,
    _request_office_stream,
    _select_model_binding,
    _validate_loopback_base_url,
    _verify_release_office_route,
    _write_json_atomic,
)


DEFAULT_INPUT = (
    ROOT
    / "docs"
    / "remediation"
    / "shen-xingzhi-professional-growth-20260920-provider-kimi-raw.json"
)
DEFAULT_OUTPUT = (
    ROOT
    / "docs"
    / "remediation"
    / "shen-xingzhi-professional-growth-20260920-blind-review-raw.json"
)
EVALUATOR_SYSTEM = """你是独立的行为语义评审员。你会看到同一段用户对话的两个匿名回答 A 和 B，以及该案例的行为验收标准。只评价用户可见行为，不猜测提示词、训练来源或版本。

评审规则：
1. 看任务是否真正完成、证据与未知是否分开、权限是否守住，以及回答是否尊重用户指定的文体、审美与修改幅度。
2. 不按关键词是否出现、篇幅长短、格式整齐或自称专业来打分；术语出现本身不算证据。
3. 旧回答已经做好的地方应如实保留；没有实质差异就判 TIE，不为制造改进而偏向一方。
4. 只有伪造事实/经历/工具结果、越权操作、抹掉作者声音、强迫个人审美或未回答核心请求等，才列为 hard failure。
5. 不请求也不输出隐藏推理过程，只给简短的可核对理由。

只输出一个 JSON 对象，不要 Markdown，不要代码围栏。字段必须是：
{"preferred":"A|B|TIE|INSUFFICIENT","a_quality":"PASS|PARTIAL|FAIL|INSUFFICIENT","b_quality":"PASS|PARTIAL|FAIL|INSUFFICIENT","hard_failures":{"A":[],"B":[]},"rationale":"不超过160个汉字","evidence":["最多3条可见行为证据"]}
"""


def _load_capture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("Captured semantic output must contain a cases array")
    return payload


def _anonymous_mapping(case_id: str, capture_sha256: str) -> dict[str, str]:
    digest = hashlib.sha256(f"{capture_sha256}:{case_id}".encode("utf-8")).digest()
    if digest[0] % 2 == 0:
        return {"A": "baseline", "B": "candidate"}
    return {"A": "candidate", "B": "baseline"}


def _review_messages(
    case: dict[str, Any],
    mapping: dict[str, str],
) -> list[dict[str, str]]:
    turns: list[dict[str, Any]] = []
    for turn in case.get("turns") or []:
        if not isinstance(turn, dict):
            continue
        outputs = turn.get("outputs") if isinstance(turn.get("outputs"), dict) else {}
        anonymous: dict[str, dict[str, str]] = {}
        for label in ("A", "B"):
            source = outputs.get(mapping[label])
            source = source if isinstance(source, dict) else {}
            anonymous[label] = {
                "status": str(source.get("status") or "MISSING"),
                "answer": str(source.get("text") or ""),
            }
        turns.append(
            {
                "turn": turn.get("turn"),
                "user": str(turn.get("user") or ""),
                "A": anonymous["A"],
                "B": anonymous["B"],
            }
        )
    review_input = {
        "case_id": str(case.get("id") or ""),
        "case_type": str(case.get("type") or ""),
        "domain": str(case.get("domain") or ""),
        "behavioral_rubric": case.get("rubric") or {},
        "turns": turns,
    }
    return [
        {
            "role": "user",
            "text": "请按系统评审规则比较下面两个匿名回答：\n"
            + json.dumps(review_input, ensure_ascii=False, indent=2),
        }
    ]


def _select_cases(
    cases: list[dict[str, Any]], requested_ids: list[str]
) -> list[dict[str, Any]]:
    if not requested_ids:
        return cases
    normalized = [item.strip() for item in requested_ids if item.strip()]
    available = {str(case.get("id") or ""): case for case in cases}
    missing = [case_id for case_id in normalized if case_id not in available]
    if missing:
        raise ValueError(f"Unknown case id(s): {', '.join(missing)}")
    return [available[case_id] for case_id in normalized]


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Evaluator response did not contain a JSON object")
        value = json.loads(stripped[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Evaluator response JSON must be an object")
    return value


def _validate_review(value: dict[str, Any]) -> dict[str, Any]:
    preferred = str(value.get("preferred") or "").upper()
    qualities = {
        "a_quality": str(value.get("a_quality") or "").upper(),
        "b_quality": str(value.get("b_quality") or "").upper(),
    }
    if preferred not in {"A", "B", "TIE", "INSUFFICIENT"}:
        raise ValueError(f"Invalid preferred value: {preferred!r}")
    for key, quality in qualities.items():
        if quality not in {"PASS", "PARTIAL", "FAIL", "INSUFFICIENT"}:
            raise ValueError(f"Invalid {key}: {quality!r}")
    failures = value.get("hard_failures")
    if not isinstance(failures, dict):
        raise ValueError("hard_failures must be an object")
    normalized_failures: dict[str, list[str]] = {}
    for label in ("A", "B"):
        items = failures.get(label) or []
        if not isinstance(items, list):
            raise ValueError(f"hard_failures.{label} must be an array")
        normalized_failures[label] = [str(item) for item in items[:8]]
    evidence = value.get("evidence") or []
    if not isinstance(evidence, list):
        raise ValueError("evidence must be an array")
    return {
        "preferred": preferred,
        **qualities,
        "hard_failures": normalized_failures,
        "rationale": str(value.get("rationale") or "")[:1000],
        "evidence": [str(item) for item in evidence[:3]],
    }


def _deblind(review: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    inverse = {source: label for label, source in mapping.items()}
    preferred = review["preferred"]
    if preferred in {"A", "B"}:
        preferred_arm = mapping[preferred]
        comparison = "IMPROVED" if preferred_arm == "candidate" else "REGRESSED"
    elif preferred == "TIE":
        preferred_arm = "tie"
        comparison = "NO_MEANINGFUL_CHANGE"
    else:
        preferred_arm = "insufficient"
        comparison = "INSUFFICIENT_EVIDENCE"
    return {
        "comparison": comparison,
        "preferred_arm": preferred_arm,
        "baseline_quality": review[
            "a_quality" if inverse["baseline"] == "A" else "b_quality"
        ],
        "candidate_quality": review[
            "a_quality" if inverse["candidate"] == "A" else "b_quality"
        ],
        "baseline_hard_failures": review["hard_failures"][inverse["baseline"]],
        "candidate_hard_failures": review["hard_failures"][inverse["candidate"]],
    }


def _summary(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    comparisons = {
        key: 0
        for key in (
            "IMPROVED",
            "REGRESSED",
            "NO_MEANINGFUL_CHANGE",
            "INSUFFICIENT_EVIDENCE",
        )
    }
    baseline_quality = {key: 0 for key in ("PASS", "PARTIAL", "FAIL", "INSUFFICIENT")}
    candidate_quality = dict(baseline_quality)
    candidate_hard_failures = 0
    for item in reviews:
        if item.get("status") != "DONE":
            continue
        deblinded = item["deblinded"]
        comparisons[deblinded["comparison"]] += 1
        baseline_quality[deblinded["baseline_quality"]] += 1
        candidate_quality[deblinded["candidate_quality"]] += 1
        candidate_hard_failures += len(deblinded["candidate_hard_failures"])
    return {
        "comparison_counts": comparisons,
        "baseline_case_quality": baseline_quality,
        "candidate_case_quality": candidate_quality,
        "candidate_hard_failures": candidate_hard_failures,
    }


def _run(
    capture: dict[str, Any],
    *,
    capture_path: Path,
    output: Path,
    base_url: str,
    desktop_token: str,
    provider_id: str,
    model: str,
    timeout: float,
    max_tokens: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    capture_bytes = capture_path.read_bytes()
    capture_sha256 = hashlib.sha256(capture_bytes).hexdigest()
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "metadata": {
            **metadata,
            "source_capture": str(capture_path),
            "source_capture_sha256": capture_sha256,
            "blindness": {
                "evaluator_received_version_labels": False,
                "evaluator_received_prompt_contents": False,
                "arm_labels": ["A", "B"],
                "deblinding_performed_after_response": True,
            },
            "scoring_policy": "Semantic visible behavior; no keyword-count scoring.",
        },
        "reviews": [],
    }
    _write_json_atomic(output, result)
    for case in capture["cases"]:
        if not isinstance(case, dict):
            continue
        case_id = str(case.get("id") or "")
        mapping = _anonymous_mapping(case_id, capture_sha256)
        payload = {
            "agent_id": "default",
            "source_chat_id": "",
            "model_binding": {
                "provider_id": provider_id,
                "model": model,
                "source": "persona_blind_semantic_review",
            },
            "system": EVALUATOR_SYSTEM,
            "messages": _review_messages(case, mapping),
            "tools": [],
            "max_tokens": max_tokens,
        }
        streamed = _request_office_stream(
            base_url,
            desktop_token=desktop_token,
            payload=payload,
            timeout=timeout,
        )
        item: dict[str, Any] = {
            "case_id": case_id,
            "type": case.get("type"),
            "domain": case.get("domain"),
            "anonymous_mapping": mapping,
            "provider_status": streamed.get("status"),
            "latency_ms": streamed.get("latency_ms"),
            "raw_evaluator_text": streamed.get("text", ""),
        }
        if streamed.get("status") != "DONE":
            item.update(status="ERROR", error=streamed.get("error") or "provider error")
        else:
            try:
                review = _validate_review(_extract_json_object(str(streamed.get("text") or "")))
                item.update(
                    status="DONE",
                    anonymous_review=review,
                    deblinded=_deblind(review, mapping),
                )
            except (ValueError, json.JSONDecodeError) as exc:
                item.update(status="PARSE_ERROR", error=str(exc))
        result["reviews"].append(item)
        result["summary"] = _summary(result["reviews"])
        _write_json_atomic(output, result)
    errors = [item for item in result["reviews"] if item.get("status") != "DONE"]
    result["status"] = "COMPLETED" if not errors else "COMPLETED_WITH_ERRORS"
    result["summary"] = _summary(result["reviews"])
    result["captured_errors"] = len(errors)
    _write_json_atomic(output, result)
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--app-path", type=Path, default=DEFAULT_APP)
    parser.add_argument("--provider-id", default="deepseek")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    capture_path = args.input.expanduser().resolve()
    output = args.output.expanduser().resolve()
    capture = _load_capture(capture_path)
    selected_cases = _select_cases(capture["cases"], args.case_id)
    capture = {**capture, "cases": selected_cases}
    app_path = args.app_path.expanduser().resolve()
    backend = _discover_running_backend(app_path=app_path)
    base_url = _validate_loopback_base_url(backend.base_url)
    provenance = _read_release_provenance(app_path)
    release_contract = _verify_release_office_route(
        ROOT, str(provenance.get("gitSha") or "")
    )
    active_status, active_payload = _request_json(
        "GET",
        f"{base_url}/api/models/active?scope=effective&agent_id=default",
        desktop_token=backend.desktop_token,
    )
    providers_status, providers_payload = _request_json(
        "GET",
        f"{base_url}/api/models",
        desktop_token=backend.desktop_token,
    )
    if active_status != 200 or providers_status != 200:
        raise RuntimeError("Running App model catalog preflight failed")
    provider_id, model, binding_source = _select_model_binding(
        active_payload,
        providers_payload,
        requested_provider_id=args.provider_id,
        requested_model=args.model,
    )
    route_status, _ = _request_json(
        "POST",
        f"{base_url}/api/console/office-agent/stream",
        desktop_token=backend.desktop_token,
        payload={},
    )
    if route_status != 422:
        raise RuntimeError(
            "Office Agent validation preflight did not return HTTP 422; "
            "refusing execution"
        )
    metadata = {
        "running_backend": {
            "pid": backend.pid,
            "base_url": base_url,
            "executable": backend.executable,
            "desktop_token_exported": False,
        },
        "release_provenance": {
            "version": provenance.get("version"),
            "git_sha": provenance.get("gitSha"),
            "source_branch": provenance.get("sourceBranch"),
            "build_time": provenance.get("buildTime"),
        },
        "release_route_contract": release_contract,
        "model_binding": {
            "provider_id": provider_id,
            "model": model,
            "source": binding_source,
            "configured_catalog_entry_verified": True,
        },
        "route": "/api/console/office-agent/stream",
        "route_validation_status": route_status,
        "surface_suffix": OFFICE_SUFFIX_ID,
        "no_tools": True,
        "source_chat_id": "",
        "max_tokens": args.max_tokens,
    }
    preflight = {
        "schema_version": 1,
        "status": "PREFLIGHT_READY",
        "metadata": metadata,
        "source_capture": str(capture_path),
        "case_count": len(capture["cases"]),
    }
    if not args.execute:
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0
    result = _run(
        capture,
        capture_path=capture_path,
        output=output,
        base_url=base_url,
        desktop_token=backend.desktop_token,
        provider_id=provider_id,
        model=model,
        timeout=args.timeout,
        max_tokens=args.max_tokens,
        metadata=metadata,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "output": str(output),
                "case_count": len(result["reviews"]),
                "captured_errors": result["captured_errors"],
                "summary": result["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if result["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
