import { describe, expect, it } from "vitest";
import {
  formatRoutingDetail,
  normalizeRoutingSnapshot,
  routingSnapshotFromResponse,
} from "./routingDecision";

describe("routingDecision", () => {
  it("normalizes aliases and explanation fields from runtime events", () => {
    const snapshot = normalizeRoutingSnapshot({
      route: "cloud",
      provider: "openai",
      model: "gpt-4.1",
      reason: "短续写消息，沿用上一轮模型路由解释",
      reasons: ["上一轮需要跨文件修改"],
      mode: "recommended",
      complexity: "complex",
      previous_route_inherited: true,
      inherited_from_run_id: "run-prev",
      estimated_context_tokens: 2400,
      estimated_cost_points: 12,
    });

    expect(snapshot).toMatchObject({
      route: "cloud",
      providerId: "openai",
      modelName: "gpt-4.1",
      mode: "recommended",
      complexity: "complex",
      previousRouteInherited: true,
      inheritedFromRunId: "run-prev",
      estimatedContextTokens: 2400,
      estimatedCostPoints: 12,
    });
    expect(formatRoutingDetail(snapshot)).toContain("openai/gpt-4.1");
    expect(formatRoutingDetail(snapshot)).toContain("沿用上一轮");
  });

  it("normalizes camelCase snapshots stored by the frontend", () => {
    const snapshot = normalizeRoutingSnapshot({
      route: "cloud",
      providerId: "openai",
      modelName: "gpt-4.1",
      reason: "短续写消息，沿用上一轮模型路由解释",
      reasons: ["短续写消息，沿用上一轮模型路由解释"],
      mode: "recommended",
      complexity: "complex",
      previousRouteInherited: true,
      inheritedFromRunId: "run-prev",
      estimatedContextTokens: 2400,
      estimatedCostPoints: 12,
      fallbackReason: "provider fallback",
    });

    expect(snapshot).toMatchObject({
      providerId: "openai",
      modelName: "gpt-4.1",
      previousRouteInherited: true,
      inheritedFromRunId: "run-prev",
      estimatedContextTokens: 2400,
      estimatedCostPoints: 12,
      fallbackReason: "provider fallback",
    });
  });

  it("never paints the current model onto a historical response without provenance", () => {
    expect(routingSnapshotFromResponse({ status: "completed" })).toBeNull();
    expect(
      routingSnapshotFromResponse({
        status: "completed",
        meta: {
          model_routing_decision: {
            provider: "deepseek",
            model: "deepseek-v4-flash",
          },
        },
      }),
    ).toMatchObject({
      providerId: "deepseek",
      modelName: "deepseek-v4-flash",
    });
  });
});
