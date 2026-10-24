import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const shellCss = readFileSync(
  resolve(__dirname, "../../../packages/ui/src/office-shell.css"),
  "utf8",
);

describe("shared Office shell theme contract", () => {
  it("binds the Shen Xingzhi panel surfaces and text to canonical theme tokens", () => {
    expect(shellCss).toContain("--lt-office-ink: var(--text-primary)");
    expect(shellCss).toContain("--lt-office-surface: var(--surface)");
    expect(shellCss).toContain(
      "--lt-office-surface-muted: var(--surface-subtle)",
    );
    expect(shellCss).toContain("--lt-office-border: var(--border)");
    expect(shellCss).toContain("background: var(--lt-office-surface)");
  });

  it("supports both explicit dark mode and the system-theme fallback", () => {
    expect(shellCss).toContain('[data-theme="dark"] {');
    expect(shellCss).toContain("@media (prefers-color-scheme: dark)");
    expect(shellCss).toContain(
      ':root:not([data-theme="light"]):not([data-theme="dark"])',
    );
    expect(shellCss).toContain("--lt-office-accent-strong: #b9cbe0");
    expect(shellCss).toContain(
      "--lt-office-composer-glaze: rgb(30 30 30 / 30%)",
    );
  });

  it("does not pin shared shell surfaces to the former light-only values", () => {
    expect(shellCss).not.toContain("--lt-office-surface: #ffffff");
    expect(shellCss).not.toContain("background: rgb(255 255 255 / 98%)");
    expect(shellCss).not.toContain("background: #dde7f1");
  });
});
