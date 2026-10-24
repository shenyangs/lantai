import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

describe("KDocs system accessibility preferences", () => {
  it("supports contrast, forced colors, and reduced transparency", () => {
    const styles = readFileSync(
      resolve(
        process.cwd(),
        "src/pages/Chat/components/Kdocs/Kdocs.module.less",
      ),
      "utf8",
    );

    expect(styles).toContain("@media (prefers-contrast: more)");
    expect(styles).toContain("@media (forced-colors: active)");
    expect(styles).toContain("@media (prefers-reduced-transparency: reduce)");
  });

  it("lets the mention list escape the chat composer's clipped shell", () => {
    const chatStyles = readFileSync(
      resolve(process.cwd(), "src/pages/Chat/index.module.less"),
      "utf8",
    );

    expect(chatStyles).toMatch(
      /\.lantai-sender\[data-kdocs-mention-open="true"\]\s*\{[^}]*overflow:\s*visible\s*!important/s,
    );
    expect(chatStyles).toMatch(
      /\.lantai-chat-anywhere-input\[data-kdocs-mention-open="true"\][^{]*\{[^}]*z-index:\s*3/s,
    );
  });

  it("lets expanded reading override the compact overlay layout", () => {
    const styles = readFileSync(
      resolve(
        process.cwd(),
        "src/pages/Chat/components/Kdocs/Kdocs.module.less",
      ),
      "utf8",
    );

    const overlayLayout = styles.indexOf(
      '.workspacePanel[data-layout="overlay"]',
    );
    const expandedLayout = styles.indexOf(".workspacePanel.expandedReader");

    expect(overlayLayout).toBeGreaterThanOrEqual(0);
    expect(expandedLayout).toBeGreaterThan(overlayLayout);
    expect(styles.slice(expandedLayout)).toMatch(
      /\.workspacePanel\.expandedReader\s*\{[^}]*position:\s*fixed;[^}]*inset:\s*32px;[^}]*min-width:\s*0;[^}]*flex:\s*none;[^}]*z-index:\s*1051;/s,
    );
  });
});
