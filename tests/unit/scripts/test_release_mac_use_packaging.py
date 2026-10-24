# -*- coding: utf-8 -*-
"""Release guards for the packaged Mac Use helper."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _function_body(script: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)^\}}\n",
        script,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing shell function: {name}"
    return match.group("body")


def test_release_rebuilds_and_gates_mac_use_helper() -> None:
    script = (ROOT / "build_and_deploy.sh").read_text(encoding="utf-8")
    main = _function_body(script, "main")
    helper_build = _function_body(script, "build_mac_use_helper")
    helper_gate = _function_body(script, "validate_packaged_mac_use_helper")
    helper_smoke = _function_body(script, "run_packaged_mac_use_helper_smoke")

    assert main.index("build_backend\n") < main.index(
        "build_mac_use_helper\n"
    ) < main.index("build_electron\n")
    assert main.index("sign_packaged_macho_leaves_for_distribution\n") < main.index(
        "validate_packaged_mac_use_helper\n"
    ) < main.index("run_packaged_mac_use_helper_smoke\n") < main.index(
        "run_packaged_skill_runtime_smoke\n"
    )
    assert "npm run build:mac-use-helper" in helper_build
    assert "Contents/Resources/mac-use/Lantai Mac Use Helper.app" in helper_gate
    assert 'lipo "$helper_executable" -verify_arch arm64' in helper_gate
    assert 'lipo "$helper_executable" -verify_arch x86_64' in helper_gate
    assert "-verify_arch arm64 x86_64" not in helper_gate
    assert "CFBundleShortVersionString" in helper_gate
    assert "codesign --verify --deep --strict" in helper_gate
    assert "LANTAI_MAC_USE_PACKAGED_SMOKE_FILE" in helper_smoke
    assert 'assert payload.get("schema") == "com.lantai.mac-use-packaged-smoke.v1"' in helper_smoke
    assert 'result.get("dispatch_status") == "not_sent"' in helper_smoke
    assert 'result.get("effect_status") == "not_applicable"' in helper_smoke
    assert "helper_path.relative_to(release_app)" in helper_smoke
    assert "mac-use-helper-smoke.json" in helper_smoke
    assert '[[ -s "$response_path" ]] || ! kill -0' not in helper_smoke
    assert 'if ! kill -0 "$app_pid"' in helper_smoke


def test_native_helper_serializes_ax_and_global_writes_with_visible_takeover() -> None:
    source = (
        ROOT / "console/electron/native/mac-use-helper/main.swift"
    ).read_text(encoding="utf-8")

    lock = source.index("let inputLockDescriptor = acquireInputLock(request)")
    ax_write = source.index('if action == "set_value" || action == "press_element"')
    global_write = source.index('if action == "click_at"')
    assert lock < ax_write < global_write
    assert "requireUserIdle(request)" in source[lock:ax_write]
    assert "showActionIndicator(owner: window.owner)" in source[lock:ax_write]
    assert "panel.ignoresMouseEvents = true" in source
    assert "panel.sharingType = .none" in source
    assert (
        'code: currentScreenLockState == true ? "screen_locked" : "screen_lock_unknown"'
        in source
    )
    assert 'action == "_test_screen_lock_policy"' in source
    assert 'action == "_test_window_geometry_mapping"' in source
    assert 'action == "_test_sensitive_element_policy"' in source
    assert "writeAllowedWhenScreenLockState" in source
    assert "uniqueGeometryMatchIndex" in source
    assert "return matches.count == 1 ? matches[0] : nil" in source
    assert "for candidate in windows where" not in source
    assert 'boolean("kCGSSessionOnConsoleKey") == true' in source
    assert 'boolean("kCGSessionLoginDoneKey") == true' in source
    assert "userID == getuid()" in source
    assert 'action == "_test_terminal_policy"' in source
    assert "containsSubmissionSeparator" in source
    assert '"com.mitchellh.ghostty"' in source
    assert '"com.github.wez.wezterm"' in source
    assert '"net.kovidgoyal.kitty"' in source
    assert 'code: "terminal_submit_blocked"' in source
    assert 'request["element_fingerprint"] as? [String: Any]' in source
    assert 'fingerprint["element_id"] as? String == request["element_id"] as? String' in source
    assert "expectedIdentifier == identifier" in source
    assert '["AXTextArea", "AXTextField"].contains(role)' in source
    assert 'code: "invalid_text_target"' in source
    assert 'subrole: subrole' in source
    assert '"AXSecureTextField"' in source
    assert 'axAttribute(element, "AXContainsProtectedContent")' in source
    assert "containsProtectedContent: containsProtectedContent" in source
    assert 'code: "sensitive_target"' in source
    for event_type in (
        ".scrollWheel",
        ".rightMouseDown",
        ".otherMouseDown",
        ".flagsChanged",
        ".leftMouseDragged",
        ".rightMouseDragged",
        ".otherMouseDragged",
    ):
        assert event_type in source
    assert "syntheticOverride: lastSyntheticKeyAt.map" in source
    pre_press = source.index("checkCancelled(request)\n        let error = AXUIElementPerformAction")
    assert "requireUserIdle(request)\n        checkCancelled(request)" in source
    assert pre_press < source.index("effect: \"ambiguous\"", pre_press)
    assert "checkCancelled(request, afterDispatch: focusError == .success)" in source
    assert "afterDispatch: focusError == .success\n            )\n            checkCancelled" in source
    terminal_guard = source.index("let terminalOrIDE = isTerminalOrIDE")
    focus = source.index("activate(window, request: request)")
    assert terminal_guard < focus
