const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { resolveMacUseHelperPath } = require("./macUseController.cjs");

function launch(appPath, requestPath, responsePath, diagnosticsPath) {
  return new Promise((resolve) => {
    childProcess.execFile(
      "/usr/bin/open",
      [
        "-n",
        "-W",
        "-g",
        "-a",
        appPath,
        "--stdout",
        "/dev/null",
        "--stderr",
        diagnosticsPath,
        "--args",
        "--request",
        requestPath,
        "--response",
        responsePath,
      ],
      { timeout: 15_000 },
      () => {
        try {
          resolve(JSON.parse(fs.readFileSync(responsePath, "utf8")));
        } catch {
          resolve({ ok: false, error_code: "missing_response" });
        }
      },
    );
  });
}

async function main() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-mac-lock-smoke-"));
  const helperPath = resolveMacUseHelperPath({
    isDev: true,
    electronDir: __dirname,
    resourcesPath: __dirname,
  });
  const appPath = path.resolve(helperPath, "../../..");
  const inputLockPath = path.join(root, "shared-input.lock");
  const startAt = Date.now() / 1000 + 1.5;
  try {
    const tasks = Array.from({ length: 24 }, async (_item, index) => {
      const directory = path.join(root, String(index));
      fs.mkdirSync(directory, { recursive: true });
      const requestPath = path.join(directory, "request.json");
      const responsePath = path.join(directory, "response.json");
      const diagnosticsPath = path.join(directory, "diagnostics.log");
      fs.writeFileSync(
        requestPath,
        JSON.stringify({
          protocol_version: 1,
          action: "_test_input_lock",
          test_only: true,
          start_at: startAt,
          hold_ms: 500,
          run_id: `lock-${index}`,
          action_id: `lock-${index}`,
          effect_id: `lock-${index}`,
          input_lock_path: inputLockPath,
        }),
        { mode: 0o600 },
      );
      fs.writeFileSync(responsePath, "", { mode: 0o600 });
      fs.writeFileSync(diagnosticsPath, "", { mode: 0o600 });
      return launch(appPath, requestPath, responsePath, diagnosticsPath);
    });
    const results = await Promise.all(tasks);
    const acquired = results.filter((result) => result.ok).length;
    const blocked = results.filter(
      (result) => result.error_code === "input_busy",
    ).length;
    assert.equal(acquired, 1, JSON.stringify(results));
    assert.equal(blocked, 23, JSON.stringify(results));
    console.log(
      JSON.stringify({ ok: true, contenders: 24, acquired, blocked }),
    );
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error.stack || error.message);
    process.exit(1);
  });
