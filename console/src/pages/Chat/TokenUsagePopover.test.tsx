import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TokenUsagePopoverContent } from "./index";

describe("TokenUsagePopoverContent", () => {
  it("labels missing cache telemetry instead of showing an ambiguous question mark", () => {
    render(
      <TokenUsagePopoverContent
        usage={{
          providerId: "kimi-codingplan",
          modelName: "kimi-for-coding",
          inputTokens: 1_000,
          outputTokens: 50,
          cachedTokens: null,
          cacheHitRate: null,
          totalTokens: 1_050,
          contextTokens: 1_000,
          contextLimit: null,
          calls: [
            {
              providerId: "kimi-codingplan",
              modelName: "kimi-for-coding",
              inputTokens: 1_000,
              outputTokens: 50,
              cachedTokens: null,
              cacheHitRate: null,
            },
          ],
          updatedAt: 1,
        }}
        contextStats={null}
        routing={null}
      />,
    );

    expect(screen.getAllByText("未上报")).toHaveLength(4);
    expect(screen.queryByText("?")).not.toBeInTheDocument();
  });

  it("keeps the primary context signal visible and moves diagnostics behind disclosure", () => {
    render(
      <TokenUsagePopoverContent
        usage={null}
        contextStats={{
          total_messages: 2,
          compressed_summary_tokens: 0,
          messages_tokens: 19,
          estimated_tokens: 19,
          max_input_length: 262_144,
          context_usage_ratio: 0,
          requested_window: 262_144,
          effective_window: 262_144,
          actual_window: null,
          context_profile_status: "unverified",
          context_policy: {
            mode: "smart",
            hard_window: 262_144,
            usable_input_tokens: 204_280,
            target_input_tokens: 163_424,
            output_reserve: 50_000,
            safety_margin: 7_864,
            used_unknown_fallback: false,
          },
          categories: [
            {
              key: "system",
              estimated_tokens: 8,
              upper_bound_tokens: 9,
              method: "estimated",
              selected: true,
              share: 0.421,
            },
            {
              key: "history",
              estimated_tokens: 11,
              upper_bound_tokens: 12,
              method: "estimated",
              selected: true,
              share: 0.579,
            },
          ],
          count_source: "manifest_estimate",
          compaction: {
            active: false,
            summary_tokens: 0,
            budget_discarded: 0,
            redundant_discarded: 0,
            provider_recovery: false,
            reason_codes: [],
            explanation:
              "本轮尚未触发压缩，澜台会按模型真实窗口动态保留上下文。",
          },
          updatedAt: 1,
        }}
        routing={{
          providerId: "lantai-smoke-openai",
          modelName: "lantai-smoke-model",
          reasons: [],
          mode: "local_first",
          complexity: "light",
          estimatedContextTokens: 19,
          updatedAt: 1,
        }}
      />,
    );

    expect(screen.getByText("本轮上下文")).toBeInTheDocument();
    expect(screen.getByText("正常")).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "上下文占用" }),
    ).toHaveAttribute("aria-valuenow", "0");
    expect(screen.getByText("用户上限")).toBeInTheDocument();
    expect(screen.getByText("模型硬上限")).toBeInTheDocument();
    expect(screen.getByText("本轮可用输入")).toBeInTheDocument();
    expect(screen.getByText("上下文都用在了哪里")).toBeInTheDocument();
    expect(screen.getByText("系统规则")).toBeInTheDocument();
    expect(screen.getByText("近期对话")).toBeInTheDocument();
    expect(screen.getAllByText("本轮清单估算")).toHaveLength(2);
    expect(screen.getByText(/本轮尚未触发压缩/)).toBeInTheDocument();
    expect(screen.getByText("输出与思考预留")).toBeInTheDocument();
    expect(screen.getByText("安全空间")).toBeInTheDocument();
    expect(screen.getByText("模型路由")).toBeInTheDocument();
    expect(screen.getByText("技术明细")).toBeInTheDocument();
    expect(screen.getByText("暂无本轮 Token 消耗明细")).toBeInTheDocument();

    expect(screen.queryByText("接近动态预算")).not.toBeInTheDocument();
    expect(screen.queryByText("需要压缩或降级")).not.toBeInTheDocument();
  });

  it("derives the visible percentage from the same numerator and denominator", () => {
    render(
      <TokenUsagePopoverContent
        usage={{
          providerId: "deepseek",
          modelName: "deepseek-v4-flash",
          inputTokens: 9_596,
          outputTokens: 0,
          cachedTokens: null,
          cacheHitRate: null,
          totalTokens: 9_596,
          contextTokens: 9_596,
          contextLimit: 177_200,
          calls: [],
          updatedAt: 2,
        }}
        contextStats={{
          total_messages: 10,
          compressed_summary_tokens: 0,
          messages_tokens: 2_959,
          estimated_tokens: 2_959,
          max_input_length: 262_144,
          context_usage_ratio: 1.7,
          requested_window: 262_144,
          effective_window: 262_144,
          actual_window: null,
          context_profile_status: "verified",
          context_policy: {
            mode: "smart",
            hard_window: 262_144,
            usable_input_tokens: 221_500,
            target_input_tokens: 177_200,
            output_reserve: 32_000,
            safety_margin: 8_644,
            used_unknown_fallback: false,
          },
          updatedAt: 1,
        }}
        routing={null}
      />,
    );

    expect(screen.getByText("5.4%")).toBeInTheDocument();
    expect(screen.queryByText("1.7%")).toBeNull();
  });

  it("requires an explicit risk confirmation before saving a per-model context override", async () => {
    const user = userEvent.setup();
    const onSaveContextWindow = vi.fn().mockResolvedValue(undefined);

    render(
      <TokenUsagePopoverContent
        usage={null}
        contextStats={{
          total_messages: 0,
          compressed_summary_tokens: 0,
          messages_tokens: 0,
          estimated_tokens: 0,
          max_input_length: 86_000,
          context_usage_ratio: 0,
          requested_window: 262_144,
          model_window: 131_072,
          effective_window: 131_072,
          actual_window: null,
          context_profile_status: "verified",
          context_policy: {
            mode: "smart",
            hard_window: 131_072,
            usable_input_tokens: 107_480,
            target_input_tokens: 85_984,
            output_reserve: 19_660,
            safety_margin: 3_932,
            used_unknown_fallback: false,
          },
          updatedAt: 1,
        }}
        routing={null}
        contextWindowTarget={{
          providerId: "minimax-cn",
          modelId: "MiniMax-M2.7",
          modelName: "MiniMax M2.7",
        }}
        onSaveContextWindow={onSaveContextWindow}
      />,
    );

    await user.click(screen.getByRole("button", { name: "修改用户上限" }));
    const input = screen.getByRole("spinbutton", { name: "用户上下文上限" });
    await user.clear(input);
    await user.type(input, "196608");
    await user.click(screen.getByRole("button", { name: "保存上下文上限" }));

    expect(onSaveContextWindow).not.toHaveBeenCalled();
    expect(
      await screen.findByText("确认修改 MiniMax M2.7 的上下文上限"),
    ).toBeInTheDocument();
    expect(screen.getByText(/错误设置可能导致请求失败/)).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "我了解风险，确认保存" }),
    );
    await waitFor(() =>
      expect(onSaveContextWindow).toHaveBeenCalledWith(196_608),
    );
  });
});
