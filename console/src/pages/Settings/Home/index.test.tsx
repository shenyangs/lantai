import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import en from "../../../locales/en.json";
import ja from "../../../locales/ja.json";
import ptBR from "../../../locales/pt-BR.json";
import ru from "../../../locales/ru.json";
import zh from "../../../locales/zh.json";
import { renderWithProviders } from "../../../test/common_setup";
import SettingsHomePage from "./index";

describe("SettingsHomePage", () => {
  it("does not expose the removed voice transcription capability", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(screen.queryByText("语音转写")).toBeNull();
  });

  it("does not expose the retired external assistant capability", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(screen.queryByRole("button", { name: /外部助手/ })).toBeNull();
    expect(screen.queryByText(/Codex 等助手/)).toBeNull();
  });

  it("does not ship stale voice transcription copy in any locale", () => {
    for (const locale of [en, ja, ptBR, ru, zh]) {
      expect(locale.nav).not.toHaveProperty("voiceTranscription");
      expect(locale).not.toHaveProperty("voiceTranscription");
      expect(locale.chat).not.toHaveProperty("speech");
    }
  });

  it("does not ship stale external assistant copy in any locale", () => {
    for (const locale of [en, ja, ptBR, ru, zh]) {
      expect(locale.nav).not.toHaveProperty("acp");
      expect(locale).not.toHaveProperty("acp");
    }
  });

  it("places the workspace command runner in settings", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(
      screen.getByRole("button", { name: /\u5de5\u4f5c\u533a\u547d\u4ee4/ }),
    ).toHaveTextContent("仅供测试与排障");
  });

  it("does not duplicate first-level product areas in settings", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(screen.queryByRole("button", { name: /^系统工具/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^渠道/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^自动化/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^记忆与个性化/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /系统日历/ })).toBeNull();
  });

  it("uses one entry for appearance and personalization", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(
      screen.getByRole("button", { name: /外观与个性化/ }),
    ).toHaveTextContent("主题、应用图标、左侧栏");
    expect(screen.queryByRole("link", { name: /创新实验/ })).toBeNull();
  });

  it("exposes the optional Lantai cloud account without replacing local use", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(screen.getByRole("button", { name: /澜台账号/ })).toHaveTextContent(
      "使用观猹身份登录",
    );
  });

  it("keeps hidden product areas discoverable from the all-functions index", async () => {
    const user = userEvent.setup();
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    await user.click(screen.getByRole("button", { name: "打开全部功能" }));

    expect(screen.queryByRole("button", { name: /任务中心/ })).toBeNull();
    expect(screen.getByRole("button", { name: /能力中心/ })).toBeVisible();
  });

  it("uses page navigation semantics for the settings index", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(screen.getByRole("navigation", { name: "设置目录" })).toBeVisible();
    expect(screen.queryByRole("tablist", { name: "设置索引" })).toBeNull();
  });

  it("presents Guanlan as a dedicated search enhancement setting", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(screen.getByRole("button", { name: /搜索增强/ })).toHaveTextContent(
      "观澜",
    );
    expect(
      screen.getByRole("button", { name: /服务连接 连接常用服务/ }),
    ).not.toHaveTextContent("搜索增强");
  });

  it("keeps the built-in Office capability out of the settings index", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(
      screen.queryByRole("button", { name: /Office 文档能力/ }),
    ).toBeNull();
  });

  it("groups permissions and security by the problem users are solving", () => {
    renderWithProviders(<SettingsHomePage />, {
      initialEntries: ["/settings"],
    });

    expect(screen.getByRole("link", { name: /安全与权限/ })).toBeVisible();
    expect(
      screen.getByRole("button", { name: /权限.*聊天权限仍由各对话单独保存/ }),
    ).toHaveTextContent("聊天权限仍由各对话单独保存");
    expect(
      screen.getByRole("button", { name: /安全策略.*附加风险扫描/ }),
    ).toHaveTextContent("附加风险扫描");
  });
});
