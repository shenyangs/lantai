import React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getTokenDanceBalance: vi.fn(),
  getTokenDanceUsage: vi.fn(),
  createTokenDancePaymentSession: vi.fn(),
  getTokenDancePaymentSession: vi.fn(),
  redeemTokenDanceCode: vi.fn(),
  startProviderOAuth: vi.fn(),
  getProviderOAuthStatus: vi.fn(),
  discoverModels: vi.fn(),
}));
const navigationMocks = vi.hoisted(() => ({
  setTitle: vi.fn(),
  open: vi.fn(),
  close: vi.fn(),
}));
const messageMocks = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}));

vi.mock("@/api", () => ({ default: apiMocks }));
vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: messageMocks,
  }),
}));
vi.mock("@/utils/openExternalUrl", () => ({
  prepareExternalNavigation: () => navigationMocks,
}));
vi.mock("@/vendor/lantaiDesign", () => ({
  Button: ({ children, onClick, ...props }: any) => (
    <button onClick={onClick} {...props}>
      {children}
    </button>
  ),
  InputNumber: ({ value, onChange, ...props }: any) => (
    <input
      value={value ?? ""}
      onChange={(event) => onChange(Number(event.target.value))}
      {...props}
    />
  ),
  Input: {
    Password: ({ value, onChange, ...props }: any) => (
      <input
        type="password"
        value={value ?? ""}
        onChange={onChange}
        {...props}
      />
    ),
  },
  Modal: ({ open, children, onCancel, title }: any) =>
    open ? (
      <div role="dialog" aria-label={title}>
        {children}
        <button onClick={onCancel}>
          {title === "词元跳动兑换码" ? "关闭兑换码" : "关闭充值"}
        </button>
      </div>
    ) : null,
  QRCode: ({ value }: any) => <div data-testid="payment-qr">{value}</div>,
  Spin: () => <span>loading</span>,
  Tag: ({ children }: any) => <span>{children}</span>,
}));

import { TokenDanceAccountPanel } from "./TokenDanceAccountPanel";
import { ApiRequestError } from "@/api/request";
import type { ProviderInfo } from "@/api/types";

const tokenDanceProvider: ProviderInfo = {
  id: "tokendance",
  name: "TokenDance",
  api_key_prefix: "",
  chat_model: "OpenAIChatModel",
  models: [],
  extra_models: [],
  is_custom: false,
  is_local: false,
  support_model_discovery: true,
  support_connection_check: true,
  freeze_url: true,
  require_api_key: true,
  api_key: "",
  base_url: "https://example.com",
  generate_kwargs: {},
  meta: { oauth_mode: "api_key_pkce" },
};

function renderPanel(connected: boolean) {
  return render(
    <TokenDanceAccountPanel
      provider={tokenDanceProvider}
      connected={connected}
      onSaved={vi.fn()}
    />,
  );
}

function captureThreeSecondTimers() {
  const callbacks: Array<() => void> = [];
  const originalSetTimeout = window.setTimeout.bind(window);
  const spy = vi.spyOn(window, "setTimeout").mockImplementation(((
    handler: TimerHandler,
    timeout?: number,
    ...args: unknown[]
  ) => {
    if (timeout === 3000 && typeof handler === "function") {
      callbacks.push(() => handler(...args));
      return callbacks.length;
    }
    return originalSetTimeout(handler, timeout, ...args);
  }) as typeof window.setTimeout);
  return { callbacks, spy };
}

