import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const args = process.argv.slice(2);
const mode = args.shift();

if (mode !== "--check" && mode !== "--write") {
  console.error(
    "Usage: node scripts/format-changed.mjs <--check|--write> [--base <git-ref>]",
  );
  process.exit(2);
}

let baseRef = process.env.FORMAT_BASE_REF || "";
let baseFromArg = false;
if (args[0] === "--base") {
  args.shift();
  baseRef = args.shift() || "";
  baseFromArg = true;
}
if (/^0+$/.test(baseRef)) baseRef = "";
if (args.length > 0) {
  console.error(`Unexpected argument: ${args[0]}`);
  process.exit(2);
}

let repoRoot = "";

function git(commandArgs, { allowFailure = false } = {}) {
  const result = spawnSync("git", commandArgs, {
    cwd: repoRoot || process.cwd(),
    encoding: "buffer",
    stdio: ["ignore", "pipe", "inherit"],
  });
  if (result.status !== 0 && !allowFailure) process.exit(result.status ?? 1);
  return result;
}

repoRoot = git(["rev-parse", "--show-toplevel"]).stdout.toString().trim();
const consoleRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const consolePrefix = `${relative(repoRoot, consoleRoot).split(sep).join("/")}/`;
const vendorPrefix = `${consolePrefix}vendor/`;
const files = new Set();

function addNullSeparated(output) {
  for (const file of output.toString().split("\0")) {
    if (!file.startsWith(consolePrefix) || file.startsWith(vendorPrefix)) {
      continue;
    }
    if (file && existsSync(join(repoRoot, file))) files.add(file);
  }
}

if (baseRef) {
  const verification = git(["rev-parse", "--verify", `${baseRef}^{commit}`], {
    allowFailure: true,
  });
  if (verification.status !== 0) {
    if (baseFromArg) {
      console.error(
        `Formatting base is not a commit available in this checkout: ${baseRef}`,
      );
      process.exit(2);
    }
    console.warn(
      `Formatting base ${baseRef} is not available in this checkout; ` +
        "checking working-tree changes only.",
    );
  } else {
    addNullSeparated(
      git([
        "diff",
        "--name-only",
        "--diff-filter=ACMRT",
        "-z",
        `${baseRef}...HEAD`,
      ]).stdout,
    );
  }
}

addNullSeparated(
  git(["diff", "--name-only", "--diff-filter=ACMRT", "-z", "--", consolePrefix])
    .stdout,
);
addNullSeparated(
  git([
    "diff",
    "--cached",
    "--name-only",
    "--diff-filter=ACMRT",
    "-z",
    "--",
    consolePrefix,
  ]).stdout,
);
addNullSeparated(
  git(["ls-files", "--others", "--exclude-standard", "-z", "--", consolePrefix])
    .stdout,
);

const changedFiles = [...files].sort();
if (changedFiles.length === 0) {
  console.log("No changed console files to format.");
  process.exit(0);
}

const executableName =
  process.platform === "win32" ? "prettier.cmd" : "prettier";
const prettierExecutable = join(
  consoleRoot,
  "node_modules",
  ".bin",
  executableName,
);
if (!existsSync(prettierExecutable)) {
  console.error("Unable to find the console Prettier executable.");
  process.exit(1);
}

const prettierMode = mode === "--write" ? "--write" : "--check";
const result = spawnSync(
  prettierExecutable,
  [prettierMode, "--ignore-unknown", "--", ...changedFiles],
  { cwd: repoRoot, stdio: "inherit" },
);

if (result.error) {
  console.error(`Unable to run Prettier: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
