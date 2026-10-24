import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const readSibling = (name: string) =>
  readFileSync(fileURLToPath(new URL(name, import.meta.url)), "utf8");

describe("SearchEnhancement layout contracts", () => {
  it("centers the oversized Guanlan artwork independently of grid overflow", () => {
    const css = readSibling("./index.module.less");

    expect(css).toMatch(/\.logo\s*\{[\s\S]*position:\s*relative/);
    expect(css).toMatch(/img\s*\{[\s\S]*position:\s*absolute/);
    expect(css).toMatch(/left:\s*50%/);
    expect(css).toMatch(/top:\s*50%/);
    expect(css).toMatch(/translate\(-50%,\s*calc\(-50%\s*\+\s*8px\)\)/);
  });
});
