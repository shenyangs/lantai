const assert = require("node:assert/strict");
const childProcess = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { createMacUseController } = require("./macUseController.cjs");

async function main() {
  const userDataPath = fs.mkdtempSync(
    path.join(os.tmpdir(), "lantai-mac-use-smoke-"),
  );
  const controller = createMacUseController({
    isDev: true,
    electronDir: __dirname,
    resourcesPath: process.resourcesPath || __dirname,
    platform: process.platform,
    userDataPath,
  });
  try {
    const status = await controller.execute({
      action: "status",
      run_id: "native-smoke",
      action_id: "status-1",
      effect_id: "status-effect",
    });
    assert.equal(status.ok, true, JSON.stringify(status));
    assert.equal(status.dispatch_status, "not_sent");
    assert.equal(status.effect_status, "not_applicable");

    const windows = await controller.execute({
      action: "list_windows",
      run_id: "native-smoke",
      action_id: "windows-1",
      effect_id: "windows-effect",
    });
    assert.equal(windows.ok, true, JSON.stringify(windows));
    assert.ok(Array.isArray(windows.data?.windows));

    const helperPath = controller.getHelperPath();
    const helperPolicyProbe = (action, fields = {}) => {
      const result = childProcess.spawnSync(helperPath, [], {
        encoding: "utf8",
        input: JSON.stringify({
          protocol_version: 1,
          action,
          test_only: true,
          run_id: "helper-policy-smoke",
          action_id: "helper-policy-smoke",
          effect_id: "helper-policy-smoke",
          ...fields,
        }),
      });
      return JSON.parse(result.stdout);
    };
    assert.equal(
      helperPolicyProbe("_test_terminal_policy", {
        target: {
          app_bundle_id: "com.mitchellh.ghostty",
          owner_name: "Ghostty",
        },
        text: "printf safe\u2028",
      }).data.blocked,
      true,
    );
    assert.equal(
      helperPolicyProbe("_test_terminal_policy", {
        target: {
          app_bundle_id: "com.apple.TextEdit",
          owner_name: "TextEdit",
        },
        text: "ordinary note\n",
      }).data.blocked,
      false,
    );
    assert.equal(
      helperPolicyProbe("_test_screen_lock_policy").data.write_allowed,
      false,
    );
    assert.equal(
      helperPolicyProbe("_test_screen_lock_policy", {
        screen_locked: false,
      }).data.write_allowed,
      true,
    );
    const selectedBounds = { x: 10, y: 20, width: 800, height: 600 };
    assert.equal(
      helperPolicyProbe("_test_window_geometry_mapping", {
        target: { bounds: selectedBounds },
        candidate_bounds: [
          selectedBounds,
          { x: 900, y: 20, width: 800, height: 600 },
        ],
      }).data.match_index,
      0,
    );
    assert.equal(
      helperPolicyProbe("_test_window_geometry_mapping", {
        target: { bounds: selectedBounds },
        candidate_bounds: [selectedBounds, selectedBounds],
      }).data.match_index,
      null,
    );
    assert.equal(
      helperPolicyProbe("_test_sensitive_element_policy", {
        element_role: "AXTextField",
        element_subrole: "AXSecureTextField",
      }).data.sensitive,
      true,
    );
    assert.equal(
      helperPolicyProbe("_test_sensitive_element_policy", {
        element_role: "AXTextArea",
        element_subrole: "",
        contains_protected_content: true,
      }).data.sensitive,
      true,
    );

    const attackerRequest = JSON.stringify({
      protocol_version: 1,
      action: "list_windows",
      run_id: "untrusted-parent-smoke",
      action_id: "untrusted-parent-smoke",
      effect_id: "untrusted-parent-smoke",
    });
    const attacker = childProcess.spawnSync(
      "/usr/bin/python3",
      [
        "-c",
        "import subprocess,sys; p=subprocess.run([sys.argv[1]],input=sys.argv[2],text=True,capture_output=True); sys.stdout.write(p.stdout)",
        helperPath,
        attackerRequest,
      ],
      { encoding: "utf8" },
    );
    const denied = JSON.parse(attacker.stdout);
    assert.equal(denied.ok, false);
    assert.equal(denied.error_code, "untrusted_parent");
    console.log(
      JSON.stringify({
        ok: true,
        protocol_version: status.protocol_version,
        permissions: status.data,
        window_count: windows.data.windows.length,
        terminal_submit_policy: "passed",
        screen_lock_policy: "passed",
        exact_window_mapping_policy: "passed",
        sensitive_element_policy: "passed",
        direct_invocation_policy: "passed",
      }),
    );
  } finally {
    await controller.stopBridgeServer();
    fs.rmSync(userDataPath, { recursive: true, force: true });
  }
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    console.error(error.stack || error.message);
    process.exit(1);
  });
