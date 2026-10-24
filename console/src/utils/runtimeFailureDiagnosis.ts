import type { RuntimeFailureDiagnosis } from "../api/types/chat";
import type { PreviewHarnessReport } from "../api/types/runtime";

function textField(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value.trim() : "";
}

function booleanField(
  source: Record<string, unknown>,
  key: string,
): boolean | undefined {
  const value = source[key];
  return typeof value === "boolean" ? value : undefined;
}

function stringListField(
  source: Record<string, unknown>,
  key: string,
): string[] {
  const value = source[key];
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.trim())
    .filter(Boolean);
}

function presentationRecoveryDiagnosis(
  payload: Record<string, unknown>,
): RuntimeFailureDiagnosis | null {
  const rawMetadata = payload.metadata;
  if (!rawMetadata || typeof rawMetadata !== "object") return null;
  const metadata = rawMetadata as Record<string, unknown>;
  const errorCode = textField(metadata, "error_code");
  const jobId = textField(metadata, "job_id");
  if (!errorCode.startsWith("presentation_") || !jobId) return null;

  const publishReady = booleanField(metadata, "publish_ready");
  const requiresPublish = booleanField(metadata, "requires_publish");
  const requiresHuman = booleanField(metadata, "requires_human");
  const recoveryActions = stringListField(metadata, "recovery_actions");
  const common = {
    job_id: jobId,
    publish_ready: publishReady,
    requires_publish: requiresPublish,
    requires_human: requiresHuman,
    evidence: errorCode,
  };

  if (
    errorCode === "presentation_delivery_conflict" &&
    requiresHuman === true &&
    publishReady === false &&
    requiresPublish === false
  ) {
    return {
      ...common,
      label: "PPT 交付需确认",
      category: "blocked",
      summary: "目标 PPTX 已被修改或占用",
      next_action:
        "保留现有文件，不要覆盖；请用户选择新的输出路径或文件名后继续原任务。",
      recovery_action: "choose_output_destination",
    };
  }

  if (
    errorCode === "presentation_delivery_response_error" &&
    publishReady === true &&
    requiresPublish === true &&
    requiresHuman === false
  ) {
    return {
      ...common,
      label: "PPT 附件待重发",
      category: "presentation_delivery",
      summary: "PPT 已通过核验，附件响应尚未送达",
      next_action: "续接原任务并重新发送当前已验证的 PPTX，不要重新生成。",
      recovery_action: "resend_verified_result",
    };
  }

  if (
    recoveryActions.includes("retry_file_stage") &&
    publishReady === false &&
    requiresPublish === false &&
    requiresHuman === false
  ) {
    return {
      ...common,
      label: "PPT 文件交付待续接",
      category: "presentation_delivery",
      summary: "PPT 文件交付尚未完成",
      next_action:
        "续接原任务，只重试文件交付阶段；不要新建作品或重新生成已验真的页面。",
      recovery_action: "retry_file_stage",
    };
  }

  return null;
}

export function runtimeFailureDiagnosisFromPayload(
  payload?: Record<string, unknown> | null,
): RuntimeFailureDiagnosis | null {
  if (!payload || typeof payload !== "object") return null;
  const raw = payload.failure_diagnosis;
  if (raw && typeof raw === "object") {
    const record = raw as Record<string, unknown>;
    const summary = textField(record, "summary");
    const nextAction = textField(record, "next_action");
    if (!summary || !nextAction) return null;
    return {
      label: textField(record, "label"),
      category: textField(record, "category"),
      summary,
      next_action: nextAction,
      evidence: textField(record, "evidence") || undefined,
    };
  }
  return presentationRecoveryDiagnosis(payload);
}

export function runtimeFailureDiagnosisTone(
  diagnosis: RuntimeFailureDiagnosis,
): "warning" | "danger" {
  return diagnosis.category === "blocked" ? "danger" : "warning";
}

