const crypto = require("crypto");
const fs = require("fs");
const { execFileSync, spawn } = require("child_process");
const path = require("node:path");

const {
  BACKEND_READINESS_INTERVAL_MS,
  BACKEND_RESTART_DELAY_MS,
  DEFAULT_BACKEND_PORT,
  MAX_BACKEND_RESTARTS,
  buildBackendAuthHeaders,
  buildBackendBaseUrl,
  buildBackendLaunchPlan,
  createBackendSpawnOptions,
  formatBackendExitMessage,
  formatBackendStartMessage,
  getBackendHealthUrl,
  getBackendReadinessUrl,
  getBackendRestartDecision,
  isCurrentBackendApiUrl,
  pickLoopbackPort,
} = require("./backendProcess.cjs");

const DEFAULT_BACKEND_RESTART_READINESS_TIMEOUT_MS = 45_000;
const DEFAULT_BACKEND_SLOW_READINESS_TIMEOUT_MS = 5 * 60_000;

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function listDescendantProcessIds(
  rootPid,
  { platform = process.platform, execFileSyncImpl = execFileSync } = {},
) {
  if (platform === "win32" || !Number.isInteger(rootPid) || rootPid <= 0) {
    return [];
  }
  const output = execFileSyncImpl("/bin/ps", ["-axo", "pid=,ppid="], {
    encoding: "utf8",
  });
  const childrenByParent = new Map();
  for (const line of String(output || "").split(/\r?\n/)) {
    const match = line.trim().match(/^(\d+)\s+(\d+)$/);
    if (!match) {
      continue;
    }
    const pid = Number.parseInt(match[1], 10);
    const ppid = Number.parseInt(match[2], 10);
    const children = childrenByParent.get(ppid) || [];
    children.push(pid);
    childrenByParent.set(ppid, children);
  }

  const descendants = [];
  const stack = [...(childrenByParent.get(rootPid) || [])];
  while (stack.length > 0) {
    const pid = stack.pop();
    descendants.push(pid);
    stack.push(...(childrenByParent.get(pid) || []));
  }
  return descendants.reverse();
}

function listMatchingExecutableProcessIds(
  executable,
  { platform = process.platform, execFileSyncImpl = execFileSync } = {},
) {
  if (platform === "win32" || !String(executable || "").trim()) {
    return [];
  }
  const marker = String(executable).trim();
  const output = execFileSyncImpl("/bin/ps", ["-axo", "pid=,command="], {
    encoding: "utf8",
  });
  const matches = [];
  for (const line of String(output || "").split(/\r?\n/)) {
    const match = line.trim().match(/^(\d+)\s+(.+)$/);
    if (!match) {
      continue;
    }
    const pid = Number.parseInt(match[1], 10);
    const command = match[2].trim();
    if (command === marker || command.startsWith(`${marker} `)) {
      matches.push(pid);
    }
  }
  return matches;
}

function installAppTerminationSignalHandlers({
  processObject = process,
  requestQuit,
}) {
  let quitRequested = false;
  const handleTerminationSignal = () => {
    if (quitRequested) {
      return;
    }
    quitRequested = true;
    requestQuit();
  };

  processObject.on("SIGTERM", handleTerminationSignal);
  processObject.on("SIGINT", handleTerminationSignal);
  return () => {
    processObject.off("SIGTERM", handleTerminationSignal);
    processObject.off("SIGINT", handleTerminationSignal);
  };
}

function preparePackagedRuntime(launchPlan, fsImpl = fs) {
  fsImpl.mkdirSync(launchPlan.runtimeWorkingDir, { recursive: true });
  fsImpl.mkdirSync(launchPlan.runtimeLogDir, { recursive: true });

  const source = launchPlan.bundledOfficeCli;
  const destination = launchPlan.runtimeOfficeCli;
  const runtimeToolBin = launchPlan.runtimeToolBin;
  if (!source || !destination || !runtimeToolBin) {
    return;
  }
  if (
    typeof fsImpl.copyFileSync !== "function" ||
    typeof fsImpl.chmodSync !== "function" ||
    typeof fsImpl.renameSync !== "function" ||
    typeof fsImpl.existsSync !== "function" ||
    typeof fsImpl.unlinkSync !== "function"
  ) {
    return;
  }

  fsImpl.mkdirSync(runtimeToolBin, { recursive: true });
  const staged = `${destination}.stage-${process.pid}`;
  try {
    fsImpl.copyFileSync(source, staged);
    fsImpl.chmodSync(staged, 0o755);
    fsImpl.renameSync(staged, destination);
  } finally {
    if (fsImpl.existsSync(staged)) {
      fsImpl.unlinkSync(staged);
    }
  }
}

