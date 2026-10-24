import fs from "node:fs";
import path from "node:path";

const SOURCE_ROOT = path.resolve("src");
const FILE_EXTENSIONS = new Set([".css", ".less", ".scss", ".ts", ".tsx"]);
const BLOCKED_PATTERNS = [
  {
    label: "transition: all",
    regex: /\btransition\s*:\s*all\b/i,
  },
  {
    label: "transition-property: all",
    regex: /\btransition-property\s*:\s*all\b/i,
  },
];

function collectFiles(directory, files = []) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const entryPath = path.join(directory, entry.name);

    if (entry.isDirectory()) {
      collectFiles(entryPath, files);
      continue;
    }

    if (FILE_EXTENSIONS.has(path.extname(entry.name))) {
      files.push(entryPath);
    }
  }

  return files;
}

function findBlockedTransitions(filePath) {
  const text = fs.readFileSync(filePath, "utf8");
  const lines = text.split(/\r?\n/);
  const findings = [];

  lines.forEach((line, index) => {
    for (const pattern of BLOCKED_PATTERNS) {
      if (pattern.regex.test(line)) {
        findings.push({
          filePath,
          line: index + 1,
          label: pattern.label,
          snippet: line.trim(),
        });
      }
    }
  });

  return findings;
}

const findings = collectFiles(SOURCE_ROOT).flatMap(findBlockedTransitions);

if (findings.length > 0) {
  console.error("Motion guardrail failed: avoid broad transition declarations.");
  console.error("Use explicit properties and --lt-motion-* tokens instead.\n");

  for (const finding of findings) {
    const relativePath = path.relative(process.cwd(), finding.filePath);
    console.error(`${relativePath}:${finding.line} ${finding.label}`);
    console.error(`  ${finding.snippet}`);
  }

  process.exit(1);
}

console.log("Motion guardrail passed: no transition: all declarations in src.");
