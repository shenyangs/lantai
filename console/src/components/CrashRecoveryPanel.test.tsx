import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  createSafeCrashContext,
  formatSafeCrashReport,
} from "./crashRecovery";
import { CrashRecoveryPanel } from "./CrashRecoveryPanel";

describe("CrashRecoveryPanel", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/console/#/memory/private-entry?token=SECRET");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("copies only a coarse route and public build identity", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const context = createSafeCrashContext("route-render");

    render(
      <CrashRecoveryPanel
        context={context}
        buildIdentity={{ version: "2.1.7", shortGitSha: "1234567890ab" }}
        onRetry={vi.fn()}
        onReload={vi.fn()}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "复制安全定位信息" }),
    );

    expect(writeText).toHaveBeenCalledOnce();
    const report = writeText.mock.calls[0][0] as string;
    expect(report).toContain("页面范围：/memory");
    expect(report).toContain("版本：2.1.7");
    expect(report).toContain("构建：1234567890ab");
    expect(report).not.toContain("private-entry");
    expect(report).not.toContain("SECRET");
    expect(screen.getByText("安全定位信息已复制")).toBeVisible();
  });

  it("does not include an error, query, hash detail, or identifiers in the report", () => {
    const context = createSafeCrashContext("app-render");
    const report = formatSafeCrashReport(context, null);

    expect(report).toContain("故障类别：工作台界面显示");
    expect(report).not.toContain("private-entry");
    expect(report).not.toContain("token");
    expect(report).not.toContain("SECRET");
    expect(report).not.toContain("request_id");
    expect(report).not.toContain("run_id");
  });

  it("reports clipboard failure without retrying or reloading", async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    const reload = vi.fn();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });

    render(
      <CrashRecoveryPanel
        context={createSafeCrashContext("chunk-load")}
        buildIdentity={null}
        onRetry={retry}
        onReload={reload}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "复制安全定位信息" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "未能复制，请先保留当前页面",
    );
    expect(retry).not.toHaveBeenCalled();
    expect(reload).not.toHaveBeenCalled();
  });

  it("puts keyboard focus on the non-destructive recovery action", async () => {
    const user = userEvent.setup();
    const retry = vi.fn();
    const reload = vi.fn();

    render(
      <CrashRecoveryPanel
        context={createSafeCrashContext("route-render")}
        buildIdentity={null}
        onRetry={retry}
        onReload={reload}
      />,
    );

    const retryButton = screen.getByRole("button", {
      name: "重新显示当前界面",
    });
    expect(retryButton).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(retry).toHaveBeenCalledOnce();
    expect(reload).not.toHaveBeenCalled();
  });
});
