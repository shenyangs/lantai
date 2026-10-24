import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const stylesheet = readFileSync(
  join(process.cwd(), "src/pages/Chat/index.module.less"),
  "utf8",
);

function sliceBetween(startMarker: string, endMarker: string): string {
  const start = stylesheet.lastIndexOf(startMarker);
  const end = stylesheet.indexOf(endMarker, start);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(start);
  return stylesheet.slice(start, end);
}

describe("daily brief compact responsive contract", () => {
  it("uses the approved two-column Brand OS grid at wide widths", () => {
    const start = stylesheet.indexOf(".dailyBriefEntryGrid {");
    const end = stylesheet.indexOf(".dailyBriefEntryCard {", start);
    const baseRule = stylesheet.slice(start, end);

    expect(baseRule).toContain("display: grid;");
    expect(baseRule).toContain(
      "grid-template-columns: repeat(2, minmax(0, 1fr));",
    );
    expect(baseRule).toContain("max-width: none;");
  });

  it("keeps a 390px viewport as wrapped entry cards, not a content panel", () => {
    const wrapBreakpoint = 560;
    const singleColumnBreakpoint = 380;
    const viewportWidth = 390;
    const wrappedRule = sliceBetween(
      "@media (max-width: 560px) {",
      "@media (max-width: 380px) {",
    );

    expect(viewportWidth).toBeLessThanOrEqual(wrapBreakpoint);
    expect(viewportWidth).toBeGreaterThan(singleColumnBreakpoint);
    expect(wrappedRule).toContain(".dailyBriefHomeShelf,");
    expect(wrappedRule).toContain("width: calc(100% - 24px);");
    expect(wrappedRule).toContain(".dailyBriefEntryGrid {");
    expect(wrappedRule).toContain(
      "grid-template-columns: minmax(0, 1fr);",
    );
  });

  it("uses the same entry cards in one column below 380px", () => {
    const singleColumnRule = stylesheet.slice(
      stylesheet.indexOf("@media (max-width: 380px) {"),
      stylesheet.indexOf("@media (prefers-reduced-motion: reduce)"),
    );

    expect(singleColumnRule).toContain(
      ".dailyBriefEntryGrid .continueWorkCard",
    );
    expect(singleColumnRule).toContain("flex-basis: 100%;");
  });
});
