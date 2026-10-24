import { describe, expect, it } from "vitest";
import {
  buildRuntimeRepairPrompt,
  runtimeFailureDiagnosisFromPayload,
} from "./runtimeFailureDiagnosis";

describe("runtimeFailureDiagnosis", () => {
  it("normalizes diagnosis payloads", () => {
    expect(
      runtimeFailureDiagnosisFromPayload({
        failure_diagnosis: {
          category: "preview",
          summary: "前端预览或浏览器验证失败",
          next_action: "检查 dev server",
          evidence: "ERR_CONNECTION_REFUSED",
        },
      }),
    ).toMatchObject({
      category: "preview",
      summary: "前端预览或浏览器验证失败",
      next_action: "检查 dev server",
    });
  });

  it("keeps an explicit runtime diagnosis ahead of presentation recovery metadata", () => {
    expect(
      runtimeFailureDiagnosisFromPayload({
        failure_diagnosis: {
          category: "timeout",
          summary: "后端已明确判定超时",
          next_action: "从最后检查点继续。",
        },
        metadata: {
          error_code: "presentation_delivery_file_error",
          job_id: "ppt-job-explicit-priority",
          publish_ready: false,
          requires_publish: false,
          recovery_actions: ["retry_file_stage"],
        },
      }),
    ).toMatchObject({
      category: "timeout",
      summary: "后端已明确判定超时",
      next_action: "从最后检查点继续。",
    });
  });

  it("turns a presentation file-stage failure into same-job recovery context", () => {
    const diagnosis = runtimeFailureDiagnosisFromPayload({
      metadata: {
        error_code: "presentation_delivery_file_error",
        job_id: "ppt-job-file-stage",
        publish_ready: false,
        requires_publish: false,
        requires_human: false,
        recovery_actions: ["retry_file_stage"],
      },
    });

    expect(diagnosis).toMatchObject({
      category: "presentation_delivery",
      summary: "PPT 文件交付尚未完成",
      job_id: "ppt-job-file-stage",
      recovery_action: "retry_file_stage",
      publish_ready: false,
      requires_publish: false,
    });
    expect(diagnosis?.next_action).toContain("原任务");
    expect(diagnosis?.next_action).toContain("文件交付阶段");

    const prompt = buildRuntimeRepairPrompt({ diagnosis: diagnosis! });
    expect(prompt).toContain("presentation_job_id: ppt-job-file-stage");
    expect(prompt).toContain("presentation_recovery_action: retry_file_stage");
    expect(prompt).toContain("不要重新生成已验真的页面");
    expect(prompt).toContain("不要新建作品");
  });

  it("turns a verified response failure into resend-only recovery context", () => {
    const diagnosis = runtimeFailureDiagnosisFromPayload({
      metadata: {
        error_code: "presentation_delivery_response_error",
        job_id: "ppt-job-response",
        publish_ready: true,
        requires_publish: true,
        requires_human: false,
        delivery_status: "response_failed",
      },
    });

    expect(diagnosis).toMatchObject({
      category: "presentation_delivery",
      summary: "PPT 已通过核验，附件响应尚未送达",
      job_id: "ppt-job-response",
      recovery_action: "resend_verified_result",
      publish_ready: true,
      requires_publish: true,
    });
    const prompt = buildRuntimeRepairPrompt({ diagnosis: diagnosis! });
    expect(prompt).toContain("重新发送当前 job 已验证的 PPTX 附件");
    expect(prompt).toContain("不要重新生成");
  });

  it("turns a presentation delivery conflict into a human-safe choice", () => {
    const diagnosis = runtimeFailureDiagnosisFromPayload({
      metadata: {
        error_code: "presentation_delivery_conflict",
        job_id: "ppt-job-conflict",
        publish_ready: false,
        requires_publish: false,
        requires_human: true,
      },
    });

    expect(diagnosis).toMatchObject({
      category: "blocked",
      summary: "目标 PPTX 已被修改或占用",
      job_id: "ppt-job-conflict",
      recovery_action: "choose_output_destination",
      requires_human: true,
    });
    expect(diagnosis?.next_action).toContain("保留现有文件");
    expect(diagnosis?.next_action).toContain("路径或文件名");
  });

  it("does not synthesize presentation recovery from unrelated or unidentified metadata", () => {
    expect(
      runtimeFailureDiagnosisFromPayload({
        metadata: {
          error_code: "filesystem_error",
          job_id: "ordinary-job",
          recovery_actions: ["retry_file_stage"],
        },
      }),
    ).toBeNull();
    expect(
      runtimeFailureDiagnosisFromPayload({
        metadata: {
          error_code: "presentation_delivery_file_error",
          recovery_actions: ["retry_file_stage"],
        },
      }),
    ).toBeNull();
    expect(
      runtimeFailureDiagnosisFromPayload({
        metadata: {
          error_code: "presentation_delivery_file_error",
          job_id: "ppt-job-string-flags",
          publish_ready: "false",
          requires_publish: "false",
          requires_human: "false",
          recovery_actions: ["retry_file_stage"],
        },
      }),
    ).toBeNull();
  });

  it("builds preview-specific repair prompts", () => {
    const prompt = buildRuntimeRepairPrompt({
      runId: "run-preview",
      stageLabel: "预览错误：Vite",
      workspaceDir: "/tmp/app",
      eventDetail: "net::ERR_CONNECTION_REFUSED localhost:5173",
      previewHarness: {
        url: "http://localhost:5173/",
        fetch_url: "http://localhost:5173/",
        ok: false,
        summary: "本地预览不可达。",
        findings: [
          {
            severity: "critical",
            category: "availability",
            message: "本地预览不可达。",
            evidence: "Connection refused",
          },
        ],
        network_errors: [
          {
            url: "http://localhost:5173/",
            status: null,
            error: "Connection refused",
          },
        ],
      },
      diagnosis: {
        label: "#2 预览错误：Vite",
        category: "preview",
        summary: "前端预览或浏览器验证失败",
        next_action: "先确认 dev server 端口和页面控制台首个错误。",
        evidence: "ERR_CONNECTION_REFUSED",
      },
    });

    expect(prompt).toContain("前端预览失败了");
    expect(prompt).toContain("run_id: run-preview");
    expect(prompt).toContain("预览诊断");
    expect(prompt).toContain("Connection refused");
    expect(prompt).toContain("浏览器控制台首个错误");
  });

  it("keeps stalled repair prompts specific enough to resume safely", () => {
    const prompt = buildRuntimeRepairPrompt({
      runId: "run-stalled",
      stageLabel: "本轮可能卡住",
      workspaceDir: "/Users/example/project",
      eventDetail: "90 秒内没有新的运行事件，且后端没有活动任务",
      diagnosis: {
        label: "#12 本轮可能卡住",
        category: "timeout",
        summary: "执行超时或运行卡住",
        next_action:
          "先确认进程是否还活着和最后输出点；若无活动任务，从最后已完成步骤继续。",
        evidence: "90 秒内没有新的运行事件",
      },
    });

    expect(prompt).toContain("run_id: run-stalled");
    expect(prompt).toContain("工作区: /Users/example/project");
    expect(prompt).toContain("阶段: 本轮可能卡住");
    expect(prompt).toContain("90 秒内没有新的运行事件");
    expect(prompt).toContain("最后已完成步骤继续");
  });
});
