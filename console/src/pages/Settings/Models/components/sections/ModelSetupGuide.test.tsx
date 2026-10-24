import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ModelSetupGuide } from "./ModelSetupGuide";

describe("ModelSetupGuide", () => {
  it("keeps the Watcha connection and primary-model steps explicit", () => {
    const onConnect = vi.fn();
    render(
      <ModelSetupGuide
        mode="watcha"
        readiness={{
          status: "not_configured",
          provider: null,
          modelId: null,
          providerConfigured: false,
          modelSelected: false,
          connectionVerified: false,
          modelVerified: false,
        }}
        verifying={false}
        verificationError={null}
        onConnect={onConnect}
        onSelectModel={vi.fn()}
        onVerifyModel={vi.fn()}
        onFinish={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText("使用观猹账号")).toBeVisible();
    expect(screen.getByText("配置提供方").closest("li")).toHaveAttribute(
      "data-current",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: /连接 TokenDance/ }));
    expect(onConnect).toHaveBeenCalledOnce();
  });

  it("returns to the preserved conversation after a primary model is ready", () => {
    const onFinish = vi.fn();
    render(
      <ModelSetupGuide
        mode="own"
        readiness={{
          status: "verified",
          provider: {
            id: "company-api",
            name: "Company API",
          } as never,
          modelId: "model-a",
          providerConfigured: true,
          modelSelected: true,
          connectionVerified: true,
          modelVerified: true,
        }}
        verifying={false}
        verificationError={null}
        onConnect={vi.fn()}
        onSelectModel={vi.fn()}
        onVerifyModel={vi.fn()}
        onFinish={onFinish}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText("确认一个小任务").closest("li")).toHaveAttribute(
      "data-current",
      "true",
    );
    fireEvent.click(
      screen.getByRole("button", { name: /带一个小任务回到对话/ }),
    );
    expect(onFinish).toHaveBeenCalledOnce();
  });

  it("requires a real model verification after configuration is saved", () => {
    const onVerifyModel = vi.fn();
    render(
      <ModelSetupGuide
        mode="own"
        readiness={{
          status: "configured_unverified",
          provider: {
            id: "company-api",
            name: "Company API",
          } as never,
          modelId: "model-a",
          providerConfigured: true,
          modelSelected: true,
          connectionVerified: false,
          modelVerified: false,
        }}
        verifying={false}
        verificationError={null}
        onConnect={vi.fn()}
        onSelectModel={vi.fn()}
        onVerifyModel={onVerifyModel}
        onFinish={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText(/尚未通过当前版本的实际调用验证/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /验证当前模型$/ }));
    expect(onVerifyModel).toHaveBeenCalledOnce();
    expect(screen.queryByText("带一个小任务回到对话")).not.toBeInTheDocument();
  });
});
