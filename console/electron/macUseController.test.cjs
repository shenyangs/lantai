const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  createMacUseController,
  resolveMacUseHelperPath,
  safeIdentifier,
} = require("./macUseController.cjs");

function fixtureController(overrides = {}) {
  const { helperResponder = null, ...controllerOverrides } = overrides;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-mac-use-test-"));
  const electronDir = path.join(root, "electron");
  const helperPath = resolveMacUseHelperPath({
    isDev: true,
    electronDir,
    resourcesPath: root,
  });
  fs.mkdirSync(path.dirname(helperPath), { recursive: true });
  fs.writeFileSync(helperPath, "fixture", { mode: 0o700 });
  const calls = [];
  const controller = createMacUseController({
    isDev: true,
    electronDir,
    resourcesPath: root,
    platform: "darwin",
    userDataPath: path.join(root, "user-data"),
    randomToken: () => "a".repeat(64),
    runHelperProcessImpl: async (request) => {
      calls.push(request);
      const custom = helperResponder
        ? await helperResponder(request)
        : undefined;
      if (custom !== undefined) return custom;
      return {
        response: {
          protocol_version: 1,
          ok: true,
          action: request.action,
          run_id: request.run_id,
          action_id: request.action_id,
          effect_id: request.effect_id,
          policy_status: "allowed",
          dispatch_status: ["list_windows", "observe_window"].includes(
            request.action,
          )
            ? "not_sent"
            : "sent",
          effect_status:
            request.action === "list_windows" ? "not_applicable" : "ambiguous",
          message: "fixture response",
          data:
            request.action === "list_windows"
              ? {
                  windows: [
                    { app_bundle_id: "com.lantai.app", window_id: "1" },
                    { app_bundle_id: "com.apple.TextEdit", window_id: "42" },
                  ],
                }
              : request.action === "observe_window"
                ? {
                    target: {
                      ...request.target,
                      bounds: { x: 0, y: 0, width: 800, height: 600 },
                    },
                    elements: [
                      {
                        element_id: "0.1",
                        role: "AXTextArea",
                        subrole: "",
                        title: "Body",
                        identifier: "fixture-body",
                        value: "before",
                        enabled: true,
                        sensitive: false,
                        bounds: {
                          x: 10,
                          y: 20,
                          width: 400,
                          height: 300,
                        },
                      },
                      {
                        element_id: "0.2",
                        role: "AXButton",
                        subrole: "",
                        title: "Submit",
                        identifier: "fixture-submit",
                        value: "",
                        enabled: true,
                        sensitive: false,
                        bounds: {
                          x: 420,
                          y: 20,
                          width: 80,
                          height: 32,
                        },
                      },
                      {
                        element_id: "0.3",
                        role: "AXTextField",
                        subrole: "AXSecureTextField",
                        title: "Password",
                        identifier: "fixture-password",
                        value: "",
                        enabled: true,
                        sensitive: true,
                        bounds: {
                          x: 10,
                          y: 340,
                          width: 240,
                          height: 28,
                        },
                      },
                    ],
                    ...(request.metadata?.verification
                      ? {
                          expected_value_matches:
                            request.expected_value === "fixture",
                        }
                      : {}),
                  }
                : {},
          evidence: [],
          retry_safe: request.action === "list_windows",
        },
        error: null,
      };
    },
    ...controllerOverrides,
  });
  return { calls, controller, root };
}

async function selectTarget(controller, target, suffix) {
  const selection = await controller.execute({
    action: "select_target",
    run_id: `selection-${suffix}`,
    action_id: `selection-${suffix}`,
    effect_id: `selection-${suffix}`,
    target,
  });
  assert.equal(selection.ok, true, JSON.stringify(selection));
  assert.ok(selection.data.selection_receipt);
  return selection.data.selection_receipt;
}

test("helper path stays inside explicit packaged or development resources", () => {
  assert.equal(
    resolveMacUseHelperPath({
      isDev: false,
      electronDir: "/app/electron",
      resourcesPath: "/app/Resources",
    }),
    "/app/Resources/mac-use/Lantai Mac Use Helper.app/Contents/MacOS/lantai-mac-use-helper",
  );
  assert.equal(safeIdentifier("../unsafe", "fallback"), "fallback");
  assert.equal(safeIdentifier("run-1"), "run-1");
});

