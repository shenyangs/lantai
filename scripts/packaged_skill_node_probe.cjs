#!/usr/bin/env node
"use strict";

// Enumerate every JavaScript Skill resource, syntax-check it with the packaged
// Node runtime, and resolve every literal import/require against bundled
// dependencies. New scripts enter this release gate automatically.

const fs = require("fs");
const path = require("path");
const { builtinModules } = require("module");
const { spawnSync } = require("child_process");

function argument(name) {
  const index = process.argv.indexOf(name);
  if (index < 0 || !process.argv[index + 1]) {
    throw new Error(`Missing ${name}`);
  }
  return path.resolve(process.argv[index + 1]);
}

function walk(root) {
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isDirectory()) files.push(...walk(target));
    else if (/\.(?:cjs|mjs|js)$/i.test(entry.name)) files.push(target);
  }
  return files.sort();
}

function dependencies(source) {
  const patterns = [
    /\brequire\s*\(\s*["']([^"']+)["']\s*\)/g,
    /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g,
    /\b(?:import|export)\s+(?:[^"']+?\s+from\s+)?["']([^"']+)["']/g,
  ];
  const found = new Set();
  for (const pattern of patterns) {
    for (const match of source.matchAll(pattern)) found.add(match[1]);
  }
  return [...found].sort();
}

function main() {
  const skillsRoot = argument("--skills-root");
  const nodeModules = argument("--node-modules");
  const builtins = new Set([
    ...builtinModules,
    ...builtinModules.map((name) => `node:${name}`),
  ]);
  const scripts = walk(skillsRoot);
  const failures = [];
  let dependencyCount = 0;

  for (const script of scripts) {
    const relative = path.relative(skillsRoot, script).split(path.sep).join("/");
    const syntax = spawnSync(process.execPath, ["--check", script], {
      encoding: "utf8",
      env: { ...process.env, ELECTRON_RUN_AS_NODE: "1" },
      timeout: 10000,
    });
    if (syntax.status !== 0) {
      failures.push({
        script: relative,
        dependency: "",
        errorType: "SyntaxError",
        message: (syntax.stderr || syntax.stdout || "syntax check failed").trim(),
      });
      continue;
    }
    const source = fs.readFileSync(script, "utf8");
    for (const dependency of dependencies(source)) {
      dependencyCount += 1;
      if (builtins.has(dependency)) continue;
      try {
        require.resolve(dependency, {
          paths: [path.dirname(script), path.dirname(nodeModules)],
        });
      } catch (error) {
        failures.push({
          script: relative,
          dependency,
          errorType: error && error.code ? error.code : "ResolveError",
          message: String(error && error.message ? error.message : error),
        });
      }
    }
  }

  const failedScripts = new Set(failures.map((item) => item.script));
  const report = {
    schemaVersion: 1,
    status: failures.length ? "failed" : "ok",
    skillsRoot,
    nodeModules,
    scriptCount: scripts.length,
    dependencyCount,
    passedCount: scripts.length - failedScripts.size,
    failedCount: failedScripts.size,
    failures,
  };
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  return failures.length ? 1 : 0;
}

try {
  process.exitCode = main();
} catch (error) {
  process.stderr.write(`${String(error && error.stack ? error.stack : error)}\n`);
  process.exitCode = 2;
}