function previewHarnessLines(report?: PreviewHarnessReport | null): string[] {
  if (!report) return [];
  const lines = [
    "预览诊断：",
    `- URL: ${report.fetch_url || report.url}`,
    `- 结果: ${report.ok ? "通过" : "发现问题"} · ${report.summary}`,
  ];
  const firstFinding = report.findings?.[0];
  if (firstFinding) {
    lines.push(
      `- 首个发现: ${firstFinding.severity}/${firstFinding.category} ${firstFinding.message}`,
    );
    if (firstFinding.evidence) {
      lines.push(`- 证据: ${firstFinding.evidence}`);
    }
  }
  const firstConsoleError = report.console_errors?.[0];
  if (firstConsoleError) {
    lines.push(`- 控制台首错: ${firstConsoleError}`);
  }
  const firstPageError = report.page_errors?.[0];
  if (firstPageError) {
    lines.push(`- 页面异常: ${firstPageError}`);
  }
  const firstNetworkError = report.network_errors?.[0];
  if (firstNetworkError) {
    lines.push(
      `- 网络失败: ${firstNetworkError.url} ${firstNetworkError.status ?? ""} ${
        firstNetworkError.error || ""
      }`.trim(),
    );
  }
  if (report.white_screen_likely) {
    lines.push("- 渲染判断: 页面可能白屏");
  }
  if (report.screenshot_path) {
    lines.push(`- 截图: ${report.screenshot_path}`);
  }
  return lines;
}

export function previewHarnessReportFromPayload(
  payload?: Record<string, unknown> | null,
): PreviewHarnessReport | null {
  const raw = payload?.preview_harness;
  if (!raw || typeof raw !== "object") return null;
  return raw as PreviewHarnessReport;
}

export function buildRuntimeRepairPrompt(input: {
  diagnosis: RuntimeFailureDiagnosis;
  runId?: string | null;
  stageLabel?: string | null;
  eventDetail?: string | null;
  workspaceDir?: string | null;
  previewHarness?: PreviewHarnessReport | null;
}): string {
  const { diagnosis } = input;
  const isPreview = diagnosis.category === "preview";
  const isPresentationRecovery = Boolean(
    diagnosis.job_id && diagnosis.recovery_action,
  );
  const harnessLines = previewHarnessLines(input.previewHarness);
  const presentationInstruction =
    diagnosis.recovery_action === "retry_file_stage"
      ? "请继续原 PPT 任务，只重试文件交付阶段；不要新建作品、不要重新生成已验真的页面，也不要覆盖用户已编辑的文件。"
      : diagnosis.recovery_action === "resend_verified_result"
        ? "请重新发送当前 job 已验证的 PPTX 附件；不要重新生成内容、重新渲染或新建作品。"
        : diagnosis.recovery_action === "choose_output_destination"
          ? "请保留现有文件，不要覆盖；先请用户选择新的输出路径或文件名，再继续原 job。"
          : "";
  return [
    isPreview
      ? "前端预览失败了，请根据下面的错误上下文定位并修复。"
      : isPresentationRecovery
        ? "上一轮 PPT 交付未完整结束，请按原任务身份安全续接。"
        : "上一轮运行失败了，请根据下面的归因上下文定位并修复。",
    "",
    input.runId ? `run_id: ${input.runId}` : "",
    diagnosis.job_id ? `presentation_job_id: ${diagnosis.job_id}` : "",
    diagnosis.recovery_action
      ? `presentation_recovery_action: ${diagnosis.recovery_action}`
      : "",
    input.workspaceDir ? `工作区: ${input.workspaceDir}` : "",
    input.stageLabel ? `阶段: ${input.stageLabel}` : "",
    `归因: ${diagnosis.summary}`,
    `建议: ${diagnosis.next_action}`,
    diagnosis.evidence ? `证据: ${diagnosis.evidence}` : "",
    input.eventDetail ? `事件详情: ${input.eventDetail}` : "",
    harnessLines.length ? ["", ...harnessLines].join("\n") : "",
    "",
    isPreview
      ? "请优先检查 dev server 是否仍在运行、端口是否一致、浏览器控制台首个错误和网络请求失败；修复后重新打开预览验证。"
      : isPresentationRecovery
        ? presentationInstruction
        : "请先复现最小失败，再修复首个确定性错误，最后运行对应验证命令。",
  ]
    .filter(Boolean)
    .join("\n");
}
