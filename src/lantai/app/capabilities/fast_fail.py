# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

from . import policy
from .evaluations import build_report, make_finding
from .models import (
    Capability,
    CapabilityRunRequest,
    DeliveryTarget,
    EvaluationFinding,
    EvaluationReport,
    Pipeline,
    PipelineRunRequest,
)
from .repo import CapabilityRepository
from .template import validate_step_parameters


def _is_workspace_path(workspace_dir: Path, target: DeliveryTarget) -> bool:
    if target.type != "workspace_file":
        return True
    relative = target.path or "capabilities/outputs/output.md"
    path = (workspace_dir / relative).resolve()
    root = workspace_dir.resolve()
    return path == root or root in path.parents


def _delivery_findings(
    *,
    targets: list[DeliveryTarget],
    workspace_dir: Path,
    app_store: bool,
) -> list[EvaluationFinding]:
    findings: list[EvaluationFinding] = []
    for target in targets:
        if not _is_workspace_path(workspace_dir, target):
            findings.append(
                make_finding(
                    code="delivery_path_outside_workspace",
                    title="交付路径不在当前工作区内",
                    detail=target.path or "",
                    metadata={"target_type": target.type, "path": target.path},
                )
            )
        if app_store and target.type == "chat":
            findings.append(
                make_finding(
                    code="app_store_external_delivery_blocked",
                    title="App Store 版不允许能力自动外部发送",
                    detail="请改为工作区文件或 Wiki 草稿交付。",
                    metadata={"target_type": target.type},
                )
            )
    return findings


def _background_scheduled_findings(source: str) -> list[EvaluationFinding]:
    matrix = policy.build_capability_matrix()
    entry = matrix.entry("background_scheduled_execution")
    if matrix.distribution != "app_store":
        return []
    if source not in {"cron", "pipeline_cron"}:
        return []
    if entry.mode == "enabled":
        return []
    return [
        make_finding(
            code=entry.reason_code,
            title="App Store 版限制后台/定时长期任务",
            detail=entry.description or "请改为可见、可人工接管的执行方式。",
            metadata={
                "capability_matrix_mode": entry.mode,
                "capability_matrix_id": entry.id,
                "source": source,
            },
        )
    ]


def capability_gate_report(
    *,
    task_run_id: str,
    capability: Capability,
    request: CapabilityRunRequest,
    values: dict,
    workspace_dir: str | Path,
    source: str,
) -> EvaluationReport:
    findings: list[EvaluationFinding] = []
    app_store = policy.is_app_store_distribution()
    workspace_root = Path(workspace_dir).expanduser()

    if not capability.enabled and source != "cron":
        findings.append(
            make_finding(
                code="capability_disabled",
                title="能力尚未启用",
                detail="请先在编辑器中确认并启用能力。",
            )
        )

    if app_store and policy.capability_has_interactive_steps(capability):
        findings.append(
            make_finding(
                code="app_store_gui_blocked",
                title="App Store 版不支持 GUI/浏览器自动操作能力",
                detail="该能力包含 GUI 或浏览器自动操作步骤。",
            )
        )

    if source in {"cron", "pipeline_cron"} and policy.capability_has_interactive_steps(capability):
        findings.append(
            make_finding(
                code="cron_gui_blocked",
                title="定时任务不允许执行 GUI/浏览器自动操作能力",
                detail="GUI/浏览器自动操作步骤必须由人工手动触发。",
            )
        )

    findings.extend(_background_scheduled_findings(source))

    for step in sorted(capability.steps, key=lambda item: item.index):
        try:
            validate_step_parameters(step, values)
        except ValueError as exc:
            findings.append(
                make_finding(
                    code="step_missing_parameters",
                    title=f"步骤「{step.title}」参数不完整",
                    detail=str(exc),
                    step_id=step.id,
                    metadata={"step_title": step.title},
                )
            )

        if step.kind == "gui_prompt" and (
            not request.manual_gui_ack or "high" not in request.approved_risks
        ):
            findings.append(
                make_finding(
                    code="gui_ack_required",
                    title=f"步骤「{step.title}」需要手动确认",
                    detail="高风险 GUI/电脑操作需要 manual_gui_ack=true 且批准 high 风险。",
                    step_id=step.id,
                    metadata={"risk_level": step.risk_level},
                )
            )

        if step.kind == "browser_prompt":
            try:
                from ..browser_workbench import BrowserProfileService

                profile = BrowserProfileService().get_profile(
                    step.profile_id or "default",
                )
            except Exception:
                profile = None
            if profile is None:
                findings.append(
                    make_finding(
                        code="browser_profile_missing",
                        title=f"步骤「{step.title}」引用的浏览器 Profile 不存在",
                        detail=step.profile_id or "default",
                        step_id=step.id,
                    )
                )
            elif not profile.agent_control_enabled:
                findings.append(
                    make_finding(
                        code="browser_profile_agent_control_disabled",
                        title=f"步骤「{step.title}」需要浏览器 Profile 授权",
                        detail="请先在浏览器工作台中允许 Agent 控制该 Profile。",
                        step_id=step.id,
                        metadata={"profile_id": profile.id},
                    )
                )
            elif step.target_domains and profile.allowed_domains:
                disallowed = []
                for domain in step.target_domains:
                    normalized_domain = str(domain or "").strip().lower().strip(".")
                    if not normalized_domain:
                        continue
                    if not any(
                        normalized_domain == allowed
                        or normalized_domain.endswith(f".{allowed}")
                        for allowed in profile.allowed_domains
                    ):
                        disallowed.append(normalized_domain)
                if disallowed:
                    findings.append(
                        make_finding(
                            code="browser_domain_not_allowed",
                            title=f"步骤「{step.title}」包含未授权域名",
                            detail=", ".join(disallowed),
                            step_id=step.id,
                            metadata={"profile_id": profile.id},
                        )
                    )
            if (
                step.access in {"write", "sensitive"}
                or step.requires_manual_confirmation
            ) and (
                not request.manual_gui_ack or "high" not in request.approved_risks
            ):
                findings.append(
                    make_finding(
                        code="browser_ack_required",
                        title=f"步骤「{step.title}」需要浏览器操作确认",
                        detail="浏览器写操作需要 manual_gui_ack=true 且批准 high 风险。",
                        step_id=step.id,
                        metadata={"risk_level": step.risk_level},
                    )
                )

        if (
            step.risk_level in capability.risk_policy.require_approval_for
            and step.risk_level not in request.approved_risks
        ):
            findings.append(
                make_finding(
                    code="risk_approval_required",
                    title=f"步骤「{step.title}」风险确认缺失",
                    detail=f"风险等级 {step.risk_level} 需要先确认。",
                    step_id=step.id,
                    metadata={"risk_level": step.risk_level},
                )
            )

    if not request.suppress_delivery:
        targets = (
            request.delivery_targets
            if request.delivery_targets is not None
            else capability.delivery_defaults
        )
        findings.extend(
            _delivery_findings(
                targets=targets,
                workspace_dir=workspace_root,
                app_store=app_store,
            )
        )

    return build_report(
        task_run_id=task_run_id,
        evaluator="fast_fail_gate",
        findings=findings,
        passed_summary="Fast-fail gate passed.",
        blocked_summary="Fast-fail gate blocked this capability run before execution.",
    )


