import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readText = (relativePath: string) =>
  readFileSync(new URL(relativePath, import.meta.url), "utf8");

function cssBlock(css: string, selector: string) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return css.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\n\\}`))?.[1];
}

describe("knowledge workspace layout contracts", () => {
  it("lets the selected knowledge editor use the complete detail pane", () => {
    const css = readText("./index.module.less");
    const editor = cssBlock(css, ".fileEditor");
    const card = cssBlock(css, ".editorCard");

    expect(editor).toContain("width: 100%;");
    expect(editor).not.toContain("width: 50%;");
    expect(card).toContain("min-width: 0;");
    expect(card).toContain("display: flex;");
    expect(card).toContain("flex-direction: column;");
  });

  it("uses a unified saved-view, content-list, and inspector grid", () => {
    const css = readText("./knowledge.module.less");
    expect(css).toContain(
      "grid-template-columns: 216px minmax(360px, 430px) minmax(0, 1fr);",
    );
    expect(css).toContain(".libraryNavigation");
    expect(css).toContain(".rowDelete");
    expect(css).not.toContain("opacity: 0.62;");
    expect(readText("./index.tsx")).toContain('data-design-seed="4d16f909"');
  });

  it("keeps long knowledge content inside a scrollable reading surface", () => {
    const css = readText("./index.module.less");
    const content = cssBlock(css, ".editorContent");
    const viewer = cssBlock(css, ".markdownViewer");

    expect(content).toContain("min-height: 0;");
    expect(content).toContain("flex: 1;");
    expect(viewer).toContain("min-height: 0;");
    expect(viewer).toContain("overflow: auto;");
    expect(viewer).toContain("scrollbar-gutter: stable;");
  });
});
