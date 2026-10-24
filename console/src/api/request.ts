import { getApiUrl, clearAuthToken } from "./config";
import { buildAuthHeaders } from "./authHeaders";

export type ApiRequestOperation = "read" | "mutation" | "stream";

export type ApiRequestErrorKind =
  | "authentication"
  | "authorization"
  | "validation"
  | "rate-limit"
  | "http"
  | "timeout"
  | "cancelled"
  | "network";

export type ApiRequestRecoveryAction =
  | "sign-in"
  | "re-pair"
  | "fix-input"
  | "wait-and-retry"
  | "retry-read"
  | "check-result"
  | "inspect-error"
  | "none";

export interface ApiRequestPolicy {
  /**
   * Declares the effect boundary. A mutation deadline is never treated as a
   * failed write or automatically replayed; callers must reconcile first.
   */
  operation: ApiRequestOperation;
  /** Optional bounded wait. Streams and legacy calls remain unchanged. */
  timeoutMs?: number;
}

interface ApiRequestErrorOptions {
  httpStatus?: number;
  code?: string;
  kind: ApiRequestErrorKind;
  requestId?: string;
  owner?: string;
  certainty?: string;
  retryHint?: string;
  recoveryAction: ApiRequestRecoveryAction;
  detail?: Record<string, unknown>;
}

export class ApiRequestError extends Error {
  readonly httpStatus?: number;
  readonly code?: string;
  readonly kind: ApiRequestErrorKind;
  readonly requestId?: string;
  readonly owner?: string;
  readonly certainty?: string;
  readonly retryHint?: string;
  readonly recoveryAction: ApiRequestRecoveryAction;
  readonly detail?: Record<string, unknown>;

  constructor(message: string, options: ApiRequestErrorOptions) {
    super(message);
    this.name = "ApiRequestError";
    this.httpStatus = options.httpStatus;
    this.code = options.code;
    this.kind = options.kind;
    this.requestId = options.requestId;
    this.owner = options.owner;
    this.certainty = options.certainty;
    this.retryHint = options.retryHint;
    this.recoveryAction = options.recoveryAction;
    this.detail = options.detail;
  }
}

export interface SafeRequestErrorDiagnostic {
  httpStatus?: number;
  code?: string;
  kind: ApiRequestErrorKind;
  requestId?: string;
  owner?: string;
  certainty?: string;
  retryHint?: string;
  recoveryAction: ApiRequestRecoveryAction;
}

function safeDiagnosticValue(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const compact = value.replace(/[\r\n\t]/g, " ").trim().slice(0, 160);
  return compact || undefined;
}

