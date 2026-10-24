#!/usr/bin/env python3
"""Validate the immutable Runtime 2.0 phase-zero contract and package gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
INVARIANTS_PATH = ROOT / "contracts/runtime-v2/invariants.v1.json"
AUTHORITY_PATH = ROOT / "contracts/runtime-v2/authority-map.v1.json"
INCIDENTS_PATH = ROOT / "tests/fixtures/runtime/incidents/runtime_v2_user_reported_v1.json"
FOUNDATION_INCIDENTS_PATH = ROOT / "contracts/foundation-experience/incidents.json"
DEFAULT_PACKAGE_EVIDENCE_PATH = ROOT / "evidence/runtime-v2/phase-00-package-gate.json"

EXPECTED_AUTHORITY_BY_DOMAIN = {
    "conversation": "ConversationStore",
    "run": "RunJournal",
    "effect": "EffectLedger",
    "artifact": "ArtifactStore",
    "memory": "MemoryLedger",
    "collaboration": "CollaborationJournal",
}
EXPECTED_AUTHORITY_SOURCES = {
    "conversation": [
        "src/lantai/app/runner/session.py",
        "src/lantai/app/routers/messages.py",
        "console/src/vendor/lantaiChat.ts",
    ],
    "run": [
        "src/lantai/runtime/store.py",
        "src/lantai/runtime/events.py",
        "src/lantai/runtime/turn_control.py",
    ],
    "effect": [
        "src/lantai/runtime/effects.py",
        "src/lantai/runtime/tool_transaction.py",
        "src/lantai/security/tool_policy.py",
    ],
    "artifact": [
        "src/lantai/runtime/artifact_effects.py",
        "src/lantai/app/capabilities/artifacts.py",
        "src/lantai/runtime/file_delivery.py",
    ],
    "memory": [
        "src/lantai/agents/memory/canonical_ledger.py",
        "src/lantai/agents/memory/layered_memory.py",
    ],
    "collaboration": [
        "src/lantai/runtime/collaboration.py",
        "src/lantai/runtime/hosted_collaboration/coordinator.py",
        "src/lantai/agents/tools/agent_management.py",
    ],
}
EXPECTED_INVARIANT_STATEMENTS = {
    "RT2-INV-001": "每个事实域在任一迁移阶段只有一个可写权威。",
    "RT2-INV-002": "持久事件必须携带完整的 conversation、turn、run 和 attempt 身份。",
    "RT2-INV-003": "消息、历史和最终回答只有提交成功后才能对用户可见。",
    "RT2-INV-004": "事件流只负责增量传输，恢复必须使用快照、游标和可重放日志。",
    "RT2-INV-005": "界面只能投影运行状态，不能自行裁决运行事实。",
    "RT2-INV-006": "终态单调，终态 attempt 不得重新进入运行态。",
    "RT2-INV-007": "外部副作用必须先记录意图，再执行副作用。",
    "RT2-INV-008": "结果不明的副作用不得自动重放。",
    "RT2-INV-009": "路径权限基于规范化目标、工作区授权和操作类型，不能依赖模糊命令文本。",
    "RT2-INV-010": "页面或会话导航不得转移异步任务的所有权。",
    "RT2-INV-011": "内部协作者和研究 worker 使用中性上下文，最终回答由主 Agent 应用用户可见人格。",
    "RT2-INV-012": "网络、模型、组队、加载和恢复步骤必须有有界截止时间及可理解的降级状态。",
    "RT2-INV-013": "低频能力按需展开，不常驻占用主对话输入区。",
    "RT2-INV-014": "迁移必须支持回滚，并能以只读方式读取旧数据。",
    "RT2-INV-015": "Runtime 迁移不得削弱 Foundation Experience 受保护契约。",
    "RT2-INV-016": "主回答提交后，蛐蛐等附加生成不得阻塞 turn 终态；超时或失败必须记录为可降级结果。",
}
EXPECTED_INCIDENT_TO_PROBE = {
    "rt2-external-assistant-persistent-composer-v1": (
        "external_assistant_persistent_composer",
        "PKG-RT2-00-01",
    ),
    "rt2-navigation-return-state-corruption-v1": (
        "navigation_return_state_corruption",
        "PKG-RT2-00-02",
    ),
    "rt2-workspace-path-false-sensitive-block-v1": (
        "workspace_path_false_sensitive_block",
        "PKG-RT2-00-03",
    ),
    "rt2-domain-entry-task-center-misdirection-v1": (
        "domain_entry_task_center_misdirection",
        "PKG-RT2-00-04",
    ),
    "rt2-task-center-unbounded-loading-v1": (
        "task_center_unbounded_loading",
        "PKG-RT2-00-05",
    ),
    "rt2-collaboration-staffing-delay-v1": (
        "collaboration_staffing_delay",
        "PKG-RT2-00-06",
    ),
    "rt2-worker-persona-boundary-v1": (
        "worker_persona_boundary_violation",
        "PKG-RT2-00-07",
    ),
    "rt2-ququ-post-answer-blocks-terminal-v1": (
        "ququ_post_answer_blocks_terminal",
        "PKG-RT2-00-08",
    ),
}
EXPECTED_INCIDENT_EVIDENCE = {
    "rt2-external-assistant-persistent-composer-v1": [
        "tests/unit/app/test_external_assistant_retirement.py",
        "console/src/pages/Settings/Home/index.test.tsx",
        "console/src/navigation/detailNavigation.test.ts",
    ],
    "rt2-navigation-return-state-corruption-v1": [
        "console/src/vendor/lantaiChat.ts",
        "console/src/pages/Chat/runtimeEventStream.ts",
        "console/src/pages/Chat/ChatPage.test.tsx",
        "tests/unit/app/test_console_chat_smoke.py",
    ],
    "rt2-workspace-path-false-sensitive-block-v1": [
        "src/lantai/security/tool_policy.py",
        "src/lantai/security/shell_effects.py",
        "tests/unit/security/test_tool_policy.py",
    ],
    "rt2-domain-entry-task-center-misdirection-v1": [
        "src/lantai/runtime/capability_catalog.py",
        "console/src/layouts/Sidebar.tsx",
        "console/src/pages/Works/index.tsx",
        "console/src/pages/Chat/continuationSuggestions.ts",
    ],
    "rt2-task-center-unbounded-loading-v1": [
        "console/src/pages/Control/TaskCenterV2/index.tsx",
        "console/src/pages/Control/TaskCenterV2/index.test.tsx",
        "src/lantai/app/runtime_tasks/service.py",
    ],
    "rt2-collaboration-staffing-delay-v1": [
        "src/lantai/agents/tools/agent_management.py",
        "src/lantai/runtime/hosted_collaboration/coordinator.py",
        "console/src/pages/Chat/components/CollaborationPhaseRail.tsx",
        "tests/unit/agents/tools/test_agent_management.py",
    ],
    "rt2-worker-persona-boundary-v1": [
        "src/lantai/runtime/adapters/native_runtime.py",
        "src/lantai/agents/legacy_lantai_agent.py",
        "tests/unit/runtime/test_native_runtime.py",
        "tests/unit/app/test_runner_behavior.py",
        "tests/unit/agents/test_lantai_agent_contracts.py",
    ],
    "rt2-ququ-post-answer-blocks-terminal-v1": [
        "src/lantai/runtime/session_turn_lifecycle.py",
        "src/lantai/app/runner/runner.py",
        "src/lantai/app/post_answer_reflection_service.py",
        "console/src/vendor/lantaiChat.ts",
        "tests/unit/app/test_runner_behavior.py",
        "tests/unit/app/test_post_answer_reflection_service.py",
    ],
}
EXPECTED_PROBE_TEXT = {
    "PKG-RT2-00-01": (
        "外部助手已退休",
        "冷启动新会话，检查设置、导航、聊天输入区和旧深链；再发送包含“请 Codex 帮忙”的任务。",
        "无入口、无连接页、无模型可见工具、无 ACP 服务或外部 runner 启动；主任务继续使用现有合法能力。",
    ),
    "PKG-RT2-00-02": (
        "跨页面与跨会话返回稳定性",
        "在运行中与完成后分别从会话 A 切到设置、侧边栏能力、会话 B，再返回 A；重复三轮并重启一次。",
        "无串话、重复回复、假运行、内容丢失或错误 owner；A 与 B 各自恢复一致。",
    ),
    "PKG-RT2-00-03": (
        "路径权限真值表",
        "分别验证普通工作区路径、含空格路径、符号链接后的工作区路径、工作区外普通路径和真实系统根路径。",
        "工作区授权写入不误报系统路径；越界按权限提示；真实系统根继续硬拒绝。",
    ),
    "PKG-RT2-00-04": (
        "应用领域入口去向",
        "从主页面选择一个应用领域，并验证首次使用和存在上次会话两种情况。",
        "进入带清晰下一步的预填聊天或原会话；不会无解释跳到任务中心。",
    ),
    "PKG-RT2-00-05": (
        "任务中心加载边界",
        "在正常、慢响应和断开 Runtime 三种条件下打开任务中心并刷新。",
        "正常返回真实状态；慢响应与断开时在截止时间内离开骨架屏，显示可重试状态。",
    ),
    "PKG-RT2-00-06": (
        "协作组队延迟",
        "用固定小型文档请求两名协作者，记录从提交到第一份 staffing 事实和第一名成员开始工作的时间。",
        "组队有明确进展、截止时间与失败降级；不会无限停留在组队转圈。",
    ),
    "PKG-RT2-00-07": (
        "内部中性与最终人格边界",
        "执行一次协作和一次深度研究，检查内部请求清单与最终用户回复。",
        "内部 worker 未注入交付人格；最终回复由主 Agent 应用所选人格且只交付一次。",
    ),
    "PKG-RT2-00-08": (
        "蛐蛐附加生成不阻塞主终态",
        "主回答提交完成后，分别注入蛐蛐慢响应、超时和失败；测量主终态与输入框恢复延迟，并继续观察附加内容 30 秒。",
        "主回答提交后 1000 毫秒内主轮次进入终态且输入框可用；蛐蛐慢响应、超时或失败不延长前台轮次，30 秒观察窗内迟到内容最多附着一次。",
    ),
}
REQUIRED_ENVELOPE_FIELDS = {
    "schema_version",
    "event_id",
    "recorded_seq",
    "recorded_at",
    "conversation_id",
    "turn_id",
    "run_id",
    "attempt_id",
    "causation_id",
    "correlation_id",
    "producer",
    "payload_type",
    "payload_version",
    "payload",
}
EXPECTED_COLLABORATION_FIELDS = ["collaboration_id", "member_id"]
EXPECTED_COLLABORATION_EXECUTION_FIELDS = ["run_id", "attempt_id"]
EXPECTED_PLAN_IDENTITY_SENTENCE = (
    "协作事件再包含 `collaboration_id` 和 `member_id`；成员执行沿用信封中的 "
    "`run_id` 和 `attempt_id`，不再另造 `worker_id`、`worker_attempt_id` "
    "或 `member_run_id`"
)
ALLOWED_SOURCE_STATUSES = {
    "source_guarded_package_pending",
    "source_repro_package_pending",
    "source_unresolved_package_pending",
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ContractError(RuntimeError):
    """Raised when the Runtime 2.0 phase-zero gate must fail closed."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"required file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"JSON root must be an object: {path}")
    return payload


