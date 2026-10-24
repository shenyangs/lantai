const childProcess = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");

const BRIDGE_HOST = "127.0.0.1";
const MAX_REQUEST_BYTES = 512 * 1024;
const MAX_COMPLETED_ACTIONS = 1000;
const MAX_USED_NONCES = 2048;
const ACTIONS = new Set([
  "status",
  "list_windows",
  "select_target",
  "observe_window",
  "focus_window",
  "click_at",
  "type_text",
  "press_key",
  "set_value",
  "press_element",
  "stop",
  "pause",
  "resume",
  "session_status",
  "request_accessibility",
  "request_screen_recording",
]);
const WRITE_ACTIONS = new Set([
  "focus_window",
  "click_at",
  "type_text",
  "press_key",
  "set_value",
  "press_element",
]);
const AMBIGUOUS_WRITE_ACTIONS = new Set([
  "click_at",
  "type_text",
  "press_key",
  "press_element",
]);
const PROTECTED_WRITE_BUNDLES = new Set([
  "com.lantai.app",
  "com.lantai.app.mac-use-helper",
  "com.apple.passwords",
  "com.apple.securityagent",
  "com.apple.keychainaccess",
  "com.apple.loginwindow",
  "com.apple.systempreferences",
]);

function isProtectedBundle(bundleId) {
  const normalized = String(bundleId || "")
    .trim()
    .toLowerCase();
  return (
    normalized.startsWith("com.lantai.app") ||
    PROTECTED_WRITE_BUNDLES.has(normalized)
  );
}

function resolveMacUseHelperPath({ isDev, electronDir, resourcesPath }) {
  const root = isDev
    ? path.resolve(electronDir, "..", "electron-dist", "mac-use")
    : path.join(resourcesPath, "mac-use");
  return path.join(
    root,
    "Lantai Mac Use Helper.app",
    "Contents",
    "MacOS",
    "lantai-mac-use-helper",
  );
}

function safeIdentifier(value, fallback = "run") {
  const normalized = String(value || "").trim();
  return /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(normalized)
    ? normalized
    : fallback;
}

function identity(payload = {}) {
  return {
    protocol_version: 1,
    action: String(payload.action || "status"),
    run_id: String(payload.run_id || ""),
    action_id: String(payload.action_id || ""),
    effect_id: String(payload.effect_id || ""),
    policy_status: "allowed",
  };
}

function failure(
  payload,
  errorCode,
  error,
  {
    dispatchStatus = "not_sent",
    effectStatus = "failed",
    retrySafe = true,
  } = {},
) {
  return {
    ...identity(payload),
    ok: false,
    dispatch_status: dispatchStatus,
    effect_status: effectStatus,
    error_code: errorCode,
    error,
    data: {},
    evidence: [],
    retry_safe: retrySafe,
  };
}

function runHelperProcess({ helperPath, payload, timeout, spawnImpl }) {
  return new Promise((resolve) => {
    let stdout = Buffer.alloc(0);
    let stderr = Buffer.alloc(0);
    let settled = false;
    let timer = null;
    const child = spawnImpl(helperPath, [], {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    });
    const finish = (response, error = null) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve({ response, error });
    };
    const append = (current, chunk) => {
      const next = Buffer.concat([current, Buffer.from(chunk)]);
      if (next.length > 1024 * 1024) {
        child.kill("SIGTERM");
        finish(null, new Error("mac_use_helper_output_too_large"));
      }
      return next;
    };
    child.stdout?.on("data", (chunk) => {
      stdout = append(stdout, chunk);
    });
    child.stderr?.on("data", (chunk) => {
      stderr = append(stderr, chunk);
    });
    child.stdin?.once("error", (error) => finish(null, error));
    child.once("error", (error) => finish(null, error));
    child.once("close", () => {
      let response = null;
      try {
        response = JSON.parse(stdout.toString("utf8").trim());
      } catch {
        // The caller converts missing or malformed output to a typed failure.
      }
      const error = response
        ? null
        : new Error(
            stderr.toString("utf8").trim() || "mac_use_helper_invalid_response",
          );
      finish(response, error);
    });
    timer = setTimeout(() => {
      const error = new Error("mac_use_helper_timeout");
      error.killed = true;
      child.kill("SIGTERM");
      finish(null, error);
    }, timeout);
    child.stdin?.end(JSON.stringify(payload));
  });
}

