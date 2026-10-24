const net = require("net");
const path = require("path");
const packageJson = require("../package.json");

const BACKEND_HOST = "127.0.0.1";
const DEFAULT_BACKEND_PORT = 8088;
const BACKEND_HEALTH_PATH = "/health";
const BACKEND_READINESS_PATH = "/api/desktop/readiness";
const BACKEND_READINESS_INTERVAL_MS = 500;
const BACKEND_RESTART_DELAY_MS = 1000;
const MAX_BACKEND_RESTARTS = 4;
const PACKAGED_BACKEND_APP_NAME = "Lantai Backend.app";

function getProjectRoot(electronDir) {
  return path.resolve(electronDir, "../..");
}

function buildBackendBaseUrl(port, host = BACKEND_HOST) {
  return `http://${host}:${String(port || DEFAULT_BACKEND_PORT)}`;
}

function getLoopbackPortFromAddress(
  address,
  fallbackPort = DEFAULT_BACKEND_PORT,
) {
  return typeof address === "object" && address ? address.port : fallbackPort;
}

function pickLoopbackPort({
  createServer = () => net.createServer(),
  host = BACKEND_HOST,
  fallbackPort = DEFAULT_BACKEND_PORT,
} = {}) {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.unref?.();
    server.on("error", reject);
    server.listen(0, host, () => {
      const port = getLoopbackPortFromAddress(server.address(), fallbackPort);
      server.close(() => resolve(port));
    });
  });
}

function buildBackendAuthHeaders(desktopToken) {
  return { "X-Lantai-Desktop-Token": desktopToken };
}

function getBackendHealthUrl(apiBaseUrl, healthPath = BACKEND_HEALTH_PATH) {
  return `${apiBaseUrl}${healthPath}`;
}

function getBackendReadinessUrl(
  apiBaseUrl,
  readinessPath = BACKEND_READINESS_PATH,
) {
  return `${apiBaseUrl}${readinessPath}`;
}

function isCurrentBackendApiUrl(urlString, backendPort) {
  try {
    const url = new URL(urlString);
    return (
      url.port === String(backendPort) &&
      (url.hostname === BACKEND_HOST || url.hostname === "localhost") &&
      url.pathname.startsWith("/api/")
    );
  } catch {
    return false;
  }
}

function getBackendRestartDecision({
  isDev,
  isQuitting,
  hasRestartTimer,
  restartCount,
  maxRestartCount = MAX_BACKEND_RESTARTS,
}) {
  if (isDev) {
    return { shouldSchedule: false, reason: "dev" };
  }
  if (isQuitting) {
    return { shouldSchedule: false, reason: "quitting" };
  }
  if (hasRestartTimer) {
    return { shouldSchedule: false, reason: "already-scheduled" };
  }
  if (restartCount >= maxRestartCount) {
    return { shouldSchedule: false, reason: "max-restarts" };
  }
  return { shouldSchedule: true, nextRestartCount: restartCount + 1 };
}

function createBackendSpawnOptions({ env, cwd }) {
  return {
    env,
    cwd,
    stdio: ["ignore", "pipe", "pipe"],
  };
}

function formatBackendStartMessage({ executable, args, cwd }) {
  return `[Electron] Starting Python backend: ${executable} ${args.join(" ")} (cwd: ${cwd})`;
}

function formatBackendExitMessage(code, signal) {
  return `Python process exited with code ${code} signal ${signal || "none"}`;
}

function getPackageMacTargets(pkg = packageJson) {
  const target = pkg?.build?.mac?.target;
  if (Array.isArray(target)) {
    return target.flatMap((item) => {
      if (typeof item === "string") {
        return [item];
      }
      if (Array.isArray(item?.target)) {
        return item.target;
      }
      if (typeof item?.target === "string") {
        return [item.target];
      }
      return [];
    });
  }
  if (typeof target === "string") {
    return [target];
  }
  return [];
}

