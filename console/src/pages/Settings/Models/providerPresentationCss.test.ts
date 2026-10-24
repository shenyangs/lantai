import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  resolve(
    process.cwd(),
    "src/pages/Settings/Models/styles/provider-cards.less",
  ),
  "utf8",
);
const page = readFileSync(
  resolve(process.cwd(), "src/pages/Settings/Models/index.tsx"),
  "utf8",
);

describe("provider presentation contracts", () => {
  it("presents TokenDance as one full-width account provider instead of a nested billboard", () => {
    expect(page).toContain("featuredDescription=");
    expect(page).not.toContain("TokenDance 模型广场");
    expect(css).toMatch(
      /\.tokendanceProviderBlock\s*\{[^}]*padding:\s*0 20px;[^}]*\}/s,
    );
    expect(css).toMatch(
      /\.tokendanceProviderCards\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\);[^}]*\}/s,
    );
    expect(css).not.toMatch(/\.tokendanceProviderBlock\s*\{[^}]*border:/s);
  });

  it("keeps featured provider facts and actions in a deliberate horizontal layout", () => {
    expect(css).toContain(".featuredProviderFacts");
    expect(css).toContain(".featuredProviderActions");
    expect(css).toMatch(
      /\.featuredProviderActions\s*\{[^}]*position:\s*static;[^}]*min-width:\s*220px;[^}]*\}/s,
    );
  });
});