describe("TokenDanceAccountPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    apiMocks.getTokenDanceBalance.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_a",
      balance: {
        credits: 10_000_000,
        credits_used: 7_500_000,
        balance: 2_500_000,
      },
      balance_cny: "2.500000",
      credits_cny: "10.000000",
      credits_used_cny: "7.500000",
    });
    apiMocks.getTokenDanceUsage.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_a",
      items: [{ id: "u1", model_id: "qwen3.8-flash", cost_cny: "0.012000" }],
      offset: 0,
      limit: 5,
      has_more: false,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the user's TokenDance balance and recent provider usage", async () => {
    renderPanel(true);

    expect(await screen.findByText("¥2.500000")).toBeInTheDocument();
    expect(screen.getByText("累计消耗 ¥7.500000")).toBeInTheDocument();
    expect(screen.getByText("qwen3.8-flash · ¥0.012000")).toBeInTheDocument();
  });

  it("redeems only on explicit submission, then refreshes the balance", async () => {
    apiMocks.redeemTokenDanceCode.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_a",
      credits: 1_000_000,
      credits_cny: "1.000000",
    });
    renderPanel(true);
    await screen.findByText("¥2.500000");

    fireEvent.click(screen.getByRole("button", { name: "兑换码" }));
    fireEvent.change(screen.getByLabelText("兑换码内容"), {
      target: { value: "  gift-code  " },
    });
    expect(apiMocks.redeemTokenDanceCode).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认兑换" }));

    await waitFor(() => {
      expect(apiMocks.redeemTokenDanceCode).toHaveBeenCalledWith("gift-code");
      expect(apiMocks.getTokenDanceBalance).toHaveBeenCalledTimes(2);
    });
    expect(messageMocks.success).toHaveBeenCalledWith(
      "兑换成功，到账 ¥1.000000",
    );
    expect(screen.queryByLabelText("兑换码内容")).not.toBeInTheDocument();
    expect(screen.queryByText("gift-code")).not.toBeInTheDocument();
  });

  it("coalesces repeated redemption submissions", async () => {
    let resolveRedeem: ((value: Record<string, unknown>) => void) | undefined;
    apiMocks.redeemTokenDanceCode.mockReturnValue(
      new Promise((resolve) => {
        resolveRedeem = resolve;
      }),
    );
    renderPanel(true);
    await screen.findByText("¥2.500000");
    fireEvent.click(screen.getByRole("button", { name: "兑换码" }));
    fireEvent.change(screen.getByLabelText("兑换码内容"), {
      target: { value: "gift-code" },
    });

    const submit = screen.getByRole("button", { name: "确认兑换" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(apiMocks.redeemTokenDanceCode).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveRedeem?.({
        authorization_context_id: "tdctx_v1_account_a",
        credits: 1_000_000,
        credits_cny: "1.000000",
      });
      await Promise.resolve();
    });
  });

  it("treats an unknown redemption outcome as needing reconciliation", async () => {
    apiMocks.redeemTokenDanceCode.mockRejectedValue(
      new ApiRequestError(
        "Request timed out after submission; result is unknown",
        {
          kind: "timeout",
          recoveryAction: "check-result",
        },
      ),
    );
    renderPanel(true);
    await screen.findByText("¥2.500000");
    fireEvent.click(screen.getByRole("button", { name: "兑换码" }));
    fireEvent.change(screen.getByLabelText("兑换码内容"), {
      target: { value: "gift-code" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认兑换" }));

    await waitFor(() => {
      expect(messageMocks.error).toHaveBeenCalledWith(
        "兑换结果暂时无法确认，请刷新余额或到 TokenDance 核对后再试。",
      );
    });
    expect(screen.queryByLabelText("兑换码内容")).not.toBeInTheDocument();
    expect(apiMocks.redeemTokenDanceCode).toHaveBeenCalledTimes(1);
  });

  it("does not display redemption credit for another authorization context", async () => {
    apiMocks.redeemTokenDanceCode.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_b",
      credits: 1_000_000,
      credits_cny: "1.000000",
    });
    renderPanel(true);
    await screen.findByText("¥2.500000");
    fireEvent.click(screen.getByRole("button", { name: "兑换码" }));
    fireEvent.change(screen.getByLabelText("兑换码内容"), {
      target: { value: "gift-code" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认兑换" }));

    await waitFor(() => {
      expect(messageMocks.error).toHaveBeenCalledWith(
        "账号授权已变化，兑换结果请到 TokenDance 核对。",
      );
    });
    expect(messageMocks.success).not.toHaveBeenCalled();
    expect(apiMocks.getTokenDanceBalance).toHaveBeenCalledTimes(1);
  });

  it("creates a payment session only after the user clicks recharge", async () => {
    apiMocks.createTokenDancePaymentSession.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_a",
      id: "pay_1",
      amount: 199,
      status: "pending",
      payment_url: "https://tokendance.space/pay/pay_1",
      expired_at: 1_789_300_800,
      created_at: 1_789_299_900,
    });
    renderPanel(true);
    await screen.findByText("¥2.500000");

    fireEvent.change(screen.getByLabelText("充值金额"), {
      target: { value: "199" },
    });
    expect(apiMocks.createTokenDancePaymentSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "充值" }));

    await waitFor(() =>
      expect(apiMocks.createTokenDancePaymentSession).toHaveBeenCalledWith(
        199,
        expect.stringMatching(/^tdpi_[A-Za-z0-9_-]{12,}$/),
      ),
    );
    expect(await screen.findByTestId("payment-qr")).toHaveTextContent(
      "https://tokendance.space/pay/pay_1",
    );
  });

  it("coalesces rapid repeated recharge clicks into one payment intent", async () => {
    let resolveCreate: ((value: Record<string, unknown>) => void) | undefined;
    apiMocks.createTokenDancePaymentSession.mockReturnValue(
      new Promise((resolve) => {
        resolveCreate = resolve;
      }),
    );
    renderPanel(true);
    await screen.findByText("¥2.500000");

    const recharge = screen.getByRole("button", { name: "充值" });
    fireEvent.click(recharge);
    fireEvent.click(recharge);

    expect(apiMocks.createTokenDancePaymentSession).toHaveBeenCalledTimes(1);
    const firstIntent =
      apiMocks.createTokenDancePaymentSession.mock.calls[0][1];
    expect(firstIntent).toMatch(/^tdpi_[A-Za-z0-9_-]{12,}$/);

    await act(async () => {
      resolveCreate?.({
        authorization_context_id: "tdctx_v1_account_a",
        id: "pay_single",
        amount: 100,
        status: "pending",
        payment_url: "https://tokendance.space/pay/pay_single",
        expired_at: Math.floor(Date.now() / 1000) + 60,
        created_at: Math.floor(Date.now() / 1000),
      });
      await Promise.resolve();
    });
  });

  it("polls after 3 seconds and preserves payment details from a sparse response", async () => {
    apiMocks.createTokenDancePaymentSession.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_a",
      id: "pay_sparse",
      amount: 199,
      status: "pending",
      payment_url: "https://tokendance.space/pay/pay_sparse",
      expired_at: Math.floor(Date.now() / 1000) + 60,
      created_at: Math.floor(Date.now() / 1000),
    });
    apiMocks.getTokenDancePaymentSession.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_a",
      id: "pay_sparse",
      status: "pending",
    });
    renderPanel(true);
    await screen.findByText("¥2.500000");
    const timers = captureThreeSecondTimers();

    fireEvent.change(screen.getByLabelText("充值金额"), {
      target: { value: "199" },
    });
    fireEvent.click(screen.getByRole("button", { name: "充值" }));
    await screen.findByTestId("payment-qr");

    expect(apiMocks.getTokenDancePaymentSession).not.toHaveBeenCalled();
    expect(timers.spy).toHaveBeenCalledWith(expect.any(Function), 3000);
    await act(async () => {
      timers.callbacks.shift()?.();
      await Promise.resolve();
    });

    expect(apiMocks.getTokenDancePaymentSession).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("payment-qr")).toHaveTextContent(
      "https://tokendance.space/pay/pay_sparse",
    );
    expect(screen.getByText("充值 ¥199")).toBeInTheDocument();
  });

  it("does not let an in-flight poll reopen a closed payment dialog", async () => {
    let resolvePoll:
      | ((value: { id: string; status: string }) => void)
      | undefined;
    const pollResult = new Promise<{ id: string; status: string }>(
      (resolve) => {
        resolvePoll = resolve;
      },
    );
    apiMocks.createTokenDancePaymentSession.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_a",
      id: "pay_closing",
      amount: 99,
      status: "pending",
      payment_url: "https://tokendance.space/pay/pay_closing",
      expired_at: Math.floor(Date.now() / 1000) + 60,
      created_at: Math.floor(Date.now() / 1000),
    });
    apiMocks.getTokenDancePaymentSession.mockReturnValue(pollResult);
    renderPanel(true);
    await screen.findByText("¥2.500000");
    const timers = captureThreeSecondTimers();

    fireEvent.click(screen.getByRole("button", { name: "充值" }));
    await screen.findByRole("dialog");
    expect(timers.callbacks).toHaveLength(1);
    await act(async () => {
      timers.callbacks.shift()?.();
      await Promise.resolve();
    });
    expect(apiMocks.getTokenDancePaymentSession).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "关闭充值" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => {
      resolvePoll?.({
        authorization_context_id: "tdctx_v1_account_a",
        id: "pay_closing",
        status: "paid",
      });
      await pollResult;
    });

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("waits for each status request before scheduling the next poll", async () => {
    let resolveFirstPoll:
      | ((value: { id: string; status: string }) => void)
      | undefined;
    const firstPoll = new Promise<{ id: string; status: string }>((resolve) => {
      resolveFirstPoll = resolve;
    });
    apiMocks.createTokenDancePaymentSession.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_a",
      id: "pay_serial",
      amount: 499,
      status: "pending",
      payment_url: "https://tokendance.space/pay/pay_serial",
      expired_at: Math.floor(Date.now() / 1000) + 60,
      created_at: Math.floor(Date.now() / 1000),
    });
    apiMocks.getTokenDancePaymentSession
      .mockReturnValueOnce(firstPoll)
      .mockResolvedValue({
        authorization_context_id: "tdctx_v1_account_a",
        id: "pay_serial",
        status: "pending",
      });
    renderPanel(true);
    await screen.findByText("¥2.500000");
    const timers = captureThreeSecondTimers();

    fireEvent.click(screen.getByRole("button", { name: "充值" }));
    await screen.findByRole("dialog");
    expect(timers.callbacks).toHaveLength(1);
    await act(async () => {
      timers.callbacks.shift()?.();
      await Promise.resolve();
    });
    expect(apiMocks.getTokenDancePaymentSession).toHaveBeenCalledTimes(1);
    expect(timers.callbacks).toHaveLength(0);

    await act(async () => {
      resolveFirstPoll?.({
        authorization_context_id: "tdctx_v1_account_a",
        id: "pay_serial",
        status: "pending",
      });
      await firstPoll;
      await Promise.resolve();
    });
    expect(apiMocks.getTokenDancePaymentSession).toHaveBeenCalledTimes(1);
    expect(timers.callbacks).toHaveLength(1);
    await act(async () => {
      timers.callbacks.shift()?.();
      await Promise.resolve();
    });
    expect(apiMocks.getTokenDancePaymentSession).toHaveBeenCalledTimes(2);
  });

  it("makes TokenDance authorization direct and explains every step", async () => {
    apiMocks.startProviderOAuth.mockResolvedValue({
      flow_id: "flow_1",
      authorization_url: "https://tokendance.space/oauth/authorize",
      expires_in: 120,
    });
    navigationMocks.open.mockResolvedValue(undefined);
    renderPanel(false);

    expect(screen.getByText("还差一次账号授权")).toBeInTheDocument();
    expect(screen.getByText("浏览器登录确认")).toBeInTheDocument();
    expect(screen.getByText("回到澜台自动完成")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "立即授权 TokenDance" }),
    );

    await waitFor(() =>
      expect(apiMocks.startProviderOAuth).toHaveBeenCalledWith("tokendance"),
    );
    expect(navigationMocks.open).toHaveBeenCalledWith(
      "https://tokendance.space/oauth/authorize",
    );
    expect(
      screen.getByText("授权页已打开，请在浏览器完成确认。"),
    ).toBeInTheDocument();
    expect(apiMocks.getTokenDanceBalance).not.toHaveBeenCalled();
  });

  it("rejects balance and usage returned for different authorization contexts", async () => {
    apiMocks.getTokenDanceUsage.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_b",
      items: [],
      offset: 0,
      limit: 5,
      has_more: false,
    });

    renderPanel(true);

    await waitFor(() => {
      expect(messageMocks.error).toHaveBeenCalledWith(
        "账号授权已变化，已丢弃本次账户数据，请刷新。",
      );
    });
    expect(screen.queryByText("¥2.500000")).not.toBeInTheDocument();
  });

  it("does not open a payment session returned for another authorization context", async () => {
    apiMocks.createTokenDancePaymentSession.mockResolvedValue({
      authorization_context_id: "tdctx_v1_account_b",
      id: "pay_other",
      amount: 99,
      status: "pending",
      payment_url: "https://tokendance.space/pay/pay_other",
      expired_at: Math.floor(Date.now() / 1000) + 60,
      created_at: Math.floor(Date.now() / 1000),
    });
    renderPanel(true);
    await screen.findByText("¥2.500000");

    fireEvent.click(screen.getByRole("button", { name: "充值" }));

    await waitFor(() => {
      expect(messageMocks.error).toHaveBeenCalledWith(
        "账号授权已变化，充值会话未显示，请刷新后重试。",
      );
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