def _unique_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{label} must be a non-empty list")
    strings = [str(item or "").strip() for item in value]
    if any(not item for item in strings) or len(strings) != len(set(strings)):
        raise ContractError(f"{label} must contain unique non-empty strings")
    return strings


def _safe_source_path(root: Path, raw_path: Any, label: str) -> None:
    value = str(raw_path or "").strip()
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{label} must be a safe repository-relative path")
    if not (root / path).is_file():
        raise ContractError(f"{label} does not exist: {value}")


def _nonempty_text(value: Any, label: str, errors: list[str]) -> str:
    text = str(value or "").strip()
    if not text:
        errors.append(f"{label} must be non-empty")
    return text


def validate_contracts(
    invariants: dict[str, Any],
    authority_map: dict[str, Any],
    fixture: dict[str, Any],
    *,
    root: Path = ROOT,
    foundation_incident_ids: set[str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    for label, payload in (
        ("invariants", invariants),
        ("authority map", authority_map),
        ("incident fixture", fixture),
    ):
        if payload.get("schema_version") != 1:
            errors.append(f"{label} schema_version must be 1")
        if payload.get("phase") != "RT2-00":
            errors.append(f"{label} phase must be RT2-00")

    if invariants.get("migration_mode") != "staged_single_authority":
        errors.append("migration_mode must be staged_single_authority")
    if invariants.get("package_gate_required") is not True:
        errors.append("package_gate_required must remain true")
    try:
        _safe_source_path(root, invariants.get("source_plan"), "source plan")
        envelope_fields = set(
            _unique_strings(
                (invariants.get("identity_envelope") or {}).get("required_fields"),
                "identity_envelope.required_fields",
            )
        )
        missing_fields = sorted(REQUIRED_ENVELOPE_FIELDS - envelope_fields)
        if missing_fields:
            errors.append("identity envelope is missing: " + ", ".join(missing_fields))
    except ContractError as exc:
        errors.append(str(exc))

    collaboration_fields = (
        (invariants.get("identity_envelope") or {}).get("collaboration_identity_fields")
    )
    if collaboration_fields != EXPECTED_COLLABORATION_FIELDS:
        errors.append("collaboration identity fields must be collaboration_id and member_id")
    authority_identity = authority_map.get("collaboration_identity") or {}
    if authority_identity.get("specific_fields") != EXPECTED_COLLABORATION_FIELDS:
        errors.append("authority map collaboration identity fields changed")
    if (
        authority_identity.get("execution_fields_from_envelope")
        != EXPECTED_COLLABORATION_EXECUTION_FIELDS
    ):
        errors.append("collaboration execution identity must reuse run_id and attempt_id")

    invariant_items = invariants.get("invariants")
    if not isinstance(invariant_items, list):
        errors.append("invariants must be a list")
        invariant_items = []
    actual_invariants: dict[str, str] = {}
    for index, item in enumerate(invariant_items):
        if not isinstance(item, dict):
            errors.append(f"invariant {index} must be an object")
            continue
        invariant_id = _nonempty_text(item.get("id"), f"invariant {index} id", errors)
        statement = _nonempty_text(item.get("statement"), f"{invariant_id} statement", errors)
        if invariant_id in actual_invariants:
            errors.append(f"duplicate invariant id: {invariant_id}")
        actual_invariants[invariant_id] = statement
    if actual_invariants != EXPECTED_INVARIANT_STATEMENTS:
        errors.append("invariant ids and statements must match the frozen RT2-00 contract")
    if "optional_post_answer_blocks_terminal" not in set(invariants.get("forbidden_patterns") or []):
        errors.append("post-answer terminal blocking must remain forbidden")

    baseline = authority_map.get("baseline")
    if not isinstance(baseline, dict):
        errors.append("authority baseline must be an object")
        baseline = {}
    for key in ("lantai_commit", "deepseek_harness_commit", "codex_harness_commit"):
        if not SHA_PATTERN.fullmatch(str(baseline.get(key) or "")):
            errors.append(f"baseline {key} must be a full commit SHA")

    domains = authority_map.get("domains")
    if not isinstance(domains, list):
        errors.append("authority domains must be a list")
        domains = []
    actual_authority_by_domain: dict[str, str] = {}
    for index, domain in enumerate(domains):
        if not isinstance(domain, dict):
            errors.append(f"authority domain {index} must be an object")
            continue
        domain_id = _nonempty_text(domain.get("domain_id"), f"authority domain {index} id", errors)
        authority = _nonempty_text(
            domain.get("target_authority"),
            f"authority domain {domain_id} target",
            errors,
        )
        if domain_id in actual_authority_by_domain:
            errors.append(f"duplicate authority domain id: {domain_id}")
        actual_authority_by_domain[domain_id] = authority
        sources = domain.get("current_authoritative_sources")
        if sources != EXPECTED_AUTHORITY_SOURCES.get(domain_id):
            errors.append(f"authority source evidence changed for {domain_id}")
        try:
            for source in _unique_strings(sources, f"authority domain {domain_id} sources"):
                _safe_source_path(root, source, f"authority source for {domain_id}")
        except ContractError as exc:
            errors.append(str(exc))
    if actual_authority_by_domain != EXPECTED_AUTHORITY_BY_DOMAIN:
        errors.append("domain to authority mapping must match the frozen RT2-00 map")

    runtime_flow = authority_map.get("runtime_flow_sources")
    if not isinstance(runtime_flow, dict):
        errors.append("runtime_flow_sources must be an object")
    else:
        for flow, sources in runtime_flow.items():
            try:
                for source in _unique_strings(sources, f"runtime flow {flow}"):
                    _safe_source_path(root, source, f"runtime flow source {flow}")
            except ContractError as exc:
                errors.append(str(exc))

    try:
        required_classes = set(
            _unique_strings(fixture.get("required_incident_classes"), "required incident classes")
        )
        expected_classes = {pair[0] for pair in EXPECTED_INCIDENT_TO_PROBE.values()}
        if required_classes != expected_classes:
            errors.append("required incident classes must contain the eight user-reported classes")
    except ContractError as exc:
        errors.append(str(exc))
        required_classes = set()
    if set(fixture.get("allowed_source_statuses") or []) != ALLOWED_SOURCE_STATUSES:
        errors.append("allowed source statuses changed")

    incidents = fixture.get("incidents")
    probes = fixture.get("package_probes")
    if not isinstance(incidents, list):
        errors.append("incidents must be a list")
        incidents = []
    if not isinstance(probes, list):
        errors.append("package_probes must be a list")
        probes = []

    actual_incident_to_probe: dict[str, tuple[str, str]] = {}
    for index, incident in enumerate(incidents):
        if not isinstance(incident, dict):
            errors.append(f"incident {index} must be an object")
            continue
        incident_id = _nonempty_text(incident.get("incident_id"), f"incident {index} id", errors)
        incident_class = _nonempty_text(
            incident.get("incident_class"), f"{incident_id} class", errors
        )
        probe_id = _nonempty_text(
            incident.get("package_probe_id"), f"{incident_id} package probe", errors
        )
        actual_incident_to_probe[incident_id] = (incident_class, probe_id)
        _nonempty_text(incident.get("user_symptom"), f"{incident_id} user symptom", errors)
        _nonempty_text(incident.get("desired_behavior"), f"{incident_id} desired behavior", errors)
        if incident.get("source_status") not in ALLOWED_SOURCE_STATUSES:
            errors.append(f"{incident_id} has an invalid source status")
        unknown_domains = set(incident.get("fact_domains") or []) - set(
            EXPECTED_AUTHORITY_BY_DOMAIN
        )
        if unknown_domains:
            errors.append(f"{incident_id} references unknown fact domains")
        sources = incident.get("source_evidence")
        if sources != EXPECTED_INCIDENT_EVIDENCE.get(incident_id):
            errors.append(f"{incident_id} source evidence must match the frozen semantic evidence")
        try:
            for source in _unique_strings(sources, f"{incident_id} source evidence"):
                _safe_source_path(root, source, f"{incident_id} source evidence")
        except ContractError as exc:
            errors.append(str(exc))
        if foundation_incident_ids is not None:
            unknown_foundation = (
                set(incident.get("foundation_incident_ids") or []) - foundation_incident_ids
            )
            if unknown_foundation:
                errors.append(f"{incident_id} references unknown Foundation incidents")
    if actual_incident_to_probe != EXPECTED_INCIDENT_TO_PROBE:
        errors.append("incident classes and package probes must keep their frozen one-to-one pairing")

    actual_probe_ids: list[str] = []
    for index, probe in enumerate(probes):
        if not isinstance(probe, dict):
            errors.append(f"package probe {index} must be an object")
            continue
        probe_id = _nonempty_text(probe.get("probe_id"), f"package probe {index} id", errors)
        actual_probe_ids.append(probe_id)
        expected_text = EXPECTED_PROBE_TEXT.get(probe_id)
        actual_text = (
            _nonempty_text(probe.get("name"), f"{probe_id} name", errors),
            _nonempty_text(probe.get("procedure"), f"{probe_id} procedure", errors),
            _nonempty_text(probe.get("pass_condition"), f"{probe_id} pass condition", errors),
        )
        if actual_text != expected_text:
            errors.append(f"{probe_id} semantic text must match the frozen package probe")
        if probe.get("result") != "pending_candidate_build":
            errors.append(f"{probe_id} must remain pending_candidate_build in RT2-00 source")
        if probe_id == "PKG-RT2-00-08":
            if probe.get("foreground_terminal_deadline_ms") != 1000:
                errors.append("PKG-RT2-00-08 foreground terminal deadline must be 1000 ms")
            if probe.get("sidecar_observation_window_seconds") != 30:
                errors.append("PKG-RT2-00-08 sidecar observation window must be 30 seconds")
            if "observation_deadline_seconds" in probe:
                errors.append("PKG-RT2-00-08 must not conflate foreground and sidecar deadlines")
        else:
            deadline = probe.get("observation_deadline_seconds")
            if not isinstance(deadline, int) or deadline <= 0:
                errors.append(f"{probe_id} must have a positive observation deadline")
    if actual_probe_ids != list(EXPECTED_PROBE_TEXT):
        errors.append("package probes must be the ordered PKG-RT2-00-01..08 set")

    if errors:
        raise ContractError("; ".join(errors))
    return {
        "source_contract_ok": True,
        "phase_ok": False,
        "phase": "RT2-00",
        "package_gate": "pending_candidate_build",
        "authority_count": len(actual_authority_by_domain),
        "incident_count": len(actual_incident_to_probe),
        "invariant_count": len(actual_invariants),
        "package_probe_count": len(actual_probe_ids),
    }


def _validate_package_evidence(
    evidence: dict[str, Any],
    *,
    expected_probe_ids: list[str],
    expected_digests: dict[str, str],
) -> None:
    errors: list[str] = []
    if evidence.get("schema_version") != 1 or evidence.get("phase") != "RT2-00":
        errors.append("package evidence must use schema 1 for RT2-00")
    if evidence.get("result") != "pass":
        errors.append("package evidence result must be pass")
    provenance = evidence.get("build_provenance")
    if not isinstance(provenance, dict):
        errors.append("build_provenance must be an object")
        provenance = {}
    if not SHA_PATTERN.fullmatch(str(provenance.get("candidate_commit") or "")):
        errors.append("build provenance candidate_commit must be a full commit SHA")
    if not SHA256_PATTERN.fullmatch(str(provenance.get("artifact_sha256") or "")):
        errors.append("build provenance artifact_sha256 must be a full SHA-256")
    for field in ("app_version", "build_id", "built_at", "environment"):
        if not str(provenance.get(field) or "").strip():
            errors.append(f"build provenance {field} must be non-empty")
    if evidence.get("source_contract_digests") != expected_digests:
        errors.append("package evidence source contract digests do not match this candidate")

    probes = evidence.get("probes")
    if not isinstance(probes, list):
        errors.append("package evidence probes must be a list")
        probes = []
    actual_probe_ids: list[str] = []
    for index, probe in enumerate(probes):
        if not isinstance(probe, dict):
            errors.append(f"package evidence probe {index} must be an object")
            continue
        probe_id = str(probe.get("probe_id") or "").strip()
        actual_probe_ids.append(probe_id)
        if probe.get("result") != "pass":
            errors.append(f"{probe_id} package result must be pass")
        for field in ("started_at", "ended_at", "actual_behavior", "evidence"):
            if not str(probe.get(field) or "").strip():
                errors.append(f"{probe_id} {field} must be non-empty")
        if probe_id == "PKG-RT2-00-08":
            latency = probe.get("foreground_terminal_latency_ms")
            window = probe.get("sidecar_observation_seconds")
            scenarios = probe.get("scenarios")
            if not isinstance(latency, int) or latency < 0 or latency > 1000:
                errors.append("PKG-RT2-00-08 foreground latency must be within 1000 ms")
            if not isinstance(window, int) or window < 30:
                errors.append("PKG-RT2-00-08 sidecar observation must be at least 30 seconds")
            if scenarios != ["slow", "timeout", "failure"]:
                errors.append("PKG-RT2-00-08 must record slow, timeout and failure scenarios")
    if actual_probe_ids != expected_probe_ids:
        errors.append("package evidence must contain the ordered eight RT2-00 probes")
    if errors:
        raise ContractError("; ".join(errors))


def validate_phase0(
    *,
    root: Path = ROOT,
    source_only: bool = False,
    package_evidence_path: Path | None = None,
) -> dict[str, Any]:
    contract_paths = tuple(
        root / path.relative_to(ROOT)
        for path in (INVARIANTS_PATH, AUTHORITY_PATH, INCIDENTS_PATH)
    )
    invariants = _read_json(contract_paths[0])
    authority_map = _read_json(contract_paths[1])
    fixture = _read_json(contract_paths[2])
    foundation = _read_json(root / FOUNDATION_INCIDENTS_PATH.relative_to(ROOT))
    foundation_ids = {
        str(item.get("id") or "")
        for item in foundation.get("incidents") or []
        if isinstance(item, dict)
    }
    report = validate_contracts(
        invariants,
        authority_map,
        fixture,
        root=root,
        foundation_incident_ids=foundation_ids,
    )
    report["digests"] = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in contract_paths
    }
    plan_text = (root / invariants["source_plan"]).read_text(encoding="utf-8")
    if EXPECTED_PLAN_IDENTITY_SENTENCE not in plan_text:
        raise ContractError("source plan collaboration identity terminology is inconsistent")
    if source_only:
        return report

    evidence_path = package_evidence_path or (
        root / DEFAULT_PACKAGE_EVIDENCE_PATH.relative_to(ROOT)
    )
    if not evidence_path.is_file():
        report["package_evidence"] = str(evidence_path)
        return report
    evidence = _read_json(evidence_path)
    _validate_package_evidence(
        evidence,
        expected_probe_ids=list(EXPECTED_PROBE_TEXT),
        expected_digests=report["digests"],
    )
    report["phase_ok"] = True
    report["package_gate"] = "pass"
    report["package_evidence"] = str(evidence_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="validate only the frozen source contract; package gate remains pending",
    )
    parser.add_argument(
        "--package-evidence",
        type=Path,
        help="use an explicit package evidence JSON file for the full phase gate",
    )
    args = parser.parse_args(argv)
    try:
        report = validate_phase0(
            source_only=args.source_only,
            package_evidence_path=args.package_evidence,
        )
    except ContractError as exc:
        print(f"RT2-00 contract check failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    elif args.source_only:
        print(
            "RT2-00 source contract passed: "
            f"{report['invariant_count']} invariants, "
            f"{report['authority_count']} authorities, "
            f"{report['incident_count']} incidents, "
            f"{report['package_probe_count']} package probes; "
            "package gate pending."
        )
    elif report["phase_ok"]:
        print("RT2-00 phase gate passed with candidate package evidence.")
    else:
        print(
            "RT2-00 source contract passed, but the package gate is pending.",
            file=sys.stderr,
        )
    if args.source_only or report["phase_ok"]:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
