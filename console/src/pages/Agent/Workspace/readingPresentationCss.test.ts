import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  resolve(process.cwd(), "src/pages/Agent/Workspace/reading.module.less"),
  "utf8",
);

describe("reading presentation contracts", () => {
  it("balances sources, list, and reader columns for a wide desktop", () => {
    expect(css).toContain("minmax(190px, 220px)");
    expect(css).toContain("minmax(400px, 0.9fr)");
    expect(css).toContain("minmax(480px, 1.25fr)");
  });

  it("adapts to the available reading pane width after navigation takes its space", () => {
    expect(css).toMatch(/\.panel\s*\{[^}]*min-width:\s*0;/s);
    expect(css).toContain("container: reading-panel / inline-size");
    expect(css).toContain("@container reading-panel (max-width: 1180px)");
    expect(css).toContain("@container reading-panel (max-width: 640px)");
    expect(css).not.toMatch(/@media\s*\(max-width:\s*(1180|640)px\)/);
  });

  it("keeps the reader typographic and row actions quiet", () => {
    expect(css).toContain("font-size: clamp(20px, 1.45vw, 25px)");
    expect(css).toMatch(
      /\.rowDelete\s*\{[^}]*width:\s*30px;[^}]*background:\s*transparent;[^}]*\}/s,
    );
    expect(css).toContain(".readerHeader");
    expect(css).toContain(".selectionActions");
  });
});
