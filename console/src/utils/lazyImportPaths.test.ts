import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const srcRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const pagesRoot = path.join(srcRoot, "pages");

function walkSourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === "dist") continue;
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walkSourceFiles(fullPath, out);
      continue;
    }
    if (
      /\.(ts|tsx)$/.test(entry.name) &&
      !entry.name.includes(".test.") &&
      !entry.name.endsWith(".d.ts")
    ) {
      out.push(fullPath);
    }
  }
  return out;
}

function lazyImportTargetExists(importPath: string): boolean {
  if (!importPath.includes("pages/")) return true;
  const relAfterPages = importPath.replace(/^.*pages\//, "");
  const base = path.join(pagesRoot, relAfterPages);
  return [
    base,
    `${base}.tsx`,
    `${base}.ts`,
    path.join(base, "index.tsx"),
    path.join(base, "index.ts"),
  ].some((candidate) => {
    try {
      return fs.statSync(candidate).isFile();
    } catch {
      return false;
    }
  });
}

describe("lazyImportWithRetry page paths", () => {
  it("all static page imports resolve to source files", () => {
    const missing: string[] = [];
    const lazyImportPattern =
      /lazyImportWithRetry\(\s*(["'`])([^"'`]+)\1/g;

    for (const sourceFile of walkSourceFiles(srcRoot)) {
      const source = fs.readFileSync(sourceFile, "utf8");
      for (const match of source.matchAll(lazyImportPattern)) {
        const importPath = match[2];
        if (!lazyImportTargetExists(importPath)) {
          missing.push(
            `${path.relative(srcRoot, sourceFile)} -> ${importPath}`,
          );
        }
      }
    }

    expect(missing).toEqual([]);
  });
});