function createBackendLifecycleController({
  isDev,
  electronDir,
  getResourcesPath,
  getUserDataPath,
  getBaseEnv,
  appendBackendLog,
  consoleObject = console,
  fsImpl = fs,
  spawnImpl = spawn,
  fetchImpl = globalThis.fetch,
  randomToken = () => crypto.randomBytes(32).toString("hex"),
  pickPort = pickLoopbackPort,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  sleep = defaultSleep,
  isQuitting = () => false,
  onBackendReady = () => {},
  ensureDesktopApiHeaderInjection = () => {},
  reloadWindowAfterBackendRestart = () => {},
  buildLaunchPlan = buildBackendLaunchPlan,
  listProcessRecords = null,
  killOwnedProcesses = null,
  killOwnedProcess = null,
  execFileSyncImpl = execFileSync,
  settlementIntervalMs = 25,
  restartReadinessTimeoutMs = DEFAULT_BACKEND_RESTART_READINESS_TIMEOUT_MS,
  slowReadinessTimeoutMs = DEFAULT_BACKEND_SLOW_READINESS_TIMEOUT_MS,
  readinessIntervalMs = BACKEND_READINESS_INTERVAL_MS,
  restartDelayMs = BACKEND_RESTART_DELAY_MS,
  maxRestartCount = MAX_BACKEND_RESTARTS,
  processTerminationGraceMs = 3000,
  processForceKillGraceMs = 1000,
}) {
  let backendProcess = null;
  let backendPort = DEFAULT_BACKEND_PORT;
  let apiBaseUrl = buildBackendBaseUrl(DEFAULT_BACKEND_PORT);
  let desktopToken = "";
  let desktopInstanceNonce = "";
  let backendRestartTimer = null;
  let backendRestartCount = 0;
  let backendWorkingDir = path.join(getUserDataPath(), "working");
  const intentionallyStoppedBackendProcesses = new WeakSet();
  const exitedBackendProcesses = new WeakSet();
  const ownedProcessRecords = new Map();
  let backendLaunchPlan = null;
  let lifecycleTail = Promise.resolve();

  function serializeLifecycle(operation) {
    const result = lifecycleTail.then(operation);
    lifecycleTail = result.catch(() => {});
    return result;
  }

  function probeProcesses(signals = []) {
    if (!backendLaunchPlan) throw new Error("backend process inspection is unavailable");
    const env = { ...backendLaunchPlan.env, LANTAI_METADATA_ONLY: "1" };
    delete env.LANTAI_DESKTOP_INSTANCE_NONCE;
    for (const key of Object.keys(env)) {
      if (/AUTH|TOKEN|SECRET|PASSWORD|CREDENTIAL/i.test(key)) delete env[key];
    }
    const args = isDev ? ["-m", "lantai", "--desktop-process-probe"] : ["--desktop-process-probe"];
    const output = execFileSyncImpl(backendLaunchPlan.executable, args, {
      cwd: backendLaunchPlan.cwd, env, encoding: "utf8", timeout: 5000,
      maxBuffer: 8 * 1024 * 1024,
      input: JSON.stringify({ nonce: desktopInstanceNonce, signals }),
      stdio: ["pipe", "pipe", "pipe"],
    });
    const result = JSON.parse(output);
    if (!Array.isArray(result.records)) throw new Error("invalid process inspection response");
    return result.records;
  }

  function hasChildExited(child) {
    return (
      exitedBackendProcesses.has(child) ||
      (child.exitCode !== null && child.exitCode !== undefined) ||
      (child.signalCode !== null && child.signalCode !== undefined)
    );
  }

  function unsettledError(detail) {
    const error = new Error(`Backend processes did not settle: ${detail}`);
    error.code = "BACKEND_UNSETTLED";
    return error;
  }

  function refreshOwnedProcesses(child) {
    let records;
    try {
      records = listProcessRecords ? listProcessRecords() : probeProcesses();
    } catch {
      throw unsettledError("process inspection failed");
    }
    if (!Array.isArray(records) || records.some((record) =>
      !Number.isInteger(record.pid) || record.pid <= 0 ||
      !Number.isInteger(record.ppid) || typeof record.birth !== "string" ||
      typeof record.alive !== "boolean" || typeof record.owned !== "boolean")) {
      throw unsettledError("process inspection is incomplete");
    }
    const current = new Map(records.map((record) => [record.pid, record]));
    for (const [pid, registered] of ownedProcessRecords) {
      const record = current.get(pid);
      if (!record || !record.alive || record.birth !== registered.birth) {
        ownedProcessRecords.delete(pid);
      } else if (!record.owned) {
        throw unsettledError(`ownership of process ${pid} is unconfirmed`);
      }
    }
    for (const record of records) {
      if (record.alive && record.owned) ownedProcessRecords.set(record.pid, record);
    }
    const root = current.get(child.pid);
    if (!hasChildExited(child) && root?.alive && !root.owned) {
      throw unsettledError("root process ownership is unconfirmed");
    }
    const depth = (record) => {
      const seen = new Set();
      let cursor = record;
      while (cursor && !seen.has(cursor.pid)) {
        seen.add(cursor.pid);
        cursor = current.get(cursor.ppid);
      }
      return seen.size;
    };
    return [...ownedProcessRecords.values()]
      .filter((record) => record.pid !== child.pid)
      .sort((left, right) => depth(right) - depth(left) || right.pid - left.pid);
  }

  async function signalHelpers(records, signal, sent) {
    const pending = records.filter((record) => !sent.has(`${record.pid}:${record.birth}:${signal}`));
    if (!pending.length) return;
    if (killOwnedProcess) {
      for (const record of pending) await killOwnedProcess(record, signal);
    } else if (killOwnedProcesses) {
      await killOwnedProcesses(pending, signal);
    } else {
      probeProcesses(pending.map((record) => ({ pid: record.pid, birth: record.birth, signal })));
    }
    for (const record of pending) sent.add(`${record.pid}:${record.birth}:${signal}`);
  }

  async function stopBackendProcessInternal() {
    clearRestartTimer();
    const child = backendProcess;
    if (!child) return;
    intentionallyStoppedBackendProcesses.add(child);
    const sent = new Set();
    const signalRoot = async (signal) => {
      if (hasChildExited(child)) return;
      const root = ownedProcessRecords.get(child.pid);
      // A native snapshot can observe death before Node delivers the exit
      // event. Wait for that event; never signal a PID without its identity.
      if (!root) return;
      await signalHelpers([root], signal, sent);
    };
    try {
      let helpers = refreshOwnedProcesses(child);
      await signalHelpers(helpers, "SIGTERM", sent);
      await signalRoot("SIGTERM");
      const phases = [
        ["SIGTERM", processTerminationGraceMs],
        ["SIGKILL", processForceKillGraceMs],
      ];
      for (const [signal, graceMs] of phases) {
        const deadline = Date.now() + Math.max(0, graceMs);
        let emptyScans = 0;
        if (signal === "SIGKILL") {
          helpers = refreshOwnedProcesses(child);
          await signalHelpers(helpers, signal, sent);
          if (!hasChildExited(child)) {
            appendBackendLog(`[Electron] Backend ${child.pid || "unknown"} did not settle after SIGTERM; sending SIGKILL`);
            await signalRoot(signal);
          }
        }
        do {
          // Yield independently of the readiness clock so exit events and
          // inherited stdio cannot strand the process-death observation.
          await defaultSleep(Math.max(1, settlementIntervalMs));
          helpers = refreshOwnedProcesses(child);
          await signalHelpers(helpers, signal, sent);
          await signalRoot(signal);
          if (hasChildExited(child) && helpers.length === 0) {
            emptyScans += 1;
            if (emptyScans >= 2) {
              backendProcess = null;
              backendLaunchPlan = null;
              ownedProcessRecords.clear();
              return;
            }
          } else emptyScans = 0;
        } while (Date.now() < deadline || emptyScans === 1);
      }
      throw unsettledError("owned root or helpers remain alive after SIGKILL");
    } catch (error) {
      const failure = error.code === "BACKEND_UNSETTLED" ? error : unsettledError("termination could not be confirmed");
      appendBackendLog(`[Electron] ${failure.message}; retaining process ownership`);
      throw failure;
    }
  }

  function stopBackendProcess() {
    return serializeLifecycle(stopBackendProcessInternal);
  }

  function getLifecycleState() {
    return {
      apiBaseUrl,
      backendPort,
      backendPid:
        Number.isInteger(backendProcess?.pid) && backendProcess.pid > 0
          ? backendProcess.pid
          : 0,
      backendRestartCount,
      desktopToken,
      desktopInstanceNonce,
      hasBackendProcess: Boolean(backendProcess),
      hasRestartTimer: Boolean(backendRestartTimer),
    };
  }

  async function hasMatchingReadinessProof(res, expectedInstanceNonce) {
    if (!res.ok || !expectedInstanceNonce) {
      return false;
    }

    try {
      const payload = await res.json();
      return (
        payload?.status === "ok" &&
        payload?.desktop === true &&
        payload?.instance_nonce === expectedInstanceNonce &&
        Number.isInteger(payload?.pid) &&
        payload.pid > 0
      );
    } catch {
      return false;
    }
  }

  async function waitForReady(timeoutMs, expectedProcess = backendProcess) {
    if (!expectedProcess || hasChildExited(expectedProcess)) {
      return false;
    }

    const start = Date.now();
    const readinessUrl = getBackendReadinessUrl(apiBaseUrl);
    const headers = buildBackendAuthHeaders(desktopToken);
    const expectedInstanceNonce = desktopInstanceNonce;

    while (Date.now() - start < timeoutMs) {
      if (
        backendProcess !== expectedProcess ||
        hasChildExited(expectedProcess)
      ) {
        return false;
      }

      try {
        const res = await fetchImpl(readinessUrl, { headers });
        if (
          backendProcess === expectedProcess &&
          (await hasMatchingReadinessProof(res, expectedInstanceNonce))
        ) {
          backendRestartCount = 0;
          try {
            onBackendReady(getLifecycleState());
          } catch (err) {
            appendBackendLog(
              `[Electron] Backend readiness observer failed: ${err.message}`,
            );
          }
          return true;
        }
      } catch {
        // backend not ready yet
      }
      await sleep(readinessIntervalMs);
    }

    return false;
  }

  async function handleReadinessTimeout(reason) {
    const expectedProcess = backendProcess;
    const expectedApiBaseUrl = apiBaseUrl;
    if (!expectedProcess || hasChildExited(expectedProcess)) {
      scheduleRestart(reason);
      return false;
    }

    appendBackendLog(
      `[Electron] Backend process ${expectedProcess.pid || "unknown"} is still alive after readiness timeout; preserving it during extended startup`,
    );
    const ready = await waitForReady(slowReadinessTimeoutMs, expectedProcess);
    if (!ready) {
      if (
        backendProcess === expectedProcess &&
        !hasChildExited(expectedProcess)
      ) {
        appendBackendLog(
          `[Electron] Backend process ${expectedProcess.pid || "unknown"} is still alive after the extended readiness wait; leaving it running`,
        );
      }
      return false;
    }

    if (backendProcess === expectedProcess) {
      reloadWindowAfterBackendRestart(expectedApiBaseUrl);
      return true;
    }
    return false;
  }

  function scheduleRestart(reason) {
    const restartDecision = getBackendRestartDecision({
      isDev,
      isQuitting: isQuitting(),
      hasRestartTimer: Boolean(backendRestartTimer),
      restartCount: backendRestartCount,
      maxRestartCount,
    });
    if (!restartDecision.shouldSchedule) {
      if (restartDecision.reason === "max-restarts") {
        appendBackendLog(
          `[Electron] Backend restart skipped after ${backendRestartCount} attempts: ${reason}`,
        );
      }
      return;
    }

    backendRestartCount = restartDecision.nextRestartCount;
    appendBackendLog(
      `[Electron] Scheduling backend restart #${backendRestartCount}: ${reason}`,
    );
    backendRestartTimer = setTimeoutFn(async () => {
      backendRestartTimer = null;
      try {
        await start();
        const ready = await waitForReady(restartReadinessTimeoutMs);
        if (!ready) {
          appendBackendLog(
            "[Electron] Restarted backend did not become ready in time; checking whether the live process only needs more time",
          );
          await handleReadinessTimeout(
            "backend readiness timed out after restart",
          );
          return;
        }
        reloadWindowAfterBackendRestart(apiBaseUrl);
      } catch (err) {
        appendBackendLog(`[Electron] Backend restart failed: ${err.message}`);
        if (err.code !== "BACKEND_UNSETTLED") scheduleRestart(err.message);
      }
    }, restartDelayMs);
  }

  async function startInternal() {
    if (backendProcess) {
      appendBackendLog(
        "[Electron] Stopping existing managed backend before starting a new one",
      );
      await stopBackendProcessInternal();
    }

    backendPort = await pickPort();
    apiBaseUrl = buildBackendBaseUrl(backendPort);
    desktopToken = randomToken();
    desktopInstanceNonce = randomToken();

    const launchPlan = buildLaunchPlan({
      isDev,
      electronDir,
      resourcesPath: getResourcesPath(),
      userDataPath: getUserDataPath(),
      baseEnv: getBaseEnv(),
      backendPort,
      desktopToken,
      desktopInstanceNonce,
    });
    backendLaunchPlan = launchPlan;
    const { executable, cwd, args, env } = launchPlan;
    backendWorkingDir =
      launchPlan.backendWorkingDir ||
      path.resolve(env.LANTAI_WORKING_DIR || backendWorkingDir);

    if (!isDev) {
      preparePackagedRuntime(launchPlan, fsImpl);
    }

    ensureDesktopApiHeaderInjection();

    const startMessage = formatBackendStartMessage({ executable, args, cwd });
    consoleObject.log(startMessage);
    appendBackendLog(startMessage);

    const child = spawnImpl(
      executable,
      args,
      createBackendSpawnOptions({ env, cwd }),
    );
    backendProcess = child;

    child.stdout?.on("data", (data) => {
      const message = `[Python] ${data.toString().trim()}`;
      consoleObject.log(message);
      appendBackendLog(message);
    });

    child.stderr?.on("data", (data) => {
      const message = `[Python Error] ${data.toString().trim()}`;
      consoleObject.error(message);
      appendBackendLog(message);
    });

    let terminationHandled = false;
    const handleTermination = (code, signal) => {
      exitedBackendProcesses.add(child);
      if (terminationHandled) return;
      terminationHandled = true;
      const message = formatBackendExitMessage(code, signal);
      consoleObject.log(message);
      appendBackendLog(message);
      if (intentionallyStoppedBackendProcesses.has(child)) return;
      void serializeLifecycle(async () => {
        if (backendProcess !== child) return;
        await stopBackendProcessInternal();
        if (!isQuitting()) scheduleRestart(message);
      }).catch((error) => appendBackendLog(`[Electron] Crash cleanup failed: ${error.message}`));
    };

    // exit confirms process death even when a helper retains the stdio pipe.
    child.once("exit", handleTermination);
    child.once("close", handleTermination);
    child.on("error", (error) => {
      const message = `Failed to start Python backend: ${error.message}`;
      consoleObject.error(message);
      appendBackendLog(message);
      if (!Number.isInteger(child.pid) || child.pid <= 0) {
        // A spawn failure with no PID never acquired an OS process.
        handleTermination(null, "spawn-error");
      }
    });
  }

  function start() {
    return serializeLifecycle(startInternal);
  }

  function clearRestartTimer() {
    if (backendRestartTimer) {
      clearTimeoutFn(backendRestartTimer);
      backendRestartTimer = null;
    }
  }

  return {
    clearRestartTimer,
    getApiBaseUrl: () => apiBaseUrl,
    getBackendPort: () => backendPort,
    getBackendWorkingDir: () => backendWorkingDir,
    getDesktopToken: () => desktopToken,
    getState: getLifecycleState,
    handleReadinessTimeout,
    isCurrentBackendApiUrl: (url) => isCurrentBackendApiUrl(url, backendPort),
    killBackendProcess: stopBackendProcess,
    scheduleRestart,
    start,
    stopBackendProcess,
    waitForReady,
  };
}

module.exports = {
  DEFAULT_BACKEND_RESTART_READINESS_TIMEOUT_MS,
  DEFAULT_BACKEND_SLOW_READINESS_TIMEOUT_MS,
  createBackendLifecycleController,
  installAppTerminationSignalHandlers,
  listDescendantProcessIds,
  listMatchingExecutableProcessIds,
  preparePackagedRuntime,
};
