import { describe, expect, it } from "vitest";
import {
  extractLatestThemeCssCandidate,
  extractThemeCssCandidates,
} from "./themeCss";

describe("themeCss", () => {
  it("extracts fenced css blocks that override Lantai theme variables", () => {
    const markdown = [
      "这里是一套主题：",
      "```css",
      ":root {",
      "  --lt-accent: #7c5cff;",
      "  --lt-sidebar-bg: #f2f0ff;",
      "}",
      "```",
    ].join("\n");

    const candidate = extractLatestThemeCssCandidate(markdown);

    expect(candidate?.variables).toEqual(["--lt-accent", "--lt-sidebar-bg"]);
    expect(candidate?.css).toContain("--lt-accent: #7c5cff;");
  });

  it("ignores non-css code blocks", () => {
    const markdown = [
      "```ts",
      "const css = ':root { --lt-accent: red; }';",
      "```",
    ].join("\n");

    expect(extractThemeCssCandidates(markdown)).toEqual([]);
  });

  it("uses the latest valid theme css block when multiple are present", () => {
    const markdown = [
      "```css",
      ":root { --lt-accent: #111111; }",
      "```",
      "```css",
      ":root { --lt-accent: #222222; }",
      "```",
    ].join("\n");

    expect(extractLatestThemeCssCandidate(markdown)?.css).toContain("#222222");
  });
});
