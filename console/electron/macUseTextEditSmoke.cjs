const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { createMacUseController } = require("./macUseController.cjs");

const delay = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

async function waitForTextEditWindow(controller, filename) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const result = await controller.execute({
      action: "list_windows",
      run_id: "textedit-smoke",
      action_id: `list-${attempt}`,
      effect_id: `list-${attempt}`,
    });
    assert.equal(result.ok, true, JSON.stringify(result));
    const target = result.data.windows.find(
      (candidate) =>
        candidate.app_bundle_id === "com.apple.TextEdit" &&
        candidate.window_title.includes(filename),
    );
    if (target) return target;
    await delay(200);
  }
  throw new Error(`TextEdit did not expose the fixture window: ${filename}`);
}

async function main() {
  assert.equal(process.platform, "darwin", "TextEdit smoke is macOS-only");
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mac-use-textedit-"),
  );
  const fixturePath = path.join(
    root,
    `Mac-Use-${process.pid}-${Date.now()}.txt`,
  );
  const filename = path.basename(fixturePath);
  const marker = `Lantai Mac Use verified ${Date.now()}`;
  fs.writeFileSync(fixturePath, "before", { mode: 0o600 });
  const controller = createMacUseController({
    isDev: true,
    electronDir: __dirname,
    resourcesPath: process.resourcesPath || __dirname,
    platform: process.platform,
    userDataPath: path.join(root, "user-data"),
  });
  let targetProcessId = 0;
  try {
    const before = await controller.execute({
      action: "list_windows",
      run_id: "textedit-smoke",
      action_id: "list-before-launch",
      effect_id: "list-before-launch",
    });
    assert.equal(before.ok, true, JSON.stringify(before));
    const preexistingTextEditPids = new Set(
      before.data.windows
        .filter((candidate) => candidate.app_bundle_id === "com.apple.TextEdit")
        .map((candidate) => candidate.process_id),
    );
    const opened = childProcess.spawnSync(
      "/usr/bin/open",
      ["-n", "-a", "TextEdit", fixturePath],
      { encoding: "utf8" },
    );
    assert.equal(opened.status, 0, opened.stderr);
    const target = await waitForTextEditWindow(controller, filename);
    assert.ok(target.process_started_at > 0, JSON.stringify(target));
    assert.equal(
      preexistingTextEditPids.has(target.process_id),
      false,
      "TextEdit smoke must own a dedicated process before cleanup",
    );
    targetProcessId = target.process_id;
    const selected = await controller.execute({
      action: "select_target",
      run_id: "textedit-smoke",
      action_id: "select",
      effect_id: "select",
      target,
    });
    assert.equal(selected.ok, true, JSON.stringify(selected));
    const selectionReceipt = selected.data.selection_receipt;
    const observed = await controller.execute({
      action: "observe_window",
      run_id: "textedit-smoke",
      action_id: "observe-before",
      effect_id: "observe-before",
      target,
      selection_receipt: selectionReceipt,
    });
    assert.equal(observed.ok, true, JSON.stringify(observed));
    const element = observed.data.elements.find(
      (candidate) =>
        candidate.enabled !== false &&
        ["AXTextArea", "AXTextField"].includes(candidate.role) &&
        candidate.sensitive !== true,
    );
    assert.ok(element, JSON.stringify(observed.data.elements));
    const written = await controller.execute({
      action: "set_value",
      run_id: "textedit-smoke",
      action_id: "write",
      effect_id: "textedit-write-effect",
      target,
      selection_receipt: selectionReceipt,
      observation_id: observed.observation_id,
      element_id: element.element_id,
      text: marker,
    });
    assert.equal(written.ok, true, JSON.stringify(written));
    assert.equal(written.dispatch_status, "sent");
    assert.equal(written.effect_status, "verified");
    const verified = await controller.execute({
      action: "observe_window",
      run_id: "textedit-smoke",
      action_id: "verify",
      effect_id: "verify",
      target,
      selection_receipt: selectionReceipt,
      observation_id: observed.observation_id,
      element_id: element.element_id,
      expected_value: marker,
      resolves_effect_id: written.effect_id,
      metadata: { verification: true },
    });
    assert.equal(verified.ok, true, JSON.stringify(verified));
    assert.equal(verified.dispatch_status, "not_sent");
    assert.equal(verified.effect_status, "verified");
    const longWritePromise = controller.execute({
      action: "type_text",
      run_id: "textedit-smoke",
      action_id: "cancelled-write",
      effect_id: "cancelled-write",
      target,
      selection_receipt: selectionReceipt,
      observation_id: verified.observation_id,
      element_id: element.element_id,
      verification_element_id: element.element_id,
      expected_value: "__long_input_must_not_complete__",
      text: "x".repeat(100_000),
    });
    await delay(600);
    const stopStartedAt = performance.now();
    const stopped = await controller.execute({
      action: "stop",
      run_id: "textedit-smoke",
      action_id: "stop-long-write",
      effect_id: "stop-long-write",
    });
    const cancelledWrite = await longWritePromise;
    const stopLatencyMs = performance.now() - stopStartedAt;
    assert.equal(stopped.ok, true, JSON.stringify(stopped));
    assert.equal(cancelledWrite.error_code, "cancelled");
    assert.equal(cancelledWrite.dispatch_status, "sent");
    assert.equal(cancelledWrite.effect_status, "ambiguous");
    assert.equal(cancelledWrite.retry_safe, false);
    assert.ok(
      stopLatencyMs <= 1_000,
      `stop latency ${stopLatencyMs.toFixed(1)}ms`,
    );
    console.log(
      JSON.stringify({
        ok: true,
        target: {
          app_bundle_id: target.app_bundle_id,
          process_id: target.process_id,
          process_started_at: target.process_started_at,
          window_id: target.window_id,
        },
        element: {
          element_id: element.element_id,
          role: element.role,
          identifier: element.identifier,
        },
        write: {
          dispatch_status: written.dispatch_status,
          effect_status: written.effect_status,
        },
        verification: {
          dispatch_status: verified.dispatch_status,
          effect_status: verified.effect_status,
        },
        stop: {
          latency_ms: Math.round(stopLatencyMs),
          cancelled_dispatch_status: cancelledWrite.dispatch_status,
          cancelled_effect_status: cancelledWrite.effect_status,
        },
      }),
    );
  } finally {
    await controller.stopBridgeServer();
    if (targetProcessId > 1) {
      try {
        process.kill(targetProcessId, "SIGTERM");
      } catch {
        // The dedicated TextEdit smoke process may already have exited.
      }
      await delay(300);
    }
    fs.rmSync(root, { recursive: true, force: true });
  }
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error.stack || error.message);
    process.exit(1);
  });
