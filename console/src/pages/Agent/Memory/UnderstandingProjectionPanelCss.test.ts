import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const readCss = () =>
  readFileSync(
    resolve(
      process.cwd(),
      "src/pages/Agent/Memory/UnderstandingProjectionPanel.module.less",
    ),
    "utf8",
  );

describe("memory decision layout CSS contracts", () => {
  it("keeps the focused decision readable and lets actions wrap", () => {
    const css = readCss();

    expect(css).toContain("overflow-wrap: anywhere;");
    expect(css).toContain("flex-wrap: wrap;");
    expect(css).toContain("max-width: 64ch;");
    expect(css).toContain("text-wrap: balance;");
  });

  it("collapses the footer into one column on narrow screens", () => {
    const css = readCss();

    expect(css).toContain("@media (max-width: 720px)");
    expect(css).toContain("flex-direction: column;");
  });
});
