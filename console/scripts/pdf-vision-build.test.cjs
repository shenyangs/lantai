const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

test(
  "compiling PDF Vision preserves the source binary and produces an executable",
  {
    skip: process.platform !== "darwin",
  },
  () => {
    const root = fs.realpathSync(
      fs.mkdtempSync(path.join(os.tmpdir(), "lantai-pdf-build-")),
    );
    try {
      const appRoot = path.join(root, "pdf");
      const arch = process.arch === "x64" ? "x64" : "arm64";
      const original = path.join(
        appRoot,
        "native-bin",
        `darwin-${arch}`,
        "lantai-pdf-vision",
      );
      fs.mkdirSync(path.dirname(original), { recursive: true });
      fs.writeFileSync(original, "source-controlled-prebuilt");
      const scripts = path.join(appRoot, "scripts");
      fs.mkdirSync(scripts);
      fs.copyFileSync(
        path.resolve(
          __dirname,
          "../vendor/lantai-docx-editor/apps/pdf/scripts/build-vision.mjs",
        ),
        path.join(scripts, "build-vision.mjs"),
      );
      const bin = path.join(root, "bin");
      fs.mkdirSync(bin);
      fs.writeFileSync(
        path.join(bin, "xcrun"),
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n  if [ "$1" = "-o" ]; then\n    shift\n    printf compiled-helper > "$1"\n    exit 0\n  fi\n  shift\ndone\nexit 1\n',
        { mode: 0o755 },
      );
      const result = spawnSync(
        process.execPath,
        [path.join(scripts, "build-vision.mjs")],
        {
          env: {
            ...process.env,
            PATH: `${bin}${path.delimiter}${process.env.PATH}`,
          },
          encoding: "utf8",
        },
      );
      assert.equal(result.status, 0, result.stderr);
      assert.equal(
        fs.readFileSync(original, "utf8"),
        "source-controlled-prebuilt",
      );
      const generated = path.join(
        appRoot,
        "generated-native",
        `darwin-${arch}`,
        "lantai-pdf-vision",
      );
      assert.equal(result.stdout.trim(), generated);
      assert.equal(fs.readFileSync(generated, "utf8"), "compiled-helper");
      fs.accessSync(generated, fs.constants.X_OK);
      const config = JSON.parse(
        fs.readFileSync(path.resolve(__dirname, "../package.json"), "utf8"),
      );
      const resource = config.build.extraResources.find(
        (entry) => entry.to === "lantai-office-editors/pdf/native/",
      );
      assert.equal(
        resource.from,
        "vendor/lantai-docx-editor/apps/pdf/generated-native/darwin-arm64/",
      );
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  },
);
