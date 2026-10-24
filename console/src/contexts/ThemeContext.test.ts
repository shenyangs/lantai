import { describe, expect, it } from "vitest";
import { sanitizeCustomThemeCss } from "./ThemeContext";

describe("sanitizeCustomThemeCss", () => {
  it("keeps Lantai design tokens and removes arbitrary selectors", () => {
    const result = sanitizeCustomThemeCss(`
      :root { --lt-accent: #b86424; --lt-surface: #fffefa; }
      body { display: none; }
      .ant-btn { pointer-events: none; }
    `);

    expect(result).toContain("--lt-accent: #b86424;");
    expect(result).toContain("--lt-surface: #fffefa;");
    expect(result).not.toContain("body");
    expect(result).not.toContain("pointer-events");
  });

  it("rejects external URL values", () => {
    const result = sanitizeCustomThemeCss(
      ":root { --lt-sidebar-bg: url(https://bad.test/image.png); }",
    );

    expect(result).toBe("");
  });
});
