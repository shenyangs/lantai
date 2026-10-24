import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { status, activate, issue, recover, accountStatus, startWatcha, pollWatcha } =
  vi.hoisted(() => ({
  status: vi.fn(),
  activate: vi.fn(),
  issue: vi.fn(),
  recover: vi.fn(),
  accountStatus: vi.fn(),
  startWatcha: vi.fn(),
  pollWatcha: vi.fn(),
}));

vi.mock("../../api/modules/betaAccess", () => ({
  betaAccessApi: { status, activate, issue, recover },
}));

vi.mock("../../api/modules/cloudAccount", () => ({
  cloudAccountApi: {
    status: accountStatus,
    startWatcha,
    pollWatcha,
    cancelWatcha: vi.fn(),
  },
}));

vi.mock("../../utils/openExternalUrl", () => ({
  prepareExternalNavigation: vi.fn(() => ({
    open: vi.fn(),
    close: vi.fn(),
    setTitle: vi.fn(),
  })),
}));

import { BetaAccessGate, BetaAccessManager } from "./BetaAccessPanel";

describe("BetaAccessPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    status.mockResolvedValue({
      enforcement_required: true,
      status: "not_activated",
      decision_seq: 0,
      snapshot_revision: 0,
      refresh_due: false,
      refresh_scheduled: false,
      renew_by: null,
      expires_at: null,
    });
    accountStatus.mockResolvedValue({
      connected: true,
      user: { id: "member-1", uid: 11, displayName: "测试用户" },
    });
  });

  it("lets an active or grace license through without adding a beta badge", async () => {
    status.mockResolvedValue({
      enforcement_required: true,
      status: "grace",
      decision_seq: 3,
      snapshot_revision: 4,
      refresh_due: true,
      refresh_scheduled: true,
      renew_by: 1,
      expires_at: 2,
    });

    render(
      <BetaAccessGate>
        <main>原有工作台</main>
      </BetaAccessGate>,
    );

    expect(await screen.findByText("原有工作台")).toBeInTheDocument();
    expect(screen.queryByText("VIP")).not.toBeInTheDocument();
    expect(screen.queryByText("已激活")).not.toBeInTheDocument();
  });

  it("keeps the invite input unconstrained and sends the exact value", async () => {
    const user = userEvent.setup();
    activate.mockResolvedValue({ status: "active" });
    render(
      <BetaAccessGate>
        <main>原有工作台</main>
      </BetaAccessGate>,
    );

    const input = await screen.findByLabelText("邀请码");
    expect(input).not.toHaveAttribute("maxlength");
    await user.type(input, "朋友 请坐");
    await user.click(screen.getByRole("button", { name: "进入澜台" }));

    await waitFor(() => {
      expect(activate).toHaveBeenCalledWith(
        expect.objectContaining({ code: "朋友 请坐" }),
      );
    });
    expect(await screen.findByText("原有工作台")).toBeInTheDocument();
  });

  it("keeps pending issuance recoverable without showing signed license material", async () => {
    const user = userEvent.setup();
    activate.mockResolvedValue({
      status: "issuance_pending",
      operation_id: "operation-1",
      recovery_token: "recovery-token-1",
    });
    recover.mockResolvedValue({ status: "active" });
    render(<BetaAccessManager />);

    await user.type(await screen.findByLabelText("邀请码"), "春");
    await user.click(screen.getByRole("button", { name: "激活本机" }));
    await user.click(
      await screen.findByRole("button", { name: "恢复签发结果" }),
    );

    expect(recover).toHaveBeenCalledWith("operation-1", "recovery-token-1");
    expect(document.body.textContent).not.toContain("signed-license");
  });

  it("keeps invite activation available without login and offers account binding", async () => {
    accountStatus.mockResolvedValue({ connected: false });

    render(
      <BetaAccessGate>
        <main>原有工作台</main>
      </BetaAccessGate>,
    );

    expect(
      await screen.findByRole("button", { name: "登录澜台账号" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("邀请码")).toBeInTheDocument();
    expect(
      screen.getByText("无需登录；直接兑换会绑定到这台 Mac。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "恢复已有访问资格" }),
    ).not.toBeInTheDocument();
  });

  it("can issue an existing entitlement without asking for another invite", async () => {
    const user = userEvent.setup();
    issue.mockResolvedValue({ status: "active" });
    render(
      <BetaAccessGate>
        <main>原有工作台</main>
      </BetaAccessGate>,
    );

    await user.click(
      await screen.findByRole("button", { name: "恢复已有访问资格" }),
    );

    expect(issue).toHaveBeenCalledWith(
      expect.objectContaining({ request_id: expect.any(String) }),
    );
    expect(await screen.findByText("原有工作台")).toBeInTheDocument();
  });

  it("does not treat IME composition Enter as an activation submit", async () => {
    render(
      <BetaAccessGate>
        <main>原有工作台</main>
      </BetaAccessGate>,
    );

    const input = await screen.findByLabelText("邀请码");
    fireEvent.change(input, { target: { value: "澜台初见" } });
    fireEvent.compositionStart(input);
    expect(fireEvent.keyDown(input, { key: "Enter", code: "Enter" })).toBe(
      false,
    );
    expect(activate).not.toHaveBeenCalled();
  });
});