/** Allowlist-only diagnostics: raw messages, response bodies and stacks stay out. */
export function getSafeRequestErrorDiagnostic(
  error: ApiRequestError,
): SafeRequestErrorDiagnostic {
  return {
    ...(error.httpStatus !== undefined
      ? { httpStatus: error.httpStatus }
      : {}),
    ...(safeDiagnosticValue(error.code)
      ? { code: safeDiagnosticValue(error.code) }
      : {}),
    kind: error.kind,
    ...(safeDiagnosticValue(error.requestId)
      ? { requestId: safeDiagnosticValue(error.requestId) }
      : {}),
    ...(safeDiagnosticValue(error.owner)
      ? { owner: safeDiagnosticValue(error.owner) }
      : {}),
    ...(safeDiagnosticValue(error.certainty)
      ? { certainty: safeDiagnosticValue(error.certainty) }
      : {}),
    ...(safeDiagnosticValue(error.retryHint)
      ? { retryHint: safeDiagnosticValue(error.retryHint) }
      : {}),
    recoveryAction: error.recoveryAction,
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parseErrorPayload(
  text: string,
  contentType: string,
): Record<string, unknown> | null {
  if (!text || !contentType.includes("application/json")) return null;
  try {
    return asRecord(JSON.parse(text));
  } catch {
    return null;
  }
}

function readContractString(
  payload: Record<string, unknown> | null,
  keys: string[],
): string | undefined {
  if (!payload) return undefined;
  const detail = asRecord(payload.detail);
  const details = asRecord(payload.details);
  const detailDetails = asRecord(detail?.details);
  for (const source of [detailDetails, detail, details, payload]) {
    if (!source) continue;
    for (const key of keys) {
      const value = source[key];
      if (typeof value === "string" && value.trim()) return value.trim();
    }
  }
  return undefined;
}

function classifyHttpError(status: number): {
  kind: ApiRequestErrorKind;
  recoveryAction: ApiRequestRecoveryAction;
} {
  if (status === 401) {
    return { kind: "authentication", recoveryAction: "sign-in" };
  }
  if (status === 403) {
    return { kind: "authorization", recoveryAction: "inspect-error" };
  }
  if (status === 422) {
    return { kind: "validation", recoveryAction: "fix-input" };
  }
  if (status === 429) {
    return { kind: "rate-limit", recoveryAction: "wait-and-retry" };
  }
  return { kind: "http", recoveryAction: "inspect-error" };
}

function inferOperation(method: string): ApiRequestOperation {
  return ["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())
    ? "read"
    : "mutation";
}

function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

function getErrorMessageFromBody(
  text: string,
  contentType: string,
): string | null {
  if (!text) {
    return null;
  }

  if (!contentType.includes("application/json")) {
    return text;
  }

  try {
    const payload = JSON.parse(text) as {
      detail?: unknown;
      message?: unknown;
      error?: unknown;
    };

    if (typeof payload.detail === "string" && payload.detail) {
      return payload.detail;
    }
    if (
      payload.detail &&
      typeof payload.detail === "object" &&
      typeof (payload.detail as { message?: unknown }).message === "string"
    ) {
      return (payload.detail as { message: string }).message;
    }
    if (typeof payload.message === "string" && payload.message) {
      return payload.message;
    }
    if (typeof payload.error === "string" && payload.error) {
      return payload.error;
    }
  } catch {
    return text;
  }

  return text;
}

function isDesktopTokenRequired(
  text: string,
  contentType: string,
): boolean {
  if (!contentType.includes("application/json")) {
    return false;
  }
  try {
    const payload = JSON.parse(text) as {
      code?: unknown;
      detail?: unknown;
    };
    return (
      payload.code === "desktop_token_required" ||
      payload.detail === "Browser pairing required" ||
      payload.detail === "Desktop API token required"
    );
  } catch {
    return false;
  }
}

function redirectToLogin(): void {
  const { pathname, hash = "" } = window.location;
  const basename = /^\/console(?:\/|$)/.test(pathname) ? "/console" : "";
  // Console routes use HashRouter. A full navigation retires the trusted
  // desktop document even when the main process blocks the destination.
  if (
    window.electronAPI?.isDesktop ||
    hash.startsWith("#/") ||
    basename ||
    pathname === "/" ||
    pathname === "/index.html"
  ) {
    const loginRoute = `${basename}/login`;
    if (hash.replace(/^#/, "").split("?")[0] !== loginRoute) {
      window.location.hash = loginRoute;
    }
    return;
  }
  if (pathname !== "/login") {
    window.location.href = "/login";
  }
}

function redirectToBrowserPairing(): void {
  if (typeof window === "undefined" || window.electronAPI?.isDesktop) {
    return;
  }
  const hash = window.location.hash || "";
  if (hash.startsWith("#/browser-pair")) {
    return;
  }
  const redirect = encodeURIComponent(hash || "#/");
  window.location.hash = `/browser-pair?redirect=${redirect}`;
}

function buildHeaders(method?: string, extra?: HeadersInit): Headers {
  // Normalize extra to a Headers instance for consistent handling
  const headers = extra instanceof Headers ? extra : new Headers(extra);

  // Only add Content-Type for methods that typically have a body
  if (method && ["POST", "PUT", "PATCH"].includes(method.toUpperCase())) {
    // Don't override if caller explicitly set Content-Type
    if (!headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
  }

  for (const [key, value] of Object.entries(buildAuthHeaders())) {
    if (!headers.has(key)) {
      headers.set(key, value);
    }
  }

  return headers;
}

export async function request<T = unknown>(
  path: string,
  options: RequestInit = {},
  policy?: ApiRequestPolicy,
): Promise<T> {
  const url = getApiUrl(path);
  const method = options.method || "GET";
  const headers = buildHeaders(method, options.headers);

  const operation = policy?.operation ?? inferOperation(method);
  const callerSignal = options.signal;
  const deadlineController = policy?.timeoutMs
    ? new AbortController()
    : null;
  let deadlineReached = false;
  let deadline: ReturnType<typeof setTimeout> | null = null;
  const forwardCallerAbort = () => {
    deadlineController?.abort(callerSignal?.reason);
  };

  if (deadlineController && callerSignal) {
    if (callerSignal.aborted) forwardCallerAbort();
    else callerSignal.addEventListener("abort", forwardCallerAbort, {
      once: true,
    });
  }
  if (deadlineController && policy?.timeoutMs) {
    deadline = setTimeout(() => {
      deadlineReached = true;
      deadlineController.abort(new DOMException("deadline", "AbortError"));
    }, policy.timeoutMs);
  }

  let response: Response;
  try {
    response = await fetch(url, {
      ...options,
      headers,
      signal: deadlineController?.signal ?? callerSignal,
      credentials: options.credentials ?? "same-origin",
    });
  } catch (error) {
    if (isAbortError(error)) {
      if (deadlineReached) {
        const isMutation = operation === "mutation";
        throw new ApiRequestError(
          isMutation
            ? "Request timed out after submission; result is unknown"
            : "Request timed out",
          {
            kind: "timeout",
            certainty: isMutation ? "unknown" : "not_applicable",
            retryHint: isMutation
              ? "reconcile_before_retry"
              : "retry_read",
            recoveryAction: isMutation ? "check-result" : "retry-read",
          },
        );
      }
      throw new ApiRequestError(
        "Request was cancelled locally; server-side cancellation is not confirmed",
        {
          kind: "cancelled",
          certainty: "unknown",
          retryHint: "do_not_infer_server_cancel",
          recoveryAction: "none",
        },
      );
    }

    const isMutation = operation === "mutation";
    throw new ApiRequestError(
      error instanceof Error ? error.message : "Network request failed",
      {
        kind: "network",
        certainty: isMutation ? "unknown" : "not_applicable",
        retryHint: isMutation ? "reconcile_before_retry" : "retry_read",
        recoveryAction: isMutation ? "check-result" : "retry-read",
      },
    );
  } finally {
    if (deadline) clearTimeout(deadline);
    callerSignal?.removeEventListener("abort", forwardCallerAbort);
  }

  if (!response.ok) {
    const contentType = response.headers.get("content-type") || "";
    const text = await response.text().catch(() => "");
    const payload = parseErrorPayload(text, contentType);
    const detail = asRecord(payload?.detail) ?? asRecord(payload?.details) ?? undefined;
    const code = readContractString(payload, ["code"]);
    const requestId =
      readContractString(payload, ["request_id", "requestId"]) ||
      (() => {
        const candidate = response.headers.get("x-request-id") || "";
        return candidate && candidate !== contentType ? candidate : undefined;
      })();
    const owner = readContractString(payload, ["owner"]);
    const certainty = readContractString(payload, ["certainty"]);
    const retryHint = readContractString(payload, [
      "retry_hint",
      "retryHint",
    ]);
    const classification = classifyHttpError(response.status);

    if (response.status === 401) {
      clearAuthToken();
      redirectToLogin();
      throw new ApiRequestError(
        getErrorMessageFromBody(text, contentType) || "Not authenticated",
        {
          httpStatus: response.status,
          code,
          ...classification,
          requestId,
          owner,
          certainty,
          retryHint,
          detail,
        },
      );
    }

    if (
      response.status === 403 &&
      isDesktopTokenRequired(text, contentType)
    ) {
      redirectToBrowserPairing();
      throw new ApiRequestError("Browser pairing required", {
        httpStatus: response.status,
        code: code || "desktop_token_required",
        kind: "authorization",
        requestId,
        owner,
        certainty,
        retryHint,
        recoveryAction: "re-pair",
        detail,
      });
    }
    const errorMessage = getErrorMessageFromBody(text, contentType);

    const finalMessage =
      errorMessage ||
      `Request failed: ${response.status} ${response.statusText}`;
    throw new ApiRequestError(finalMessage, {
      httpStatus: response.status,
      code,
      ...classification,
      requestId,
      owner,
      certainty,
      retryHint,
      detail: detail ?? payload ?? undefined,
    });
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    return (await response.text()) as unknown as T;
  }

  return (await response.json()) as T;
}
