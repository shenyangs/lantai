import {
  CircleAlert,
  CircleStop,
  Play,
  RotateCcw,
  TriangleAlert,
} from "lucide-react";

import { useChatAnywhereOptions } from "./lantaiChat";

type ResponseStatusTone = "stopped" | "warning" | "error";

interface ResponseStatusPresentation {
  tone: ResponseStatusTone;
  title: string;
  detail: string;
  technicalDetail?: string;
}

function errorText(data: unknown, depth = 0): string {
  if (depth > 5) return "请求失败";
  if (!data) return "请求失败";
  if (typeof data === "string") return data;
  if (typeof data !== "object") return String(data);
  const record = data as Record<string, unknown>;
  for (const value of [record.message, record.detail, record.error]) {
    if (typeof value === "string" && value.trim()) return value.trim();
    if (value && typeof value === "object") {
      const nested = errorText(value, depth + 1);
      if (nested !== "请求失败") return nested;
    }
  }
  if (Array.isArray(record.content)) {
    for (const item of record.content) {
      const nested = errorText(item, depth + 1);
      if (nested !== "请求失败") return nested;
    }
  }
  if (typeof record.text === "string" && record.text.trim()) {
    return record.text.trim();
  }
  return "请求失败";
}

function nestedRecord(data: unknown): Record<string, unknown> {
  if (!data || typeof data !== "object") return {};
  const record = data as Record<string, unknown>;
  return record.error && typeof record.error === "object"
    ? (record.error as Record<string, unknown>)
    : record;
}

function responseErrorCode(data: unknown): string {
  const record = nestedRecord(data);
  return String(record.code || record.error_code || record.reason || "")
    .trim()
    .toLowerCase();
}

function isPartialSuccess(data: unknown): boolean {
  if (!data || typeof data !== "object") return false;
  const record = data as Record<string, unknown>;
  const metadata =
    record.metadata && typeof record.metadata === "object"
      ? (record.metadata as Record<string, unknown>)
      : {};
  return record.partial_success === true || metadata.partial_success === true;
}

function isUserStopped(data: unknown, message: string): boolean {
  const record = nestedRecord(data);
  const code = responseErrorCode(data);
  if (
    record.user_stopped === true ||
    ["user_stopped", "user_cancelled", "user_canceled"].includes(code)
  ) {
    return true;
  }
  return /^(?:已|用户已)?停止(?:生成|回复|当前任务)?[。.!]?$/.test(
    message.trim(),
  );
}

function isTechnicalMessage(message: string): boolean {
  return (
    message.length > 108 ||
    /(?:https?:\/\/|\b(?:http|json|traceback|exception|stack|status|code)\b)/i.test(
      message,
    ) ||
    message.includes("{") ||
    message.includes("[")
  );
}

function presentResponseStatus(data: unknown): ResponseStatusPresentation {
  const message = errorText(data);
  const code = responseErrorCode(data);
  if (code === "delivered_with_validation_warning") {
    return {
      tone: "warning",
      title: "文件已生成并保留",
      detail: "质量校验尚未全部通过，可以先打开文件检查。",
    };
  }
  if (code === "completion_gate_failed") {
    return {
      tone: "error",
      title: "交付校验未完成",
      detail: "没有确认到可用文件，可先核实已有结果，再决定是否重新执行。",
    };
  }
  if (isUserStopped(data, message)) {
    return {
      tone: "stopped",
      title: "已停止生成",
      detail: "已生成的内容已保留",
    };
  }
  if (isPartialSuccess(data)) {
    return {
      tone: "warning",
      title: "部分内容已完成",
      detail: "其余内容暂时未能完成",
      technicalDetail: message,
    };
  }
  if (isTechnicalMessage(message)) {
    return {
      tone: "error",
      title: "这次没能完成",
      detail: "执行过程中遇到了问题，可先核实已产生的结果，再决定是否重新执行。",
      technicalDetail: message,
    };
  }
  return {
    tone: "error",
    title: "这次没能完成",
    detail: message,
  };
}

export default function LantaiResponseError({ data }: { data: unknown }) {
  const presentation = presentResponseStatus(data);
  const recovery = useChatAnywhereOptions((value) => value.recovery) as
    | {
        busy?: boolean;
        continueGeneration?: () => void;
        retryGeneration?: () => void;
      }
    | undefined;
  const Icon =
    presentation.tone === "stopped"
      ? CircleStop
      : presentation.tone === "warning"
        ? TriangleAlert
        : CircleAlert;
  const canContinue = Boolean(recovery?.continueGeneration);
  const canRetry = Boolean(recovery?.retryGeneration);

  return (
    <section
      className="lantai-response-status"
      data-tone={presentation.tone}
      role={presentation.tone === "error" ? "alert" : "status"}
      aria-live={presentation.tone === "error" ? "assertive" : "polite"}
    >
      <span className="lantai-response-status-icon" aria-hidden="true">
        <Icon size={15} strokeWidth={1.9} fill="none" />
      </span>
      <div className="lantai-response-status-body">
        <div className="lantai-response-status-copy">
          <strong>{presentation.title}</strong>
          <span>{presentation.detail}</span>
        </div>
        {presentation.technicalDetail ? (
          <details className="lantai-response-status-details">
            <summary>查看原因</summary>
            <p>{presentation.technicalDetail}</p>
          </details>
        ) : null}
      </div>
      {canContinue || canRetry ? (
        <div className="lantai-response-status-actions">
          {presentation.tone !== "error" && canContinue ? (
            <button
              type="button"
              title="把继续说明放入输入框"
              disabled={recovery?.busy}
              onClick={recovery?.continueGeneration}
            >
              <Play size={13} fill="currentColor" aria-hidden="true" />
              准备继续
            </button>
          ) : null}
          {canRetry ? (
            <button
              type="button"
              title="把原请求放回输入框"
              disabled={recovery?.busy}
              onClick={recovery?.retryGeneration}
            >
              <RotateCcw size={13} aria-hidden="true" />
              准备重新执行
            </button>
          ) : null}
          <small className="lantai-response-status-action-effect">
            只会回填输入框；检查后由你发送。
          </small>
        </div>
      ) : null}
    </section>
  );
}
