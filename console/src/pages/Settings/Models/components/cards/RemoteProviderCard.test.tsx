import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ProviderInfo } from "@/api/types";

vi.mock("@/vendor/lantaiDesign", () => ({
  Card: ({ children }: any) => <section>{children}</section>,
  Button: ({ children, onClick, ...props }: any) => (
    <button onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Modal: { confirm: vi.fn() },
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, values?: { count?: number }) =>
      ({
        "models.builtin": "内置",
        "models.models": "模型",
        "models.settings": "设置",
        "models.modelsCount": `${values?.count ?? 0} 个模型`,
        "models.providerAvailable": "配置齐全 · 待验证",
        "models.providerNeedsCheck": "需要连接检查",
        "models.providerNoModels": "缺少模型",
        "models.providerNotConfigured": "未配置",
      })[key] ?? key,
  }),
}));
vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { success: vi.fn(), error: vi.fn() } }),
}));
vi.mock("../modals/ProviderConfigModal", () => ({
  ProviderConfigModal: () => null,
}));
vi.mock("../modals/ModelManageModal", () => ({
  ModelManageModal: () => null,
}));
vi.mock("../ProviderIconComponent", () => ({
  ProviderIcon: () => <span aria-hidden="true">T</span>,
}));

import { RemoteProviderCard } from "./RemoteProviderCard";

function tokenDanceProvider(credentialStatus: string): ProviderInfo {
  return {
    id: "tokendance",
    name: "TokenDance",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [
      {
        id: "qwen3.8-flash",
        name: "Qwen 3.8 Flash",
        supports_multimodal: false,
        supports_image: false,
        supports_video: false,
        generate_kwargs: {},
      },
    ],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: true,
    support_connection_check: true,
    freeze_url: true,
    require_api_key: true,
    api_key: "******",
    base_url: "https://example.com",
    generate_kwargs: {},
    meta: { credential_status: credentialStatus },
  };
}

describe("RemoteProviderCard TokenDance authorization status", () => {
  it("does not claim the provider is configured while authorization is missing", () => {
    render(
      <RemoteProviderCard
        provider={tokenDanceProvider("reauthorization_required")}
        activeModels={null}
        onSaved={vi.fn()}
        featured
      />,
    );

    expect(screen.getAllByText("等待账号授权")).toHaveLength(2);
    expect(screen.queryByText("配置齐全 · 待验证")).not.toBeInTheDocument();
  });

  it("distinguishes account authorization from connection verification", () => {
    render(
      <RemoteProviderCard
        provider={tokenDanceProvider("active")}
        activeModels={null}
        onSaved={vi.fn()}
        featured
      />,
    );

    expect(screen.getByText("账号已授权 · 待验证")).toBeInTheDocument();
    expect(screen.getByText("观猹账号已授权")).toBeInTheDocument();
  });
});
