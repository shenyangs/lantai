export interface ChatRoutingSnapshot {
  route?: string;
  providerId?: string;
  modelName?: string;
  reason?: string;
  reasons: string[];
  mode?: string;
  complexity?: string;
  previousRouteInherited?: boolean;
  inheritedFromRunId?: string;
  estimatedContextTokens?: number;
  estimatedCostPoints?: number;
  fallbackReason?: string;
  updatedAt: number;
}

export const ROUTING_DECISION_STORAGE_KEY = "lantai.latestRoutingDecision";
export const ROUTING_DECISION_EVENT = "lantai:model-routing-decision";

function readRoutingNumber(source: unknown, ...keys: string[]): number | null {
  if (!source || typeof source !== "object") return null;
  const record = source as Record<string, unknown>;
  for (const key of keys) {
    const raw = record[key];
    if (raw === undefined || raw === null || raw === "") continue;
    const value = Number(raw);
    if (Number.isFinite(value)) return value;
  }
  return null;
}

export function normalizeRoutingSnapshot(
  raw: unknown,
): ChatRoutingSnapshot | null {
  if (!raw || typeof raw !== "object") return null;
  const record = raw as Record<string, unknown>;
  const reasons = Array.isArray(record.reasons)
    ? record.reasons.map((item) => String(item)).filter(Boolean)
    : [];
  const reason = String(record.reason || reasons[0] || "").trim();
  const route = String(record.route || "").trim();
  const providerId = String(
    record.selected_provider ||
      record.provider ||
      record.provider_id ||
      record.providerId ||
      "",
  ).trim();
  const modelName = String(
    record.selected_model ||
      record.model ||
      record.model_name ||
      record.modelName ||
      "",
  ).trim();
  const mode = String(record.mode || "").trim();
  const complexity = String(record.complexity || "").trim();
  const fallbackReason = String(
    record.fallback_reason || record.fallbackReason || "",
  ).trim();
  const inheritedFromRunId = String(
    record.inherited_from_run_id || record.inheritedFromRunId || "",
  ).trim();
  if (
    !route &&
    !providerId &&
    !modelName &&
    !reason &&
    reasons.length === 0 &&
    !mode &&
    !complexity
  ) {
    return null;
  }
  return {
    route,
    providerId,
    modelName,
    reason,
    reasons: reason && reasons[0] !== reason ? [reason, ...reasons] : reasons,
    mode,
    complexity,
    previousRouteInherited: Boolean(
      record.previous_route_inherited ?? record.previousRouteInherited,
    ),
    inheritedFromRunId,
    estimatedContextTokens:
      readRoutingNumber(
        record,
        "estimated_context_tokens",
        "estimatedContextTokens",
      ) ?? undefined,
    estimatedCostPoints:
      readRoutingNumber(
        record,
        "estimated_cost_points",
        "estimatedCostPoints",
      ) ?? undefined,
    fallbackReason,
    updatedAt: Date.now(),
  };
}

export function routingSnapshotFromResponse(
  responseData: unknown,
): ChatRoutingSnapshot | null {
  if (!responseData || typeof responseData !== "object") return null;
  const record = responseData as Record<string, unknown>;
  const meta =
    record.meta && typeof record.meta === "object"
      ? (record.meta as Record<string, unknown>)
      : null;
  return normalizeRoutingSnapshot(
    meta?.model_routing_decision ?? record.model_routing_decision,
  );
}

export function formatRoutingRouteLabel(
  routing: Pick<ChatRoutingSnapshot, "route"> | null | undefined,
): string {
  if (!routing?.route) return "当前模型";
  return routing.route === "cloud" ? "云端模型" : "本地模型";
}

export function formatRoutingModelLabel(
  routing: ChatRoutingSnapshot | null | undefined,
): string {
  if (!routing) return "";
  return (
    [routing.providerId, routing.modelName].filter(Boolean).join("/") ||
    formatRoutingRouteLabel(routing)
  );
}

export function formatRoutingReason(
  routing: ChatRoutingSnapshot | null | undefined,
): string {
  if (!routing) return "";
  return routing.reason || routing.reasons[0] || routing.fallbackReason || "";
}

export function formatRoutingDetail(
  routing: ChatRoutingSnapshot | null | undefined,
): string {
  if (!routing) return "";
  const parts = [
    formatRoutingModelLabel(routing),
    routing.mode ? `模式 ${routing.mode}` : "",
    routing.complexity ? `复杂度 ${routing.complexity}` : "",
    routing.previousRouteInherited ? "沿用上一轮" : "",
    formatRoutingReason(routing),
    routing.fallbackReason ? `fallback: ${routing.fallbackReason}` : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

export function readStoredRoutingSnapshot(): ChatRoutingSnapshot | null {
  if (typeof window === "undefined") return null;
  try {
    return normalizeRoutingSnapshot(
      JSON.parse(
        window.localStorage.getItem(ROUTING_DECISION_STORAGE_KEY) || "null",
      ),
    );
  } catch {
    return null;
  }
}

export function publishRoutingSnapshot(
  routing: ChatRoutingSnapshot | null,
): void {
  if (typeof window === "undefined") return;
  try {
    if (routing) {
      window.localStorage.setItem(
        ROUTING_DECISION_STORAGE_KEY,
        JSON.stringify(routing),
      );
    } else {
      window.localStorage.removeItem(ROUTING_DECISION_STORAGE_KEY);
    }
  } catch {
    // localStorage may be unavailable in private contexts.
  }
  window.dispatchEvent(
    new CustomEvent(ROUTING_DECISION_EVENT, { detail: routing }),
  );
}
