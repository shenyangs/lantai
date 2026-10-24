const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const { validateAuthorizationArgs } = require("@electron/notarize");

function readAuthority(appPath) {
  return runCommand("codesign", ["-dvv", appPath]).then((result) => {
    const match = result.combined.match(/^Authority=(.+)$/m);
    return match ? match[1].trim() : "";
  });
}

function resolveNotarizationAuth() {
  const keychainProfile =
    process.env.LANTAI_NOTARY_KEYCHAIN_PROFILE ||
    process.env.APPLE_KEYCHAIN_PROFILE ||
    "";
  if (keychainProfile) {
    return {
      source: `keychain profile ${keychainProfile}`,
      options: {
        keychainProfile,
        ...(process.env.LANTAI_NOTARY_KEYCHAIN || process.env.APPLE_KEYCHAIN
          ? {
              keychain:
                process.env.LANTAI_NOTARY_KEYCHAIN || process.env.APPLE_KEYCHAIN,
            }
          : {}),
      },
    };
  }

  if (
    process.env.APPLE_ID &&
    process.env.APPLE_APP_SPECIFIC_PASSWORD &&
    process.env.APPLE_TEAM_ID
  ) {
    return {
      source: `Apple ID ${process.env.APPLE_ID}`,
      options: {
        appleId: process.env.APPLE_ID,
        appleIdPassword: process.env.APPLE_APP_SPECIFIC_PASSWORD,
        teamId: process.env.APPLE_TEAM_ID,
      },
    };
  }

  if (
    process.env.APPLE_API_KEY &&
    process.env.APPLE_API_KEY_ID &&
    process.env.APPLE_API_ISSUER
  ) {
    return {
      source: `App Store Connect API key ${process.env.APPLE_API_KEY_ID}`,
      options: {
        appleApiKey: path.resolve(process.env.APPLE_API_KEY),
        appleApiKeyId: process.env.APPLE_API_KEY_ID,
        appleApiIssuer: process.env.APPLE_API_ISSUER,
      },
    };
  }

  return null;
}

function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, options);
    const stdout = [];
    const stderr = [];

    child.stdout?.on("data", (chunk) => stdout.push(chunk.toString()));
    child.stderr?.on("data", (chunk) => stderr.push(chunk.toString()));
    child.on("error", reject);
    child.on("close", (code, signal) => {
      resolve({
        code,
        signal,
        stdout: stdout.join(""),
        stderr: stderr.join(""),
        combined: stdout.join("") + stderr.join(""),
      });
    });
  });
}

function buildNotaryArgs(authOptions) {
  const validated = validateAuthorizationArgs(authOptions);
  if (validated.keychainProfile) {
    const args = [];
    if (validated.keychain) {
      args.push("--keychain", validated.keychain);
    }
    args.push("--keychain-profile", validated.keychainProfile);
    return args;
  }

  if (validated.appleId) {
    return [
      "--apple-id",
      validated.appleId,
      "--password",
      validated.appleIdPassword,
      "--team-id",
      validated.teamId,
    ];
  }

  return [
    "--key",
    validated.appleApiKey,
    "--key-id",
    validated.appleApiKeyId,
    "--issuer",
    validated.appleApiIssuer,
  ];
}

function extractJson(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return null;

  try {
    return JSON.parse(trimmed);
  } catch {}

  const start = trimmed.indexOf("{");
  const end = trimmed.lastIndexOf("}");
  if (start === -1 || end === -1 || end <= start) {
    return null;
  }

  try {
    return JSON.parse(trimmed.slice(start, end + 1));
  } catch {
    return null;
  }
}

async function submitForNotarization(zipPath, authOptions) {
  const args = [
    "notarytool",
    "submit",
    zipPath,
    ...buildNotaryArgs(authOptions),
    "--wait",
    "--output-format",
    "json",
  ];

  const result = await runCommand("xcrun", args);
  const parsed = extractJson(result.stdout) || extractJson(result.combined);
  if (result.code === 0 && parsed?.status === "Accepted") {
    return parsed;
  }

  const details = parsed ? JSON.stringify(parsed, null, 2) : result.combined.trim();
  throw new Error(`notarytool submit failed\n${details}`);
}