test("missing helper never reports policy acceptance as execution", async () => {
  const root = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mac-use-missing-"),
  );
  const controller = createMacUseController({
    platform: "darwin",
    isDev: true,
    electronDir: path.join(root, "electron"),
    userDataPath: path.join(root, "user-data"),
  });

  const result = await controller.execute({
    action: "list_windows",
    run_id: "run-1",
    action_id: "action-1",
    effect_id: "effect-1",
  });

  assert.equal(result.ok, false);
  assert.equal(result.dispatch_status, "not_sent");
  assert.equal(result.effect_status, "failed");
  assert.equal(result.error_code, "mac_use_helper_missing");
});

test("helper transport failures preserve read and write retry semantics", async () => {
  const timeout = () => {
    const error = new Error("fixture timeout");
    error.killed = true;
    return { response: null, error };
  };
  const read = fixtureController({
    helperResponder: async (request) =>
      request.action === "status" ? timeout() : undefined,
  });
  const readResult = await read.controller.execute({
    action: "status",
    run_id: "read-timeout",
    action_id: "read-timeout",
    effect_id: "read-timeout",
  });
  assert.equal(readResult.dispatch_status, "transport_failed");
  assert.equal(readResult.effect_status, "failed");
  assert.equal(readResult.retry_safe, true);
  assert.equal(readResult.error_code, "mac_use_helper_timeout");

  const write = fixtureController({
    helperResponder: async (request) =>
      request.action === "type_text" ? timeout() : undefined,
  });
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const selection = await selectTarget(
    write.controller,
    target,
    "write-timeout",
  );
  const observation = await write.controller.execute({
    action: "observe_window",
    run_id: "write-timeout",
    action_id: "write-timeout-observe",
    effect_id: "write-timeout-observe",
    target,
    selection_receipt: selection,
  });
  const writeResult = await write.controller.execute({
    action: "type_text",
    run_id: "write-timeout",
    action_id: "write-timeout",
    effect_id: "write-timeout",
    target,
    selection_receipt: selection,
    observation_id: observation.observation_id,
    element_id: "0.1",
    verification_element_id: "0.1",
    expected_value: "after",
    text: "hello",
  });
  assert.equal(writeResult.dispatch_status, "unknown");
  assert.equal(writeResult.effect_status, "ambiguous");
  assert.equal(writeResult.retry_safe, false);
  assert.equal(writeResult.error_code, "mac_use_helper_timeout");
});

test("action id deduplicates bridge dispatch", async () => {
  const { calls, controller } = fixtureController();
  const request = {
    action: "list_windows",
    run_id: "run-1",
    action_id: "action-1",
    effect_id: "effect-1",
  };

  const first = await controller.execute(request);
  const replay = await controller.execute(request);

  assert.equal(first.ok, true);
  assert.deepEqual(replay, first);
  assert.equal(calls.length, 1);
  assert.deepEqual(first.data.windows, [
    { app_bundle_id: "com.apple.TextEdit", window_id: "42" },
  ]);
});

test("pause and stop revoke writes until explicit resume", async () => {
  const { calls, controller } = fixtureController();
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };

  const paused = await controller.execute({
    action: "pause",
    run_id: "run-2",
    action_id: "pause-1",
    effect_id: "pause-effect",
  });
  const blocked = await controller.execute({
    action: "type_text",
    run_id: "run-2",
    action_id: "write-1",
    effect_id: "write-effect",
    target,
    text: "must not dispatch",
  });
  const resumed = await controller.execute({
    action: "resume",
    run_id: "run-2",
    action_id: "resume-1",
    effect_id: "resume-effect",
  });
  const selectionReceipt = await selectTarget(controller, target, "resume");
  const observation = await controller.execute({
    action: "observe_window",
    run_id: "run-2",
    action_id: "observe-1",
    effect_id: "observe-effect",
    target,
    selection_receipt: selectionReceipt,
  });
  const allowed = await controller.execute({
    action: "type_text",
    run_id: "run-2",
    action_id: "write-2",
    effect_id: "write-effect-2",
    observation_id: observation.observation_id,
    selection_receipt: selectionReceipt,
    target,
    element_id: "0.1",
    text: "dispatch after fresh observation",
    verification_element_id: "0.1",
    expected_value: "after",
  });

  assert.equal(paused.data.session.status, "paused");
  assert.equal(blocked.ok, false);
  assert.equal(blocked.error_code, "paused");
  assert.equal(resumed.data.requires_fresh_observation, true);
  assert.equal(allowed.dispatch_status, "sent");
  assert.equal(calls.length, 3);
});