async def pipeline_gate_report(
    *,
    task_run_id: str,
    pipeline: Pipeline,
    request: PipelineRunRequest,
    workspace_dir: str | Path,
    source: str,
    repo: CapabilityRepository,
) -> EvaluationReport:
    findings: list[EvaluationFinding] = []
    app_store = policy.is_app_store_distribution()
    capabilities: dict[str, Capability] = {}

    if not pipeline.enabled and source != "cron":
        findings.append(
            make_finding(
                code="pipeline_disabled",
                title="Pipeline 尚未启用",
                detail="请先确认并启用 Pipeline。",
            )
        )

    for node in pipeline.nodes:
        capability = await repo.get_capability(node.capability_id)
        if capability is None:
            findings.append(
                make_finding(
                    code="pipeline_capability_missing",
                    title=f"Pipeline 节点「{node.title}」引用的能力不存在",
                    detail=node.capability_id,
                    metadata={"node_id": node.id, "capability_id": node.capability_id},
                )
            )
            continue
        capabilities[node.capability_id] = capability
        if app_store and policy.capability_has_interactive_steps(capability):
            findings.append(
                make_finding(
                    code="app_store_gui_blocked",
                    title=f"节点「{node.title}」包含 App Store 版不支持的 GUI/浏览器自动操作",
                    detail=capability.name,
                    metadata={"node_id": node.id, "capability_id": capability.id},
                )
            )
        if source == "cron" and policy.capability_has_interactive_steps(capability):
            findings.append(
                make_finding(
                    code="cron_gui_blocked",
                    title=f"定时 Pipeline 节点「{node.title}」不允许 GUI/浏览器自动操作",
                    detail=capability.name,
                    metadata={"node_id": node.id, "capability_id": capability.id},
                )
            )

    findings.extend(_background_scheduled_findings(source))

    targets = request.delivery_targets if request.delivery_targets is not None else pipeline.delivery_defaults
    findings.extend(
        _delivery_findings(
            targets=targets,
            workspace_dir=Path(workspace_dir).expanduser(),
            app_store=app_store,
        )
    )

    return build_report(
        task_run_id=task_run_id,
        evaluator="fast_fail_gate",
        findings=findings,
        passed_summary="Fast-fail gate passed.",
        blocked_summary="Fast-fail gate blocked this pipeline run before execution.",
    )