function createMacUseController({
  ipcMain = null,
  isDev = false,
  electronDir = __dirname,
  resourcesPath = process.resourcesPath || __dirname,
  platform = process.platform,
  userDataPath = path.join(os.tmpdir(), "lantai-mac-use"),
  fsImpl = fs,
  httpImpl = http,
  spawnImpl = childProcess.spawn,
  runHelperProcessImpl = null,
  desktopCapturer = null,
  getScreenRecordingStatus = () => "unknown",
  randomToken = () => crypto.randomBytes(32).toString("hex"),
  isTrustedIpcEvent = () => false,
  appendLog = () => {},
} = {}) {
  const helperPath = resolveMacUseHelperPath({
    isDev,
    electronDir,
    resourcesPath,
  });
  const storageRoot = path.join(userDataPath, "mac-use");
  const evidenceRoot = path.join(storageRoot, "evidence");
  const cancellationRoot = path.join(storageRoot, "cancellations");
  const inputLockPath = path.join(storageRoot, "shared-input.lock");
  const syntheticTrailPath = path.join(storageRoot, "synthetic-input.json");
  const controllerGeneration = crypto.randomBytes(12).toString("hex");
  const bridgeToken = randomToken();
  const completedActions = new Map();
  const pendingActions = new Map();
  const observations = new Map();
  const targetSelections = new Map();
  const sessions = new Map();
  const usedNonces = new Map();
  let bridgeServer = null;
  let bridgeBaseUrl = "";
  let shuttingDown = false;

  function ensureStorage() {
    for (const directory of [storageRoot, evidenceRoot, cancellationRoot]) {
      fsImpl.mkdirSync(directory, { recursive: true, mode: 0o700 });
      try {
        fsImpl.chmodSync(directory, 0o700);
      } catch {
        // The directory may live on a filesystem without POSIX modes.
      }
    }
  }

  function cleanupExpiredEvidence(maxAgeMs = 24 * 60 * 60 * 1000) {
    ensureStorage();
    const cutoff = Date.now() - maxAgeMs;
    for (const name of fsImpl.readdirSync(evidenceRoot)) {
      if (!/^window-\d+-[0-9a-f]{16}\.png$/.test(name)) continue;
      const filePath = path.join(evidenceRoot, name);
      let stat;
      try {
        stat = fsImpl.lstatSync(filePath);
      } catch {
        continue;
      }
      if (stat.isFile() && !stat.isSymbolicLink() && stat.mtimeMs < cutoff) {
        try {
          fsImpl.unlinkSync(filePath);
        } catch {
          // Cleanup is best effort; a current task may still hold the file.
        }
      }
    }
  }

  function cancelPath(runId, revocationEpoch) {
    return path.join(
      cancellationRoot,
      `${safeIdentifier(runId)}.${controllerGeneration}.epoch-${Math.max(0, Number(revocationEpoch) || 0)}.cancelled`,
    );
  }

  function sessionState(runId) {
    const id = safeIdentifier(runId);
    let state = sessions.get(id);
    if (!state) {
      state = {
        run_id: id,
        status: "ready",
        revocation_epoch: 0,
        updated_at: new Date().toISOString(),
      };
      sessions.set(id, state);
    }
    return state;
  }

  function setSessionState(runId, status) {
    const previous = sessionState(runId);
    const next = {
      ...previous,
      status,
      revocation_epoch:
        status === "paused" || status === "stopped"
          ? previous.revocation_epoch + 1
          : previous.revocation_epoch,
      updated_at: new Date().toISOString(),
    };
    sessions.set(next.run_id, next);
    return next;
  }

  function sessionResult(payload, status, message) {
    const state = sessionState(payload.run_id);
    return {
      ...identity(payload),
      ok: true,
      dispatch_status: "not_sent",
      effect_status: "verified",
      message,
      data: { session: { ...state, status } },
      evidence: [],
      retry_safe: true,
    };
  }

  async function controlSession(payload) {
    ensureStorage();
    if (payload.action === "session_status") {
      const state = sessionState(payload.run_id);
      return sessionResult(
        payload,
        state.status,
        "Mac Use session status inspected.",
      );
    }
    if (payload.action === "stop" || payload.action === "pause") {
      const status = payload.action === "stop" ? "stopped" : "paused";
      const normalizedRunId = safeIdentifier(payload.run_id);
      const previous = sessionState(normalizedRunId);
      const wasActive = previous.status !== "stopped";
      const marker = cancelPath(payload.run_id, previous.revocation_epoch);
      await fsImpl.promises.writeFile(marker, `${status}\n`, { mode: 0o600 });
      const state = setSessionState(payload.run_id, status);
      return {
        ...sessionResult(
          payload,
          status,
          status === "stopped"
            ? "Mac Use stopped; queued actions have been revoked."
            : "Mac Use paused; queued actions have been revoked.",
        ),
        data: { session: { ...state, was_active: wasActive } },
      };
    }
    if (payload.action === "resume") {
      const state = setSessionState(payload.run_id, "ready");
      return {
        ...sessionResult(
          payload,
          "ready",
          "Mac Use can resume after a fresh target observation.",
        ),
        data: { session: state, requires_fresh_observation: true },
      };
    }
    return failure(
      payload,
      "mac_use_session_action_unsupported",
      "Unsupported Mac Use session action.",
    );
  }

  function rememberCompleted(actionId, result) {
    completedActions.set(actionId, result);
    while (completedActions.size > MAX_COMPLETED_ACTIONS) {
      completedActions.delete(completedActions.keys().next().value);
    }
  }

  function sameTarget(left = {}, right = {}) {
    return (
      String(left.app_bundle_id || "") === String(right.app_bundle_id || "") &&
      String(left.window_id || "") === String(right.window_id || "") &&
      Number(left.process_id || 0) === Number(right.process_id || 0) &&
      Number(left.process_id || 0) > 0 &&
      Number(left.process_started_at || 0) > 0 &&
      Math.abs(
        Number(left.process_started_at || 0) -
          Number(right.process_started_at || 0),
      ) < 0.001
    );
  }

  function bindTargetSelection(payload, result) {
    if (payload.action !== "select_target" || !result?.ok) return result;
    const selectionReceipt = `macsel_${crypto.randomBytes(24).toString("hex")}`;
    const target = result.data?.target || payload.target || {};
    targetSelections.set(selectionReceipt, {
      target,
      elements: Array.isArray(result.data?.elements)
        ? result.data.elements
        : [],
      created_at_ms: Date.now(),
    });
    while (targetSelections.size > 1000) {
      targetSelections.delete(targetSelections.keys().next().value);
    }
    return {
      ...result,
      data: { ...result.data, selection_receipt: selectionReceipt },
    };
  }

  function validateTargetSelection(payload) {
    if (
      !WRITE_ACTIONS.has(payload.action) &&
      payload.action !== "observe_window"
    ) {
      return { ok: true };
    }
    const receipt = targetSelections.get(
      String(payload.selection_receipt || ""),
    );
    if (
      !receipt ||
      Date.now() - receipt.created_at_ms > 8 * 60 * 60 * 1000 ||
      !sameTarget(receipt.target, payload.target)
    ) {
      return {
        ok: false,
        result: failure(
          payload,
          "target_selection_required",
          "Select this exact application window in the Mac Use picker before reading or writing it.",
        ),
      };
    }
    return { ok: true };
  }

  function bindObservation(payload, result) {
    if (payload.action !== "observe_window" || !result?.ok) return result;
    const observationId =
      result.observation_id ||
      `macobs_${crypto.randomBytes(12).toString("hex")}`;
    const state = sessionState(payload.run_id);
    const target = result.data?.target || payload.target || {};
    observations.set(observationId, {
      observation_id: observationId,
      run_id: payload.run_id,
      target,
      elements: Array.isArray(result.data?.elements)
        ? result.data.elements
        : [],
      revocation_epoch: state.revocation_epoch,
      created_at_ms: Date.now(),
    });
    while (observations.size > 2000) {
      observations.delete(observations.keys().next().value);
    }
    return { ...result, observation_id: observationId };
  }

  function fingerprintForObservedElement(receipt, elementId) {
    const element = receipt?.elements?.find(
      (candidate) =>
        String(candidate?.element_id || "") === String(elementId || ""),
    );
    const bounds = element?.bounds;
    if (
      !element ||
      !String(element.role || "") ||
      !bounds ||
      ![bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)
    ) {
      return null;
    }
    return {
      element_id: String(element.element_id),
      role: String(element.role),
      subrole: String(element.subrole || ""),
      title: String(element.title || ""),
      identifier: String(element.identifier || ""),
      enabled: element.enabled !== false,
      sensitive: element.sensitive === true,
      bounds: {
        x: Number(bounds.x),
        y: Number(bounds.y),
        width: Number(bounds.width),
        height: Number(bounds.height),
      },
    };
  }

  function currentObservationReceipt(payload) {
    const receipt = observations.get(String(payload.observation_id || ""));
    const state = sessionState(payload.run_id);
    if (
      !receipt ||
      receipt.run_id !== payload.run_id ||
      receipt.revocation_epoch !== state.revocation_epoch ||
      Date.now() - receipt.created_at_ms > 120_000 ||
      !sameTarget(receipt.target, payload.target)
    ) {
      return null;
    }
    return receipt;
  }

  function bindWriteObservation(payload) {
    if (!WRITE_ACTIONS.has(payload.action)) return { ok: true, payload };
    const receipt = currentObservationReceipt(payload);
    if (!receipt) {
      return {
        ok: false,
        result: failure(
          payload,
          "stale_observation",
          "Mac Use requires a fresh observation of this exact target before writing.",
        ),
      };
    }
    let elementFingerprint = null;
    if (["type_text", "set_value", "press_element"].includes(payload.action)) {
      elementFingerprint = fingerprintForObservedElement(
        receipt,
        payload.element_id,
      );
      if (!elementFingerprint) {
        return {
          ok: false,
          result: failure(
            payload,
            "stale_observation",
            "The action element is missing from the bound observation.",
          ),
        };
      }
      if (elementFingerprint.sensitive) {
        return {
          ok: false,
          result: failure(
            payload,
            "sensitive_target",
            "Mac Use does not write to password or protected-content controls.",
          ),
        };
      }
      if (
        payload.action === "type_text" &&
        (!new Set(["AXTextArea", "AXTextField"]).has(elementFingerprint.role) ||
          !elementFingerprint.enabled ||
          elementFingerprint.sensitive)
      ) {
        return {
          ok: false,
          result: failure(
            payload,
            "invalid_text_target",
            "Text input requires an enabled, non-sensitive observed text control.",
          ),
        };
      }
    }
    if (AMBIGUOUS_WRITE_ACTIONS.has(payload.action)) {
      const verificationElementId = String(
        payload.verification_element_id || "",
      );
      const element = receipt.elements.find(
        (candidate) =>
          String(candidate?.element_id || "") === verificationElementId,
      );
      const verificationFingerprint = fingerprintForObservedElement(
        receipt,
        verificationElementId,
      );
      if (!verificationElementId || !element || !verificationFingerprint) {
        return {
          ok: false,
          result: failure(
            payload,
            "verification_predicate_required",
            "This GUI action requires a verification element from the bound observation.",
          ),
        };
      }
      if (
        String(element.value ?? "") === String(payload.expected_value ?? "")
      ) {
        return {
          ok: false,
          result: failure(
            payload,
            "verification_precondition_already_satisfied",
            "The requested verification state was already true before this action.",
          ),
        };
      }
    }
    return {
      ok: true,
      payload: {
        ...payload,
        observation_target: receipt.target,
        ...(elementFingerprint
          ? {
              element_role: elementFingerprint.role,
              element_title: elementFingerprint.title,
              element_fingerprint: elementFingerprint,
            }
          : {}),
      },
    };
  }

  function bindVerificationObservation(payload) {
    if (
      payload.action !== "observe_window" ||
      payload.metadata?.verification !== true
    ) {
      return { ok: true, payload };
    }
    const receipt = currentObservationReceipt(payload);
    const elementFingerprint = fingerprintForObservedElement(
      receipt,
      payload.element_id,
    );
    if (
      !receipt ||
      !elementFingerprint ||
      !String(payload.resolves_effect_id || "")
    ) {
      return {
        ok: false,
        result: failure(
          payload,
          "stale_observation",
          "Verification requires the original observation and exact observed control.",
        ),
      };
    }
    return {
      ok: true,
      payload: {
        ...payload,
        observation_target: receipt.target,
        element_role: elementFingerprint.role,
        element_title: elementFingerprint.title,
        element_fingerprint: elementFingerprint,
      },
    };
  }

  function normalizeHelperResponse(parsed, payload) {
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return failure(
        payload,
        "mac_use_helper_invalid_response",
        "Mac Use helper did not return a valid response.",
        {
          dispatchStatus: WRITE_ACTIONS.has(payload.action)
            ? "unknown"
            : "transport_failed",
          effectStatus: WRITE_ACTIONS.has(payload.action)
            ? "ambiguous"
            : "failed",
          retrySafe: !WRITE_ACTIONS.has(payload.action),
        },
      );
    }
    return { ...identity(payload), ...parsed };
  }

  async function addEvidenceIntegrity(result) {
    const screenshotPath = result?.data?.screenshot_path;
    if (!screenshotPath || !fsImpl.existsSync(screenshotPath)) return result;
    const bytes = await fsImpl.promises.readFile(screenshotPath);
    const stat = await fsImpl.promises.stat(screenshotPath);
    const evidence = Array.isArray(result.evidence) ? [...result.evidence] : [];
    const existing = evidence.find(
      (entry) => entry && entry.kind === "window_screenshot",
    );
    const receipt = {
      kind: "window_screenshot",
      path: screenshotPath,
      size_bytes: stat.size,
      sha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    };
    if (existing) Object.assign(existing, receipt);
    else evidence.push(receipt);
    return {
      ...result,
      observation_id:
        result.observation_id ||
        `macobs_${crypto.randomBytes(12).toString("hex")}`,
      evidence,
    };
  }

  function applyDesktopPermissionStatus(result) {
    if (!result?.data || typeof result.data !== "object") return result;
    let status = "unknown";
    try {
      const nativeStatus = getScreenRecordingStatus();
      status = nativeStatus === "granted" ? "granted" : "unknown";
    } catch {
      status = "unknown";
    }
    return { ...result, data: { ...result.data, screen_recording: status } };
  }

  function filterWindowCatalog(payload, result) {
    if (
      payload.action !== "list_windows" ||
      !Array.isArray(result?.data?.windows)
    ) {
      return result;
    }
    return {
      ...result,
      data: {
        ...result.data,
        windows: result.data.windows.filter(
          (entry) => !isProtectedBundle(entry?.app_bundle_id),
        ),
      },
    };
  }

  function applyVerificationResult(payload, result) {
    if (
      payload.metadata?.verification !== true ||
      payload.action !== "observe_window"
    ) {
      return result;
    }
    const matches = result?.data?.expected_value_matches;
    if (matches === true) {
      return {
        ...result,
        ok: true,
        effect_status: "verified",
        message: "The selected control matches the expected value.",
        retry_safe: true,
      };
    }
    if (matches === false) {
      return {
        ...result,
        ok: true,
        effect_status: "no_effect_verified",
        message: "The selected control does not match the expected value.",
        retry_safe: true,
      };
    }
    return {
      ...result,
      ok: false,
      effect_status: "failed",
      error_code: "verification_unavailable",
      error: "The selected control could not be read back for verification.",
      retry_safe: false,
    };
  }

  async function captureObservation(payload, result) {
    if (
      payload.action !== "observe_window" ||
      !result?.ok ||
      !desktopCapturer?.getSources
    ) {
      return result;
    }
    const windowId = String(payload.target?.window_id || "");
    try {
      const sources = await desktopCapturer.getSources({
        types: ["window"],
        thumbnailSize: { width: 1600, height: 1200 },
        fetchWindowIcons: false,
      });
      const source = sources.find(
        (candidate) => String(candidate.id || "").split(":")[1] === windowId,
      );
      if (!source || source.thumbnail?.isEmpty?.()) {
        return {
          ...result,
          data: {
            ...result.data,
            screenshot_error: "capture_source_unavailable",
          },
        };
      }
      const screenshotPath = path.join(
        evidenceRoot,
        `window-${Date.now()}-${crypto.randomBytes(8).toString("hex")}.png`,
      );
      await fsImpl.promises.writeFile(
        screenshotPath,
        source.thumbnail.toPNG(),
        {
          mode: 0o600,
        },
      );
      return {
        ...result,
        data: { ...result.data, screenshot_path: screenshotPath },
        evidence: [
          ...(Array.isArray(result.evidence) ? result.evidence : []),
          { kind: "window_screenshot", path: screenshotPath },
        ],
      };
    } catch {
      return {
        ...result,
        data: { ...result.data, screenshot_error: "capture_failed" },
      };
    }
  }

  async function requestScreenRecording(payload) {
    if (platform !== "darwin" || !desktopCapturer?.getSources) {
      return failure(
        payload,
        "screen_recording_request_unavailable",
        "Screen Recording must be enabled from macOS System Settings.",
      );
    }
    try {
      await desktopCapturer.getSources({
        types: ["screen"],
        thumbnailSize: { width: 1, height: 1 },
        fetchWindowIcons: false,
      });
      return {
        ...identity(payload),
        ok: true,
        dispatch_status: "not_sent",
        effect_status: "not_applicable",
        message: "macOS Screen Recording permission request was evaluated.",
        data: { screen_recording: getScreenRecordingStatus() },
        evidence: [],
        retry_safe: true,
      };
    } catch {
      return failure(
        payload,
        "screen_recording_request_failed",
        "macOS did not make Screen Recording available.",
      );
    }
  }

  async function runHelper(payload) {
    if (shuttingDown) {
      return failure(
        payload,
        "mac_use_shutting_down",
        "Mac Use is stopping with the desktop application.",
      );
    }
    if (platform !== "darwin") {
      return failure(
        payload,
        "mac_use_unsupported_platform",
        "Mac Use is available only on macOS.",
      );
    }
    if (!fsImpl.existsSync(helperPath)) {
      return failure(
        payload,
        "mac_use_helper_missing",
        "The signed Mac Use helper is not installed.",
      );
    }
    ensureStorage();
    if (payload.action === "observe_window") cleanupExpiredEvidence();
    const state = sessionState(payload.run_id);
    if (WRITE_ACTIONS.has(payload.action) && state.status !== "ready") {
      return failure(
        payload,
        state.status === "stopped" ? "cancelled" : "paused",
        `Mac Use session is ${state.status}; no input was sent.`,
      );
    }

    const helperPayload = {
      ...payload,
      cancel_path: cancelPath(payload.run_id, state.revocation_epoch),
      input_lock_path: inputLockPath,
      synthetic_trail_path: syntheticTrailPath,
      required_idle_seconds: 2.0,
      allowed_output_root: evidenceRoot,
    };
    if (payload.action === "observe_window" && !desktopCapturer?.getSources) {
      helperPayload.output_path = path.join(
        evidenceRoot,
        `window-${Date.now()}-${crypto.randomBytes(8).toString("hex")}.png`,
      );
    }
    const timeout = payload.action === "observe_window" ? 30_000 : 15_000;
    const launched = runHelperProcessImpl
      ? await runHelperProcessImpl(helperPayload, timeout)
      : await runHelperProcess({
          helperPath,
          payload: helperPayload,
          timeout,
          spawnImpl,
        });
    let result = normalizeHelperResponse(launched?.response, payload);
    if (launched?.error) {
      result = failure(
        payload,
        launched.error.killed
          ? "mac_use_helper_timeout"
          : "mac_use_helper_launch_failed",
        launched.error.killed
          ? "Mac Use helper timed out. The external effect may be unknown."
          : "Mac Use helper could not be launched.",
        {
          dispatchStatus: WRITE_ACTIONS.has(payload.action)
            ? "unknown"
            : "transport_failed",
          effectStatus: WRITE_ACTIONS.has(payload.action)
            ? "ambiguous"
            : "failed",
          retrySafe: !WRITE_ACTIONS.has(payload.action),
        },
      );
    }
    result = applyDesktopPermissionStatus(result);
    result = filterWindowCatalog(payload, result);
    result = applyVerificationResult(payload, result);
    result = await captureObservation(payload, result);
    result = await addEvidenceIntegrity(result);
    result = bindObservation(payload, result);
    result = bindTargetSelection(payload, result);
    if (result.error_code) {
      appendLog(
        `[Mac Use] ${JSON.stringify({
          action: payload.action,
          error_code: result.error_code,
          dispatch_status: result.dispatch_status,
          effect_status: result.effect_status,
        })}`,
      );
    }
    return result;
  }

  async function execute(payload = {}) {
    const action = String(payload.action || "").trim();
    if (!ACTIONS.has(action)) {
      return failure(
        { ...payload, action: action || "status" },
        "mac_use_action_unsupported",
        "Unsupported Mac Use action.",
      );
    }
    const normalized = {
      ...payload,
      protocol_version: 1,
      action,
      run_id: safeIdentifier(payload.run_id),
      action_id: safeIdentifier(payload.action_id, crypto.randomUUID()),
      effect_id: safeIdentifier(payload.effect_id, crypto.randomUUID()),
    };
    if (["stop", "pause", "resume", "session_status"].includes(action)) {
      return controlSession(normalized);
    }
    if (action === "request_screen_recording") {
      return requestScreenRecording(normalized);
    }
    if (completedActions.has(normalized.action_id)) {
      return completedActions.get(normalized.action_id);
    }
    if (pendingActions.has(normalized.action_id)) {
      return pendingActions.get(normalized.action_id);
    }
    const currentSession = sessionState(normalized.run_id);
    if (WRITE_ACTIONS.has(action) && currentSession.status !== "ready") {
      return failure(
        normalized,
        currentSession.status === "stopped" ? "cancelled" : "paused",
        `Mac Use session is ${currentSession.status}; no input was sent.`,
      );
    }
    if (
      WRITE_ACTIONS.has(action) &&
      isProtectedBundle(normalized.target?.app_bundle_id)
    ) {
      return failure(
        normalized,
        "protected_target",
        "Mac Use does not write into Lantai, permission surfaces, passwords, Keychain, or login windows.",
      );
    }
    if (
      action === "select_target" &&
      isProtectedBundle(normalized.target?.app_bundle_id)
    ) {
      return failure(
        normalized,
        "protected_target",
        "Mac Use does not select Lantai, permission surfaces, passwords, Keychain, or login windows.",
      );
    }
    const targetSelection = validateTargetSelection(normalized);
    if (!targetSelection.ok) return targetSelection.result;
    const observationBinding = bindWriteObservation(normalized);
    if (!observationBinding.ok) return observationBinding.result;
    const verificationBinding = bindVerificationObservation(
      observationBinding.payload,
    );
    if (!verificationBinding.ok) return verificationBinding.result;
    const dispatchPayload = verificationBinding.payload;
    const pending = runHelper(dispatchPayload)
      .then((result) => {
        rememberCompleted(normalized.action_id, result);
        return result;
      })
      .finally(() => pendingActions.delete(normalized.action_id));
    pendingActions.set(normalized.action_id, pending);
    return pending;
  }

  function readBody(request) {
    return new Promise((resolve, reject) => {
      const chunks = [];
      let total = 0;
      request.on("data", (chunk) => {
        total += chunk.length;
        if (total > MAX_REQUEST_BYTES) {
          reject(new Error("request_too_large"));
          request.destroy();
          return;
        }
        chunks.push(chunk);
      });
      request.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
      request.on("error", reject);
    });
  }

  function sendJson(response, status, payload) {
    const body = Buffer.from(JSON.stringify(payload));
    response.writeHead(status, {
      "Content-Type": "application/json; charset=utf-8",
      "Content-Length": body.length,
      "Cache-Control": "no-store",
    });
    response.end(body);
  }

  function pruneNonces(now = Date.now()) {
    for (const [nonce, expiresAt] of usedNonces) {
      if (expiresAt <= now || usedNonces.size > MAX_USED_NONCES) {
        usedNonces.delete(nonce);
      }
    }
  }

  function timingSafeHexEqual(left, right) {
    if (!/^[0-9a-f]+$/i.test(left) || !/^[0-9a-f]+$/i.test(right)) return false;
    const a = Buffer.from(left, "hex");
    const b = Buffer.from(right, "hex");
    return a.length === b.length && crypto.timingSafeEqual(a, b);
  }

  function isAuthorizedCapability(header, rawBody) {
    const parts = String(header || "").split(":");
    if (parts.length !== 5 || parts[0] !== "v1") return false;
    const [, expiresAtRaw, nonce, bodyHash, signature] = parts;
    const expiresAt = Number(expiresAtRaw);
    const now = Date.now();
    pruneNonces(now);
    if (!nonce || usedNonces.has(nonce) || !Number.isFinite(expiresAt))
      return false;
    if (expiresAt <= now || expiresAt - now > 15_000) return false;
    const actualBodyHash = crypto
      .createHash("sha256")
      .update(rawBody)
      .digest("hex");
    if (!timingSafeHexEqual(bodyHash, actualBodyHash)) return false;
    const message = Buffer.from(`v1\n${expiresAtRaw}\n${nonce}\n${bodyHash}`);
    const expected = crypto
      .createHmac("sha256", bridgeToken)
      .update(message)
      .digest("hex");
    if (!timingSafeHexEqual(signature, expected)) return false;
    usedNonces.set(nonce, expiresAt);
    return true;
  }

  async function handleHttp(request, response) {
    try {
      const url = new URL(request.url || "/", `http://${BRIDGE_HOST}`);
      if (request.method === "GET" && url.pathname === "/health") {
        sendJson(response, 200, {
          ok: true,
          bridge: "mac_use",
          protocol_version: 1,
        });
        return;
      }
      if (request.method === "GET" && url.pathname === "/state") {
        if (request.headers["x-lantai-mac-use-token"] !== bridgeToken) {
          sendJson(response, 401, {
            ok: false,
            error_code: "mac_use_unauthorized",
          });
          return;
        }
        sendJson(response, 200, {
          ok: true,
          bridge: "mac_use",
          protocol_version: 1,
          helper_installed:
            platform === "darwin" && fsImpl.existsSync(helperPath),
          pending_action_count: pendingActions.size,
          sessions: [...sessions.values()],
        });
        return;
      }
      if (request.method === "POST" && url.pathname === "/action") {
        const raw = await readBody(request);
        if (
          !isAuthorizedCapability(
            request.headers["x-lantai-mac-use-capability"],
            raw,
          )
        ) {
          sendJson(response, 401, {
            ok: false,
            error_code: "mac_use_unauthorized",
            error: "Unauthorized Mac Use bridge request.",
          });
          return;
        }
        const payload = raw ? JSON.parse(raw) : {};
        const result = await execute(payload);
        sendJson(response, result.ok === false ? 400 : 200, result);
        return;
      }
      sendJson(response, 404, {
        ok: false,
        error_code: "mac_use_endpoint_not_found",
      });
    } catch (error) {
      sendJson(response, 500, {
        ok: false,
        error_code: "mac_use_bridge_internal_error",
        error: error.message,
      });
    }
  }

  async function startBridgeServer() {
    if (bridgeServer && bridgeBaseUrl) return bridgeBaseUrl;
    shuttingDown = false;
    cleanupExpiredEvidence();
    bridgeServer = httpImpl.createServer((request, response) => {
      void handleHttp(request, response);
    });
    await new Promise((resolve, reject) => {
      bridgeServer.on("error", reject);
      bridgeServer.listen(0, BRIDGE_HOST, resolve);
    });
    const address = bridgeServer.address();
    bridgeBaseUrl = `http://${BRIDGE_HOST}:${address.port}`;
    appendLog(`[Mac Use] Bridge listening on ${bridgeBaseUrl}`);
    return bridgeBaseUrl;
  }

  async function stopBridgeServer() {
    shuttingDown = true;
    ensureStorage();
    await Promise.allSettled(
      [...sessions.values()].map((state) =>
        fsImpl.promises.writeFile(
          cancelPath(state.run_id, state.revocation_epoch),
          "stopped\n",
          {
            mode: 0o600,
          },
        ),
      ),
    );
    for (const runId of sessions.keys()) setSessionState(runId, "stopped");
    if (!bridgeServer) return;
    const server = bridgeServer;
    bridgeServer = null;
    bridgeBaseUrl = "";
    await new Promise((resolve) => server.close(resolve));
  }

  function installIpcHandlers() {
    if (!ipcMain) return;
    ipcMain.handle("lantai:mac-use-status", async (event) => {
      if (!isTrustedIpcEvent(event)) {
        return { ok: false, error_code: "untrusted_ipc_sender" };
      }
      return execute({ action: "status" });
    });
    ipcMain.handle("lantai:mac-use-stop", async (event, runId = "") => {
      if (!isTrustedIpcEvent(event)) {
        return { ok: false, error_code: "untrusted_ipc_sender" };
      }
      return execute({ action: "stop", run_id: runId });
    });
    ipcMain.handle(
      "lantai:mac-use-select-target",
      async (event, target = {}) => {
        if (!isTrustedIpcEvent(event)) {
          return { ok: false, error_code: "untrusted_ipc_sender" };
        }
        return execute({
          action: "select_target",
          run_id: "desktop-window-picker",
          target: {
            app_bundle_id: String(target.app_bundle_id || "").slice(0, 300),
            window_id: String(target.window_id || "").slice(0, 80),
            process_id: Number(target.process_id) || 0,
            process_started_at: Number(target.process_started_at) || 0,
            window_title: String(target.window_title || "").slice(0, 500),
          },
        });
      },
    );
    ipcMain.handle(
      "lantai:mac-use-request-permission",
      async (event, key = "") => {
        if (!isTrustedIpcEvent(event)) {
          return { ok: false, error_code: "untrusted_ipc_sender" };
        }
        if (key === "accessibility") {
          return execute({
            action: "request_accessibility",
            run_id: "permission-ui",
          });
        }
        if (key === "screen_recording") {
          return execute({
            action: "request_screen_recording",
            run_id: "permission-ui",
          });
        }
        return { ok: false, error_code: "mac_use_permission_unsupported" };
      },
    );
  }

  return {
    execute,
    getBridgeBaseUrl: () => bridgeBaseUrl,
    getBridgeToken: () => bridgeToken,
    getHelperPath: () => helperPath,
    cleanupExpiredEvidence,
    installIpcHandlers,
    startBridgeServer,
    stopBridgeServer,
  };
}

module.exports = {
  ACTIONS,
  AMBIGUOUS_WRITE_ACTIONS,
  PROTECTED_WRITE_BUNDLES,
  WRITE_ACTIONS,
  createMacUseController,
  failure,
  identity,
  runHelperProcess,
  resolveMacUseHelperPath,
  safeIdentifier,
};