test("loopback bridge rejects requests without a capability", async () => {
  const { controller } = fixtureController();
  const baseUrl = await controller.startBridgeServer();
  try {
    const health = await fetch(`${baseUrl}/health`).then((response) =>
      response.json(),
    );
    const denied = await fetch(`${baseUrl}/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "status" }),
    });
    assert.equal(health.bridge, "mac_use");
    assert.equal(denied.status, 401);
  } finally {
    await controller.stopBridgeServer();
  }
});

test("resume uses a new cancellation epoch and cannot revive old actions", async () => {
  const { calls, controller, root } = fixtureController();
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const selectionReceipt = await selectTarget(controller, target, "epoch");
  const before = await controller.execute({
    action: "observe_window",
    run_id: "epoch-run",
    action_id: "epoch-observe-before",
    effect_id: "epoch-observe-before",
    target,
    selection_receipt: selectionReceipt,
  });
  await controller.execute({
    action: "type_text",
    run_id: "epoch-run",
    action_id: "epoch-write-before",
    effect_id: "epoch-write-before",
    target,
    selection_receipt: selectionReceipt,
    observation_id: before.observation_id,
    element_id: "0.1",
    verification_element_id: "0.1",
    expected_value: "after-one",
    text: "first",
  });
  const oldCancelPath = calls.at(-1).cancel_path;

  await controller.execute({
    action: "pause",
    run_id: "epoch-run",
    action_id: "epoch-pause",
    effect_id: "epoch-pause",
  });
  await controller.execute({
    action: "resume",
    run_id: "epoch-run",
    action_id: "epoch-resume",
    effect_id: "epoch-resume",
  });
  assert.equal(fs.existsSync(oldCancelPath), true);

  const after = await controller.execute({
    action: "observe_window",
    run_id: "epoch-run",
    action_id: "epoch-observe-after",
    effect_id: "epoch-observe-after",
    target,
    selection_receipt: selectionReceipt,
  });
  await controller.execute({
    action: "type_text",
    run_id: "epoch-run",
    action_id: "epoch-write-after",
    effect_id: "epoch-write-after",
    target,
    selection_receipt: selectionReceipt,
    observation_id: after.observation_id,
    element_id: "0.1",
    verification_element_id: "0.1",
    expected_value: "after-two",
    text: "second",
  });
  const newCancelPath = calls.at(-1).cancel_path;

  assert.notEqual(newCancelPath, oldCancelPath);
  assert.match(oldCancelPath, /\.epoch-0\.cancelled$/);
  assert.match(newCancelPath, /\.epoch-1\.cancelled$/);
  fs.rmSync(root, { recursive: true, force: true });
});

test("controller restart cannot inherit a stale cancellation generation", async () => {
  const sharedRoot = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mac-use-generation-test-"),
  );
  const sharedUserData = path.join(sharedRoot, "user-data");
  const first = fixtureController({ userDataPath: sharedUserData });
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const firstSelection = await selectTarget(
    first.controller,
    target,
    "first-generation",
  );
  const firstObservation = await first.controller.execute({
    action: "observe_window",
    run_id: "restart-run",
    action_id: "first-observe",
    effect_id: "first-observe",
    target,
    selection_receipt: firstSelection,
  });
  await first.controller.execute({
    action: "type_text",
    run_id: "restart-run",
    action_id: "first-write",
    effect_id: "first-write",
    target,
    selection_receipt: firstSelection,
    observation_id: firstObservation.observation_id,
    element_id: "0.1",
    verification_element_id: "0.1",
    expected_value: "first-after",
    text: "first",
  });
  const oldCancelPath = first.calls.at(-1).cancel_path;
  await first.controller.execute({
    action: "stop",
    run_id: "restart-run",
    action_id: "first-stop",
    effect_id: "first-stop",
  });
  assert.equal(fs.existsSync(oldCancelPath), true);

  const second = fixtureController({ userDataPath: sharedUserData });
  const secondSelection = await selectTarget(
    second.controller,
    target,
    "second-generation",
  );
  const secondObservation = await second.controller.execute({
    action: "observe_window",
    run_id: "restart-run",
    action_id: "second-observe",
    effect_id: "second-observe",
    target,
    selection_receipt: secondSelection,
  });
  const secondWrite = await second.controller.execute({
    action: "type_text",
    run_id: "restart-run",
    action_id: "second-write",
    effect_id: "second-write",
    target,
    selection_receipt: secondSelection,
    observation_id: secondObservation.observation_id,
    element_id: "0.1",
    verification_element_id: "0.1",
    expected_value: "second-after",
    text: "second",
  });
  const newCancelPath = second.calls.at(-1).cancel_path;

  assert.equal(secondWrite.dispatch_status, "sent");
  assert.notEqual(newCancelPath, oldCancelPath);
  assert.equal(fs.existsSync(oldCancelPath), true);
  assert.equal(fs.existsSync(newCancelPath), false);
  fs.rmSync(first.root, { recursive: true, force: true });
  fs.rmSync(second.root, { recursive: true, force: true });
  fs.rmSync(sharedRoot, { recursive: true, force: true });
});

test("stale capability nonce cannot replay a second action", async () => {
  const { controller } = fixtureController();
  const baseUrl = await controller.startBridgeServer();
  const token = controller.getBridgeToken();
  const body = JSON.stringify({
    protocol_version: 1,
    action: "list_windows",
    run_id: "run-http",
    action_id: "action-http",
    effect_id: "effect-http",
    target: {},
  });
  const expires = Date.now() + 5000;
  const nonce = "0123456789abcdef0123456789abcdef";
  const bodyHash = crypto.createHash("sha256").update(body).digest("hex");
  const signature = crypto
    .createHmac("sha256", token)
    .update(`v1\n${expires}\n${nonce}\n${bodyHash}`)
    .digest("hex");
  const capability = `v1:${expires}:${nonce}:${bodyHash}:${signature}`;
  const send = () =>
    fetch(`${baseUrl}/action`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Lantai-Mac-Use-Capability": capability,
      },
      body,
    });
  try {
    assert.equal((await send()).status, 200);
    assert.equal((await send()).status, 401);
  } finally {
    await controller.stopBridgeServer();
  }
});

test("Electron captures the exact selected window and records integrity", async () => {
  const thumbnail = {
    isEmpty: () => false,
    toPNG: () => Buffer.from("window pixels"),
  };
  const { controller, root } = fixtureController({
    desktopCapturer: {
      getSources: async () => [
        { id: "window:7:0", thumbnail },
        { id: "window:42:0", thumbnail },
      ],
    },
    getScreenRecordingStatus: () => "granted",
  });
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const selectionReceipt = await selectTarget(controller, target, "capture");

  const result = await controller.execute({
    action: "observe_window",
    run_id: "observe-run",
    action_id: "observe-action",
    effect_id: "observe-effect",
    target,
    selection_receipt: selectionReceipt,
  });

  assert.equal(result.ok, true);
  assert.equal(result.data.screen_recording, "granted");
  assert.ok(result.observation_id.startsWith("macobs_"));
  assert.equal(result.evidence[0].sha256.length, 64);
  assert.equal(
    fs.readFileSync(result.data.screenshot_path, "utf8"),
    "window pixels",
  );
  fs.rmSync(root, { recursive: true, force: true });
});

test("screen recording request stays in Electron permission ownership", async () => {
  let calls = 0;
  const { controller } = fixtureController({
    desktopCapturer: {
      getSources: async (options) => {
        calls += 1;
        assert.deepEqual(options.types, ["screen"]);
        return [];
      },
    },
    getScreenRecordingStatus: () => "granted",
  });

  const result = await controller.execute({
    action: "request_screen_recording",
    run_id: "permission-ui",
  });

  assert.equal(result.ok, true);
  assert.equal(result.data.screen_recording, "granted");
  assert.equal(calls, 1);
});

test("write requires a current observation bound to the same run and target", async () => {
  const { calls, controller } = fixtureController();
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const inventedSelection = await controller.execute({
    action: "observe_window",
    run_id: "bound-run",
    action_id: "observe-invented",
    effect_id: "observe-invented",
    selection_receipt: "macsel_invented",
    target,
  });
  const selectionReceipt = await selectTarget(controller, target, "bound");

  const missing = await controller.execute({
    action: "type_text",
    run_id: "bound-run",
    action_id: "write-missing",
    effect_id: "write-effect",
    target,
    selection_receipt: selectionReceipt,
    text: "do not send",
  });
  const observed = await controller.execute({
    action: "observe_window",
    run_id: "bound-run",
    action_id: "observe-bound",
    effect_id: "observe-effect",
    target,
    selection_receipt: selectionReceipt,
  });
  const wrongTarget = await controller.execute({
    action: "type_text",
    run_id: "bound-run",
    action_id: "write-wrong-target",
    effect_id: "write-effect-2",
    observation_id: observed.observation_id,
    selection_receipt: selectionReceipt,
    target: { ...target, window_id: "99" },
    text: "do not send",
  });
  const wrongGeneration = await controller.execute({
    action: "type_text",
    run_id: "bound-run",
    action_id: "write-wrong-generation",
    effect_id: "write-effect-generation",
    observation_id: observed.observation_id,
    selection_receipt: selectionReceipt,
    target: {
      ...target,
      process_started_at: target.process_started_at + 1,
    },
    text: "do not send",
  });
  const missingPredicate = await controller.execute({
    action: "type_text",
    run_id: "bound-run",
    action_id: "write-no-predicate",
    effect_id: "write-effect-predicate",
    observation_id: observed.observation_id,
    selection_receipt: selectionReceipt,
    target,
    element_id: "0.1",
    text: "do not send",
  });
  const alreadySatisfied = await controller.execute({
    action: "type_text",
    run_id: "bound-run",
    action_id: "write-old-predicate",
    effect_id: "write-effect-old-predicate",
    observation_id: observed.observation_id,
    selection_receipt: selectionReceipt,
    target,
    element_id: "0.1",
    text: "do not send",
    verification_element_id: "0.1",
    expected_value: "before",
  });
  const allowed = await controller.execute({
    action: "type_text",
    run_id: "bound-run",
    action_id: "write-bound",
    effect_id: "write-effect-3",
    observation_id: observed.observation_id,
    selection_receipt: selectionReceipt,
    target,
    element_id: "0.1",
    text: "send once",
    verification_element_id: "0.1",
    expected_value: "after",
  });

  assert.equal(missing.error_code, "stale_observation");
  assert.equal(inventedSelection.error_code, "target_selection_required");
  assert.equal(wrongTarget.error_code, "target_selection_required");
  assert.equal(wrongGeneration.error_code, "target_selection_required");
  assert.equal(missingPredicate.error_code, "verification_predicate_required");
  assert.equal(
    alreadySatisfied.error_code,
    "verification_precondition_already_satisfied",
  );
  assert.equal(allowed.dispatch_status, "sent");
  assert.equal(calls.length, 3);
  assert.equal(calls.at(-1).element_fingerprint.identifier, "fixture-body");
});

test("verification reports expected value match without repeating a write", async () => {
  const { calls, controller } = fixtureController();
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const selectionReceipt = await selectTarget(controller, target, "verify");
  const original = await controller.execute({
    action: "observe_window",
    run_id: "verify-run",
    action_id: "verify-original",
    effect_id: "verify-original-effect",
    target,
    selection_receipt: selectionReceipt,
  });
  const verified = await controller.execute({
    action: "observe_window",
    run_id: "verify-run",
    action_id: "verify-yes",
    effect_id: "verify-effect-1",
    target,
    selection_receipt: selectionReceipt,
    observation_id: original.observation_id,
    element_id: "0.1",
    expected_value: "fixture",
    resolves_effect_id: "write-effect-1",
    metadata: { verification: true },
  });
  const absent = await controller.execute({
    action: "observe_window",
    run_id: "verify-run",
    action_id: "verify-no",
    effect_id: "verify-effect-2",
    target,
    selection_receipt: selectionReceipt,
    observation_id: original.observation_id,
    element_id: "0.1",
    expected_value: "different",
    resolves_effect_id: "write-effect-2",
    metadata: { verification: true },
  });

  assert.equal(verified.effect_status, "verified");
  assert.equal(absent.effect_status, "no_effect_verified");
  assert.equal(absent.dispatch_status, "not_sent");
  assert.deepEqual(calls.at(-1).element_fingerprint, {
    element_id: "0.1",
    role: "AXTextArea",
    subrole: "",
    title: "Body",
    identifier: "fixture-body",
    enabled: true,
    sensitive: false,
    bounds: { x: 10, y: 20, width: 400, height: 300 },
  });
  assert.equal(calls.at(-1).element_role, "AXTextArea");
  assert.equal(calls.at(-1).element_title, "Body");
});

test("element writes use the observation fingerprint instead of model fields", async () => {
  const { calls, controller } = fixtureController();
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const selectionReceipt = await selectTarget(
    controller,
    target,
    "fingerprint",
  );
  const observed = await controller.execute({
    action: "observe_window",
    run_id: "fingerprint-run",
    action_id: "fingerprint-observe",
    effect_id: "fingerprint-observe-effect",
    target,
    selection_receipt: selectionReceipt,
  });
  const result = await controller.execute({
    action: "set_value",
    run_id: "fingerprint-run",
    action_id: "fingerprint-write",
    effect_id: "fingerprint-write-effect",
    target,
    selection_receipt: selectionReceipt,
    observation_id: observed.observation_id,
    element_id: "0.1",
    element_role: "AXButton",
    element_title: "forged",
    text: "after",
  });

  assert.equal(result.ok, true);
  assert.equal(calls.at(-1).element_role, "AXTextArea");
  assert.equal(calls.at(-1).element_title, "Body");
  assert.deepEqual(calls.at(-1).element_fingerprint, {
    element_id: "0.1",
    role: "AXTextArea",
    subrole: "",
    title: "Body",
    identifier: "fixture-body",
    enabled: true,
    sensitive: false,
    bounds: { x: 10, y: 20, width: 400, height: 300 },
  });
});

test("type_text rejects a non-text observation element before dispatch", async () => {
  const { calls, controller } = fixtureController();
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const selection = await selectTarget(
    controller,
    target,
    "invalid-text-target",
  );
  const observation = await controller.execute({
    action: "observe_window",
    run_id: "invalid-text-target",
    action_id: "invalid-text-observe",
    effect_id: "invalid-text-observe",
    target,
    selection_receipt: selection,
  });
  const result = await controller.execute({
    action: "type_text",
    run_id: "invalid-text-target",
    action_id: "invalid-text-write",
    effect_id: "invalid-text-write",
    target,
    selection_receipt: selection,
    observation_id: observation.observation_id,
    element_id: "0.2",
    verification_element_id: "0.1",
    expected_value: "after",
    text: "must not dispatch",
  });

  assert.equal(result.error_code, "invalid_text_target");
  assert.equal(result.dispatch_status, "not_sent");
  assert.equal(calls.length, 2);
});

test("password subroles reject text and value writes before dispatch", async () => {
  const { calls, controller } = fixtureController();
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
  };
  const selection = await selectTarget(controller, target, "sensitive-target");
  const observation = await controller.execute({
    action: "observe_window",
    run_id: "sensitive-target",
    action_id: "sensitive-observe",
    effect_id: "sensitive-observe",
    target,
    selection_receipt: selection,
  });
  const common = {
    run_id: "sensitive-target",
    target,
    selection_receipt: selection,
    observation_id: observation.observation_id,
    element_id: "0.3",
    text: "must not dispatch",
  };
  const typed = await controller.execute({
    ...common,
    action: "type_text",
    action_id: "sensitive-type",
    effect_id: "sensitive-type",
    verification_element_id: "0.1",
    expected_value: "after",
  });
  const set = await controller.execute({
    ...common,
    action: "set_value",
    action_id: "sensitive-set",
    effect_id: "sensitive-set",
  });

  assert.equal(typed.error_code, "sensitive_target");
  assert.equal(set.error_code, "sensitive_target");
  assert.equal(calls.length, 2);
});

test("evidence cleanup removes only expired controller-owned screenshots", () => {
  const { controller, root } = fixtureController();
  const evidenceRoot = path.join(root, "user-data", "mac-use", "evidence");
  fs.mkdirSync(evidenceRoot, { recursive: true });
  const expired = path.join(evidenceRoot, "window-1-0123456789abcdef.png");
  const current = path.join(evidenceRoot, "window-2-fedcba9876543210.png");
  const unrelated = path.join(evidenceRoot, "keep-me.txt");
  fs.writeFileSync(expired, "old");
  fs.writeFileSync(current, "new");
  fs.writeFileSync(unrelated, "user-like file");
  const old = new Date(Date.now() - 25 * 60 * 60 * 1000);
  fs.utimesSync(expired, old, old);

  controller.cleanupExpiredEvidence();

  assert.equal(fs.existsSync(expired), false);
  assert.equal(fs.existsSync(current), true);
  assert.equal(fs.existsSync(unrelated), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test("only a trusted desktop renderer can mint a target selection receipt", async () => {
  const handlers = new Map();
  const ipcMain = {
    handle: (channel, handler) => handlers.set(channel, handler),
  };
  const trusted = fixtureController({
    ipcMain,
    isTrustedIpcEvent: (event) => event?.trusted === true,
  });
  trusted.controller.installIpcHandlers();
  const select = handlers.get("lantai:mac-use-select-target");
  const target = {
    app_bundle_id: "com.apple.TextEdit",
    window_id: "42",
    process_id: 101,
    process_started_at: 1788700000.125,
    window_title: "fixture.txt",
  };

  const denied = await select({ trusted: false }, target);
  const allowed = await select({ trusted: true }, target);
  const protectedBundles = [
    "com.apple.Passwords",
    "COM.APPLE.PASSWORDS",
    "com.apple.SecurityAgent",
    "COM.APPLE.SECURITYAGENT",
    "com.apple.keychainaccess",
    "com.apple.systempreferences",
    "com.apple.loginwindow",
    "com.lantai.app",
    "com.lantai.app.future-helper",
  ];
  const protectedTargets = await Promise.all(
    protectedBundles.map((app_bundle_id) =>
      select({ trusted: true }, { ...target, app_bundle_id }),
    ),
  );

  assert.equal(denied.error_code, "untrusted_ipc_sender");
  assert.match(allowed.data.selection_receipt, /^macsel_[0-9a-f]{48}$/);
  assert.deepEqual(
    protectedTargets.map((result) => result.error_code),
    protectedBundles.map(() => "protected_target"),
  );
});

test("protected desktop targets reject writes before observation or input", async () => {
  const { calls, controller } = fixtureController();

  const result = await controller.execute({
    action: "press_key",
    run_id: "protected-run",
    action_id: "protected-action",
    effect_id: "protected-effect",
    target: {
      app_bundle_id: "com.apple.keychainaccess",
      window_id: "8",
      process_id: 108,
      process_started_at: 1788700001.5,
    },
    key: "enter",
  });

  assert.equal(result.error_code, "protected_target");
  assert.equal(result.dispatch_status, "not_sent");
  assert.equal(calls.length, 0);
});
