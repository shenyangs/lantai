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

describe("MCP page layout contracts", () => {
  it("keeps MCP focused on a single custom-client workspace", () => {
    const source = readSibling("./index.tsx");
    const css = readSibling("./index.module.less");
    const content = cssBlock(css, ".content");

    expect(source).toContain("<main className={styles.content}");
    expect(source).not.toContain("GuanlanMCPCard");
    expect(content).toContain("width: min(100%, 880px);");
    expect(content).toContain("padding: 24px 28px 40px;");
  });

  it("separates every top-level MCP surface with a stable vertical rhythm", () => {
    const source = readSibling("./index.tsx");
    const catalogSource = readSibling(
      "./components/OfficialConnectorCatalog.tsx",
    );
    const css = readSibling("./index.module.less");
    const content = cssBlock(css, ".content");

    expect(source).toContain('data-testid="mcp-content"');
    expect(source).toContain('data-testid="mcp-scope-notice"');
    expect(source).toContain('data-testid="mcp-advanced-connections"');
    expect(catalogSource).toContain('data-testid="mcp-official-catalog"');
    expect(content).toContain("display: flex;");
    expect(content).toContain("flex-direction: column;");
    expect(content).toContain("gap: 24px;");
  });

  it("keeps custom clients as a compact vertical list without stretching actions", () => {
    const css = readSibling("./index.module.less");
    const grid = cssBlock(css, ".mcpGrid");
    const cardFooter = cssBlock(css, ".cardFooter");
    const toolsButton = cssBlock(css, ".toolsButton");
    const toggleButton = cssBlock(css, ".toggleButton");
    const deleteButton = cssBlock(css, ".deleteButton");

    expect(grid).toContain("grid-template-columns: minmax(0, 1fr);");
    expect(grid).toContain("padding: 0;");
    expect(cardFooter).toContain("justify-content: flex-end;");
    expect(toolsButton).toContain("margin-right: auto;");
    expect(toolsButton).toContain("flex: 0 0 auto;");
    expect(toggleButton).toContain("flex: 0 0 auto;");
    expect(deleteButton).toContain("flex: 0 0 auto;");
  });

  it("gives each custom client a real inset surface", () => {
    const css = readSibling("./index.module.less");
    const card = cssBlock(css, ".mcpCard");

    expect(card).toContain("padding: 18px 20px");
    expect(card).toContain("border-radius: 12px;");
  });
});