function isMasDistributionPackage(pkg = packageJson) {
  const textHints = [
    pkg?.build?.appId,
    pkg?.build?.artifactName,
    pkg?.build?.productName,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return (
    getPackageMacTargets(pkg)
      .map((target) => String(target).toLowerCase())
      .includes("mas") ||
    textHints.includes("appstore") ||
    textHints.includes("app-store") ||
    textHints.includes("-mas-") ||
    textHints.includes("mas-")
  );
}

function getDesktopDistributionChannel(pkg = packageJson) {
  return isMasDistributionPackage(pkg) ? "mas" : undefined;
}

function applyDistributionEnv(env, pkg = packageJson) {
  const distributionChannel = getDesktopDistributionChannel(pkg);
  if (distributionChannel !== "mas") {
    return;
  }
  env.LANTAI_DISTRIBUTION_CHANNEL = distributionChannel;
  env.LANTAI_RELEASE_CHANNEL = distributionChannel;
  env.LANTAI_APP_STORE = "1";
  env.LANTAI_DISABLE_GITHUB_UPDATER = "1";
}

function applyRuntimeDefaultEnv(env, pkg = packageJson) {
  if (!env.LANTAI_RUNTIME_ENGINE && !env.LANTAI_RUNTIME_ENGINE_DEFAULT) {
    env.LANTAI_RUNTIME_ENGINE_DEFAULT = "native";
  }
}

function buildBackendLaunchPlan({
  isDev,
  electronDir,
  resourcesPath,
  userDataPath,
  baseEnv,
  backendPort,
  desktopToken,
  desktopInstanceNonce = "",
}) {
  const args = ["app", "--host", BACKEND_HOST, "--port", String(backendPort)];
  const env = { ...baseEnv };
  env.PYTHONUNBUFFERED = "1";
  env.LANTAI_DESKTOP_TOKEN = desktopToken;
  if (desktopInstanceNonce) {
    env.LANTAI_DESKTOP_INSTANCE_NONCE = desktopInstanceNonce;
  }
  env.LANTAI_DESKTOP_APP = "1";
  env.LANTAI_DESKTOP_LOG_PATH = path.join(userDataPath, "backend.log");

  if (isDev) {
    const projectRoot = getProjectRoot(electronDir);
    args.unshift("-m", "lantai");
    env.PYTHONPATH = `${projectRoot}/src:${env.PYTHONPATH || ""}`;
    env.LANTAI_CONSOLE_STATIC_DIR = path.join(projectRoot, "console", "dist");
    applyDistributionEnv(env);
    applyRuntimeDefaultEnv(env);
    return {
      executable: path.join(projectRoot, ".venv/bin/python"),
      cwd: projectRoot,
      args,
      env,
      projectRoot,
    };
  }

  const runtimeWorkingDir = path.join(userDataPath, "working");
  const runtimeLogDir = path.join(userDataPath, "logs");
  const backendWorkingDir = path.resolve(
    env.LANTAI_WORKING_DIR || runtimeWorkingDir,
  );
  env.LANTAI_WORKING_DIR = backendWorkingDir;
  env.LANTAI_LOG_DIR = env.LANTAI_LOG_DIR || runtimeLogDir;
  env.LANTAI_CONSOLE_STATIC_DIR = path.join(resourcesPath, "console", "dist");
  env.LANTAI_DISABLE_KEYRING = "1";
  env.LANTAI_SKIP_KEYRING = "1";
  const bundledToolBin = path.join(resourcesPath, "lantai-server", "bin");
  const runtimeToolBin = path.join(runtimeWorkingDir, "runtime-tools");
  const bundledOfficeCli = path.join(bundledToolBin, "officecli");
  const runtimeOfficeCli = path.join(runtimeToolBin, "officecli");
  env.OFFICECLI_PATH = runtimeOfficeCli;
  env.OFFICECLI_SKIP_UPDATE = "1";
  env.PATH = [runtimeToolBin, env.PATH].filter(Boolean).join(path.delimiter);
  applyDistributionEnv(env);
  applyRuntimeDefaultEnv(env);

  return {
    executable: path.join(
      resourcesPath,
      "lantai-server",
      PACKAGED_BACKEND_APP_NAME,
      "Contents",
      "MacOS",
      "lantai-server",
    ),
    cwd: userDataPath,
    args,
    env,
    backendWorkingDir,
    runtimeWorkingDir,
    runtimeLogDir,
    runtimeToolBin,
    bundledOfficeCli,
    runtimeOfficeCli,
  };
}

module.exports = {
  BACKEND_HEALTH_PATH,
  BACKEND_HOST,
  BACKEND_READINESS_PATH,
  BACKEND_READINESS_INTERVAL_MS,
  BACKEND_RESTART_DELAY_MS,
  DEFAULT_BACKEND_PORT,
  MAX_BACKEND_RESTARTS,
  PACKAGED_BACKEND_APP_NAME,
  applyRuntimeDefaultEnv,
  buildBackendAuthHeaders,
  buildBackendBaseUrl,
  buildBackendLaunchPlan,
  createBackendSpawnOptions,
  formatBackendExitMessage,
  formatBackendStartMessage,
  getBackendHealthUrl,
  getBackendReadinessUrl,
  getBackendRestartDecision,
  getDesktopDistributionChannel,
  getLoopbackPortFromAddress,
  getProjectRoot,
  getPackageMacTargets,
  isCurrentBackendApiUrl,
  isMasDistributionPackage,
  pickLoopbackPort,
};