async function fetchNotarizationLog(id, authOptions) {
  const result = await runCommand("xcrun", [
    "notarytool",
    "log",
    id,
    ...buildNotaryArgs(authOptions),
  ]);
  return result.combined.trim();
}

async function stapleApp(appPath) {
  const result = await runCommand("xcrun", ["stapler", "staple", "-v", appPath]);
  if (result.code !== 0) {
    throw new Error(`stapler staple failed\n${result.combined.trim()}`);
  }
}

async function validateStaple(appPath) {
  const result = await runCommand("xcrun", ["stapler", "validate", appPath]);
  if (result.code !== 0) {
    throw new Error(`stapler validate failed\n${result.combined.trim()}`);
  }
}

exports.default = async function notarizeElectronApp(context) {
  if (context.electronPlatformName !== "darwin") return;
  if (process.env.LANTAI_SKIP_NOTARIZATION === "1") {
    console.log("[notarize] Skipping notarization because LANTAI_SKIP_NOTARIZATION=1");
    return;
  }

  const appName = context.packager.appInfo.productFilename;
  const appPath = path.join(context.appOutDir, `${appName}.app`);
  const authority = await readAuthority(appPath);
  const allowUnverified = process.env.LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD === "1";

  if (!authority.startsWith("Developer ID Application:")) {
    const message = authority
      ? `App must be signed with Developer ID Application before notarization. Current authority: ${authority}`
      : "App is unsigned or ad-hoc signed; notarization requires a Developer ID Application signature.";
    if (allowUnverified) {
      console.warn(`[notarize] ${message} Skipping because LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1`);
      return;
    }
    throw new Error(message);
  }

  const resolved = resolveNotarizationAuth();
  if (!resolved) {
    const message =
      "Missing notarization credentials. Set LANTAI_NOTARY_KEYCHAIN_PROFILE or APPLE_KEYCHAIN_PROFILE, or APPLE_ID + APPLE_APP_SPECIFIC_PASSWORD + APPLE_TEAM_ID, or APPLE_API_KEY + APPLE_API_KEY_ID + APPLE_API_ISSUER.";
    if (allowUnverified) {
      console.warn(`[notarize] ${message} Skipping because LANTAI_ALLOW_UNVERIFIED_LOCAL_BUILD=1`);
      return;
    }
    throw new Error(message);
  }

  const authOptions = {
    ...resolved.options,
    ...(process.env.APPLE_NOTARYTOOL_PATH
      ? { notarytoolPath: process.env.APPLE_NOTARYTOOL_PATH }
      : {}),
  };

  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "lantai-notary-"));
  const zipPath = path.join(tempDir, "Lantai-notary.zip");

  try {
    const zipResult = await runCommand(
      "ditto",
      ["-c", "-k", "--sequesterRsrc", "--keepParent", path.basename(appPath), zipPath],
      { cwd: path.dirname(appPath) },
    );
    if (zipResult.code !== 0) {
      throw new Error(`Failed to zip app for notarization\n${zipResult.combined.trim()}`);
    }

    console.log(`[notarize] Submitting ${appPath} using ${resolved.source}`);
    const submission = await submitForNotarization(zipPath, authOptions);
    if (submission?.id) {
      console.log(`[notarize] Accepted submission ${submission.id}`);
    }
    await stapleApp(appPath);
    await validateStaple(appPath);
    console.log(`[notarize] Notarization and stapling completed for ${appPath}`);
  } catch (error) {
    if (error && typeof error.message === "string") {
      const match = error.message.match(
        /"id"\s*:\s*"([0-9a-f-]{36})"/i,
      );
      if (match?.[1]) {
        const logOutput = await fetchNotarizationLog(match[1], authOptions).catch(() => "");
        if (logOutput) {
          error.message += `\n\nNotary log:\n${logOutput}`;
        }
      }
    }
    throw error;
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
};
