import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  resolve(
    process.cwd(),
    "src/pages/Chat/components/CapabilityMenuButton.module.less",
  ),
  "utf8",
);

function rule(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  const end = css.indexOf("\n}", start);
  return start >= 0 && end > start ? css.slice(start, end + 2) : "";
}

describe("CapabilityMenuButton visual contracts", () => {
  it("matches the compact typography and density of the composer", () => {
    const popover = rule(".popover");
    const menuItem = rule(".menuItem");
    const itemTitle = rule(".itemTitle");

    expect(popover).toContain("width: min(306px, calc(100vw - 28px));");
    expect(menuItem).toContain("min-height: 46px;");
    expect(menuItem).toContain(
      "grid-template-columns: 22px minmax(0, 1fr) auto 16px;",
    );
    expect(itemTitle).toContain("font-size: 13px;");
  });

  it("keeps secondary panels on the same compact scale", () => {
    const optionItems = rule(".optionItem,\n.connectorItem");
    const optionVisuals = rule(".optionIcon,\n.skillVisual,\n.connectorMark");

    expect(optionItems).toContain("min-height: 52px;");
    expect(optionVisuals).toContain("width: 30px;");
    expect(optionVisuals).toContain("height: 30px;");
  });
});
