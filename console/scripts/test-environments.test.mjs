import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { nodeTestFiles } from "./test-environments.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));

test("Node and DOM projects collect every Console test exactly once", () => {
  const result = spawnSync(
    process.execPath,
    ["node_modules/vitest/vitest.mjs", "list", "--filesOnly", "--json"],
    { cwd: root, encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  const files = JSON.parse(result.stdout);
  const actual = files.map((entry) =>
    path.relative(root, entry.file).split(path.sep).join("/"),
  );
  const expected = readdirSync(path.join(root, "src"), { recursive: true })
    .map(String)
    .map((name) => "src/" + name.split(path.sep).join("/"))
    .filter((name) => /\.(test|spec)\.[cm]?[jt]sx?$/.test(name))
    // Existing node:test legacy file is not a Vitest suite.
    .filter((name) => !name.endsWith("/testConnectionMessage.test.ts"));
  assert.equal(
    new Set(actual).size,
    actual.length,
    "duplicate test collection",
  );
  assert.deepEqual(
    actual.sort(),
    expected.sort(),
    "omitted or unexpected tests",
  );
  assert.equal(new Set(nodeTestFiles).size, nodeTestFiles.length);
  assert.deepEqual(
    files
      .filter((entry) => entry.projectName === "node")
      .map((entry) => path.relative(root, entry.file).split(path.sep).join("/"))
      .sort(),
    [...nodeTestFiles].sort(),
  );
});
