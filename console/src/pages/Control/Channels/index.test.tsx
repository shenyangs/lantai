import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import ChannelsPage from "./index";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "channels.title": "渠道",
        "channels.description":
          "连接外部会话入口，并配置消息权限、提醒方式和回复策略。",
        "channels.filterAll": "全部",
        "channels.builtin": "内置",
        "channels.custom": "自定义",
        "nav.control": "控制",
      })[key] ?? key,
  }),
}));

vi.mock("../../../stores/agentStore", () => ({
  useAgentStore: () => ({ selectedAgent: "default" }),
}));

vi.mock("./components", () => ({
  ChannelCard: () => null,
  ChannelDrawer: () => null,
  getChannelLabel: (key: string) => key,
  useChannels: () => ({
    channels: {},
    descriptors: {},
    orderedKeys: [],
    isBuiltin: () => false,
    loading: false,
    error: "",
    fetchChannels: vi.fn(),
  }),
}));

describe("ChannelsPage", () => {
  it("shows its top-level sidebar label as the only page heading", () => {
    renderWithProviders(<ChannelsPage />, { initialEntries: ["/channels"] });

    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(
      screen.getByRole("heading", { level: 1, name: "渠道" }),
    ).toBeVisible();
    expect(screen.queryByText("控制")).not.toBeInTheDocument();
  });
});
