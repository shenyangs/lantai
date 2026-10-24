const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const EXPECTED_ELECTRON_RUNTIME_FILES = [
  "electron/appWindowController.cjs",
  "electron/backendLifecycleController.cjs",
  "electron/backendProcess.cjs",
  "electron/browserWorkbenchController.cjs",
  "electron/consoleMessageDetails.cjs",
  "electron/customizationRecoveryController.cjs",
  "electron/desktopIpcController.cjs",
  "electron/desktopSecurityHeaders.cjs",
  "electron/dockIconController.cjs",
  "electron/documentEntryController.cjs",
  "electron/docxEditorController.cjs",
  "electron/launchServicesController.cjs",
  "electron/logRedaction.cjs",
  "electron/macUseController.cjs",
  "electron/main.cjs",
  "electron/markdown-editor/**/*",
  "electron/markdownEditorController.cjs",
  "electron/native/bin/notification-delivery.node",
  "electron/native/bin/window-material.node",
  "electron/notificationDeliveryObserver.cjs",
  "electron/officeEditorController.cjs",
  "electron/officeWorkbenchController.cjs",
  "electron/preload.js",
  "electron/rendererDocumentTrust.cjs",
  "electron/runtimePaths.cjs",
  "electron/systemBehaviorController.cjs",
  "electron/trayController.cjs",
  "electron/windowMaterialController.cjs",
  "electron/windowState.cjs",
  "electron/workspaceGrantBookmarkStore.cjs",
];

function loadPackageJson() {
  return JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"),
  );
}

function loadPackageLock() {
  return JSON.parse(
    fs.readFileSync(path.join(__dirname, "..", "package-lock.json"), "utf8"),
  );
}

function resolveNodePackage(name, fromDir) {
  let cursor = fromDir;
  const projectRoot = path.join(__dirname, "..");
  while (cursor.startsWith(projectRoot)) {
    const packagePath = path.join(cursor, "node_modules", name, "package.json");
    if (fs.existsSync(packagePath)) {
      return packagePath;
    }
    const parent = path.dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
  }
  throw new Error(`Unable to resolve production dependency ${name}`);
}

function collectProductionDependencyClosure(rootPackage) {
  const projectRoot = path.join(__dirname, "..");
  const modulesRoot = path.join(projectRoot, "node_modules");
  const lockPackages = loadPackageLock().packages || {};
  const seenPaths = new Set();
  const packagedRoots = new Set();
  function findLockPackage(name, fromDir) {
    let cursor = fromDir;
    while (cursor.startsWith(projectRoot)) {
      const lockKey = path
        .relative(projectRoot, path.join(cursor, "node_modules", name))
        .split(path.sep)
        .join("/");
      if (lockPackages[lockKey]) {
        return { lockKey, metadata: lockPackages[lockKey] };
      }
      const parent = path.dirname(cursor);
      if (parent === cursor) break;
      cursor = parent;
    }
    return null;
  }
  function visit(name, fromDir) {
    // Type declaration packages are compile-time only and must never expand the
    // runtime packaging closure, including when package-lock records a nested
    // production copy beneath a dependency.
    if (name.startsWith("@types/")) return;

    const lockedPackage = findLockPackage(name, fromDir);
    let packagePath;
    try {
      packagePath = resolveNodePackage(name, fromDir);
    } catch (error) {
      // A pnpm-linked development tree need not expose npm's hoisted transitive
      // packages at the project root. package-lock is the production-build
      // authority, so continue from its metadata when the local link is absent.
      if (!lockedPackage) throw error;
      packagePath = `package-lock:${lockedPackage.lockKey}`;
    }
    if (seenPaths.has(packagePath)) return;
    seenPaths.add(packagePath);
    const lockKey = packagePath.startsWith("package-lock:")
      ? lockedPackage.lockKey
      : path
          .relative(projectRoot, path.dirname(packagePath))
          .split(path.sep)
          .join("/");
    const lockMetadata = lockPackages[lockKey] || lockedPackage?.metadata;
    if (lockMetadata?.dev === true) return;
    if (packagePath.startsWith("package-lock:")) {
      packagedRoots.add(name);
    } else {
      const relative = path.relative(modulesRoot, path.dirname(packagePath));
      const segments = relative.split(path.sep);
      packagedRoots.add(
        segments[0].startsWith("@")
          ? `${segments[0]}/${segments[1]}`
          : segments[0],
      );
    }
    const pkg = packagePath.startsWith("package-lock:")
      ? lockMetadata
      : JSON.parse(fs.readFileSync(packagePath, "utf8"));
    for (const dependency of Object.keys(pkg.dependencies || {})) {
      visit(
        dependency,
        packagePath.startsWith("package-lock:")
          ? path.join(projectRoot, lockKey)
          : path.dirname(packagePath),
      );
    }
  }
  visit(rootPackage, projectRoot);
  return [...packagedRoots].sort();
}

