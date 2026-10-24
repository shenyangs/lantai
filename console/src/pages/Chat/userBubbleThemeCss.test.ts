import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readText = (relativePath: string) =>
  readFileSync(new URL(relativePath, import.meta.url), "utf8");

describe("user message bubble theme contract", () => {
  it("uses the quiet graphite material and approved asymmetric geometry", () => {
    const chatModuleCss = readText("./index.module.less");
    const codexCss = readText("../../styles/modules/chat-codex.css");
    const lantaiCss = readText("../../styles/modules/lantai-chat.css");
    const tokensCss = readText("../../styles/base/tokens.css");

    for (const stylesheet of [chatModuleCss, codexCss, lantaiCss]) {
      expect(stylesheet).toContain("background: var(--lt-chat-user-bg)");
      expect(stylesheet).toContain("color: var(--lt-chat-user-text)");
      expect(stylesheet).toContain(
        "border-radius: var(--lt-chat-user-radius) !important;",
      );
    }

    expect(tokensCss).toContain("--lt-chat-user-bg: #464b49;");
    expect(tokensCss).toContain("--lt-chat-user-bg: #535b58;");
    expect(tokensCss).toContain("--lt-chat-user-text: #f7f5f0;");
    expect(tokensCss).toContain("--lt-chat-user-radius: 18px 18px 9px 18px;");
    expect(tokensCss).toContain("--lt-chat-user-padding-block: 8px;");
    expect(tokensCss).toContain("--lt-chat-user-padding-inline: 14px;");
    expect(chatModuleCss).toContain(
      "--lt-user-bubble-max-width: min(680px, 64%);",
    );
    expect(chatModuleCss).toContain(
      "padding: var(--lt-chat-user-padding-block) var(--lt-chat-user-padding-inline) !important;",
    );
  });

  it("treats attachments and text as one quiet right-aligned message group", () => {
    const chatModuleCss = readText("./index.module.less");

    expect(chatModuleCss).toContain("gap: var(--lt-chat-user-group-gap);");
    expect(chatModuleCss).toContain(
      "margin: 0 0 var(--lt-chat-user-group-gap) auto;",
    );
    expect(chatModuleCss).toContain(
      "border-radius: var(--lt-chat-user-attachment-radius);",
    );
  });

  it("keeps the default and custom-theme reset aligned with the selected visual", () => {
    const tokensCss = readText("../../styles/base/tokens.css");
    const appearanceTs = readText("../../theme/appearance.ts");

    expect(appearanceTs).toContain(
      'DEFAULT_APPEARANCE_PALETTE: AppearancePaletteId = "mist"',
    );
    expect(tokensCss).toContain('html[data-palette="pine"]');
    expect(tokensCss).toContain("--lt-chat-user-bg: #464b49;");
    expect(appearanceTs).toContain('userBubble: "#464b49",');
    expect(appearanceTs).toContain('userBubble: "#535b58",');
    expect(appearanceTs).toContain("--lt-chat-user-bg: #464b49;");
  });
});
