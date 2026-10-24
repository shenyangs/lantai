import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

function readSibling(name: string) {
  return readFileSync(fileURLToPath(new URL(name, import.meta.url)), "utf8");
}

function cssBlock(css: string, selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return css.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\n\\}`))?.[1];
}

describe("Appearance page layout contracts", () => {
  it("keeps the effect notice separated from and aligned with the first section", () => {
    const source = readSibling("./index.tsx");
    const css = readSibling("./index.module.less");
    const effectNotice = cssBlock(css, ".effectNotice");
    const section = cssBlock(css, ".section");

    expect(source).toContain('data-testid="appearance-effect-notice"');
    expect(source).toContain('data-testid="appearance-sidebar-section"');
    expect(effectNotice).toContain("width: 100%;");
    expect(effectNotice).toContain("max-width: 1040px;");
    expect(effectNotice).toContain("box-sizing: border-box;");
    expect(effectNotice).toContain("margin: 16px auto 20px;");
    expect(section).toContain("width: 100%;");
    expect(section).toContain("max-width: 1040px;");
  });

  it("keeps advanced appearance controls on the same content rail", () => {
    const css = readSibling("./index.module.less");
    const advancedCustomization = cssBlock(css, ".advancedCustomization");

    expect(advancedCustomization).toContain("width: 100%;");
    expect(advancedCustomization).toContain("max-width: 1040px;");
    expect(advancedCustomization).toContain("box-sizing: border-box;");
  });
});