function packagedNodeModules(files) {
  return files
    .filter((entry) => entry.startsWith("node_modules/"))
    .map((entry) =>
      entry.replace(/^node_modules\//, "").replace(/\/\*\*\/\*$/, ""),
    )
    .sort();
}

test("electron-builder files use a runtime allowlist without tests, smoke helpers, or duplicate dist", () => {
  const pkg = loadPackageJson();
  const files = pkg.build?.files || [];
  const extraResources = pkg.build?.extraResources || [];
  assert.deepEqual(pkg.build?.asarUnpack, [
    "node_modules/**/*",
    "electron/native/bin/window-material.node",
    "electron/native/bin/notification-delivery.node",
  ]);
  assert.equal(
    Object.hasOwn(
      pkg.build?.mac?.extendInfo || {},
      "NSSpeechRecognitionUsageDescription",
    ),
    false,
    "the packaged app must not request Speech Recognition permission",
  );
  assert.equal(
    Object.hasOwn(
      pkg.build?.mac?.extendInfo || {},
      "NSMicrophoneUsageDescription",
    ),
    false,
    "the packaged app must not request microphone permission",
  );
  assert.match(
    pkg.build?.mac?.extendInfo?.NSAppleEventsUsageDescription || "",
    /跨应用|automation/i,
    "the packaged app must explain cross-application automation",
  );

  assert.equal(files.includes("electron/**/*"), false);
  assert.equal(files.includes("build/**/*"), false);
  assert.equal(files.includes("dist/**/*"), false);
  assert.equal(files.includes("build/icon.icns"), true);

  const electronFiles = files
    .filter((entry) => entry.startsWith("electron/"))
    .sort();
  assert.deepEqual(electronFiles, EXPECTED_ELECTRON_RUNTIME_FILES);
  assert.equal(
    electronFiles.some((entry) => entry.endsWith(".test.cjs")),
    false,
  );
  assert.equal(
    electronFiles.some((entry) => /Smoke|ManualLogin|notarize/.test(entry)),
    false,
  );

  assert.equal(
    extraResources.filter(
      (resource) =>
        resource.from === "dist/" && resource.to === "console/dist/",
    ).length,
    1,
  );
  assert.equal(
    extraResources.some(
      (resource) =>
        resource.from === "electron-dist/apple-speech/" ||
        resource.to === "apple-speech/",
    ),
    false,
    "the removed Apple Speech helper must not be packaged",
  );
  assert.equal(
    extraResources.some(
      (resource) =>
        resource.from === "electron-dist/mac-use/" &&
        resource.to === "mac-use/" &&
        Array.isArray(resource.filter) &&
        resource.filter.includes("**/*"),
    ),
    true,
    "the signed Mac Use helper must be copied into packaged Resources",
  );
  assert.equal(
    extraResources.some(
      (resource) =>
        resource.from === "vendor/lantai-docx-editor/apps/docs/out/" &&
        resource.to === "lantai-docx-editor/",
    ),
    true,
    "the local DOCX editor runtime must be copied into packaged Resources",
  );
  assert.equal(
    extraResources.some(
      (resource) =>
        resource.from ===
          "vendor/lantai-docx-editor/apps/pdf/generated-native/darwin-arm64/" &&
        resource.to === "lantai-office-editors/pdf/native/",
    ),
    true,
    "the signed PDF Vision OCR runtime must be copied into packaged Resources",
  );
  for (const expectedResource of [
    "public/lantai-mark.png",
    "public/lantai-trayTemplate.png",
    "public/lantai-trayTemplate@2x.png",
  ]) {
    assert.equal(
      extraResources.some((resource) => resource.from === expectedResource),
      true,
      `${expectedResource} must be copied into packaged Resources`,
    );
  }
});

test("packaged app does not register as a system document handler", () => {
  const pkg = loadPackageJson();
  assert.equal(Object.hasOwn(pkg.build || {}, "fileAssociations"), false);
});

test("Office packaging is limited to Lantai-owned runtime entry points and carries provenance", () => {
  const pkg = loadPackageJson();
  const vendorRoot = path.join(__dirname, "..", "vendor", "lantai-docx-editor");
  const vendorPackage = JSON.parse(
    fs.readFileSync(path.join(vendorRoot, "package.json"), "utf8"),
  );
  const provenance = JSON.parse(
    fs.readFileSync(
      path.join(vendorRoot, "INTEGRATION_PROVENANCE.json"),
      "utf8",
    ),
  );

  assert.deepEqual(vendorPackage.workspaces, [
    "apps/docs",
    "apps/sheets",
    "apps/slides",
    "apps/pdf",
    "packages/*",
  ]);
  assert.equal(provenance.component_id, "lantai-local-office-runtime");
  assert.deepEqual(provenance.runtime_apps, [
    "docs",
    "sheets",
    "slides",
    "pdf",
  ]);
  assert.equal(provenance.authority.host_shell, "lantai");
  assert.equal(provenance.authority.ai_product, "lantai");
  assert.equal(provenance.authority.permissions, "lantai");

  const officeResources = (pkg.build?.extraResources || []).filter((resource) =>
    String(resource.from || "").startsWith("vendor/lantai-docx-editor/"),
  );
  assert.deepEqual(
    officeResources.map((resource) => resource.from).sort(),
    [
      "vendor/lantai-docx-editor/INTEGRATION_PROVENANCE.json",
      "vendor/lantai-docx-editor/LICENSE",
      "vendor/lantai-docx-editor/LICENSE",
      "vendor/lantai-docx-editor/NOTICE",
      "vendor/lantai-docx-editor/NOTICE",
      "vendor/lantai-docx-editor/SOURCE.md",
      "vendor/lantai-docx-editor/SOURCE.md",
      "vendor/lantai-docx-editor/apps/docs/out/",
      "vendor/lantai-docx-editor/apps/pdf/generated-native/darwin-arm64/",
      "vendor/lantai-docx-editor/apps/pdf/out/",
      "vendor/lantai-docx-editor/apps/sheets/native-bin/darwin-arm64/",
      "vendor/lantai-docx-editor/apps/sheets/out/",
      "vendor/lantai-docx-editor/apps/slides/out/",
    ].sort(),
  );
  assert.equal(
    officeResources.some((resource) =>
      /(?:^|\/)(?:ee|shell|account|telemetry|updater)(?:\/|$)/i.test(
        String(resource.from || ""),
      ),
    ),
    false,
  );
  assert.equal(
    officeResources.some(
      (resource) =>
        resource.from === "vendor/lantai-docx-editor/" ||
        resource.from === "vendor/lantai-docx-editor/**/*",
    ),
    false,
    "the complete upstream-style source tree must never be packaged",
  );
});

test("every local module required by Electron main is packaged", () => {
  const pkg = loadPackageJson();
  const files = new Set(pkg.build?.files || []);
  const mainSource = fs.readFileSync(path.join(__dirname, "main.cjs"), "utf8");
  const requiredModules = [
    ...mainSource.matchAll(/require\(["']\.\/([^"']+)["']\)/g),
  ].map((match) => `electron/${match[1]}`);

  assert.ok(requiredModules.length > 0);
  for (const modulePath of requiredModules) {
    assert.equal(
      fs.existsSync(
        path.join(__dirname, modulePath.replace(/^electron\//, "")),
      ),
      true,
      `Electron main requires a missing local module: ${modulePath}`,
    );
    assert.equal(
      files.has(modulePath),
      true,
      `Electron main dependency is absent from build.files: ${modulePath}`,
    );
  }
});

test("node_modules allowlist follows every production dependency closure", () => {
  const pkg = loadPackageJson();
  const files = pkg.build?.files || [];
  const expectedNodeModules = [
    ...new Set(
      Object.keys(pkg.dependencies || {}).flatMap((dependency) =>
        collectProductionDependencyClosure(dependency),
      ),
    ),
  ].sort();

  assert.deepEqual(packagedNodeModules(files), expectedNodeModules);
});

test("production dependencies contain only modules required by Electron main", () => {
  const pkg = loadPackageJson();
  const expectedRuntimeDependencies = ["chrome-devtools-mcp", "pptxgenjs"];

  assert.deepEqual(
    Object.keys(pkg.dependencies || {}).sort(),
    expectedRuntimeDependencies,
  );
});

test("mac signing ignores only known data files", () => {
  const pkg = loadPackageJson();
  const signIgnore = pkg.build?.mac?.signIgnore || [];

  assert.deepEqual(signIgnore, [
    "\\.pak$",
    "\\.py$",
    "\\.pyc$",
    "/pytz/zoneinfo/",
    "/pdfminer/cmap/",
    "/pdfjs/cmaps/",
  ]);
  for (const codePattern of [
    "\\.so$",
    "\\.dylib$",
    "lantai-server",
    "officecli",
    "Contents/MacOS",
  ]) {
    assert.equal(signIgnore.includes(codePattern), false);
  }
});
