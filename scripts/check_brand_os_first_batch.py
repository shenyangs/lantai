#!/usr/bin/env python3
"""Re-run Brand OS first-batch source, browser, and package checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


EXPECTED_BASELINE = "5c9ab7f9e14bd837b6580e65d3692f70ee353b6f"
EXPECTED_SCENARIOS = {
    "brand-composer-light",
    "brand-composer-dark",
    "brand-tools-light",
    "brand-approval-light",
    "brand-approval-narrow-dark",
    "brand-approval-loading-light",
    "brand-artifact-split-light",
    "brand-artifact-overlay-dark",
    "brand-kdocs-split-light",
    "brand-kdocs-overlay-dark",
    "brand-appearance-light",
}


class CheckFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def read_json(path: Path) -> Any:
    require(path.is_file(), f"missing JSON: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckFailure(f"invalid JSON {path}: {exc}") from exc


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_audit(scenario: dict[str, Any], audit_id: str) -> dict[str, Any]:
    audits = scenario.get("pageState", {}).get("styleAudits", [])
    for audit in audits:
        if audit.get("id") == audit_id:
            return audit
    raise CheckFailure(f"{scenario['route']['id']}: missing style audit {audit_id}")


def find_control_audit(scenario: dict[str, Any], audit_id: str) -> dict[str, Any]:
    for audit in scenario.get("controlStateAudits", []):
        if audit.get("id") == audit_id:
            return audit
    raise CheckFailure(
        f"{scenario['route']['id']}: missing control-state audit {audit_id}"
    )


def parse_css_color(value: str) -> tuple[float, float, float, float]:
    value = str(value or "").strip().lower()
    rgb_match = re.fullmatch(r"rgba?\(([^)]+)\)", value)
    if rgb_match:
        parts = [part for part in re.split(r"[,\s/]+", rgb_match.group(1)) if part]
        require(len(parts) in {3, 4}, f"unsupported rgb color: {value}")
        channels = [float(part.rstrip("%")) for part in parts[:3]]
        if any("%" in part for part in parts[:3]):
            channels = [channel * 2.55 for channel in channels]
        alpha = float(parts[3].rstrip("%")) if len(parts) == 4 else 1.0
        if len(parts) == 4 and "%" in parts[3]:
            alpha /= 100
        return (*(max(0.0, min(255.0, channel)) / 255 for channel in channels), alpha)

    srgb_match = re.fullmatch(r"color\(srgb\s+([^)]*)\)", value)
    if srgb_match:
        parts = [part for part in re.split(r"\s*/\s*|\s+", srgb_match.group(1)) if part]
        require(len(parts) in {3, 4}, f"unsupported srgb color: {value}")
        channels = [float(part) for part in parts[:3]]
        alpha = float(parts[3]) if len(parts) == 4 else 1.0
        return (*channels, alpha)

    oklab_match = re.fullmatch(r"oklab\(([^)]*)\)", value)
    if oklab_match:
        parts = [part for part in re.split(r"\s*/\s*|\s+", oklab_match.group(1)) if part]
        require(len(parts) in {3, 4}, f"unsupported oklab color: {value}")
        lightness, axis_a, axis_b = (float(part.rstrip("%")) for part in parts[:3])
        if "%" in parts[0]:
            lightness /= 100
        alpha = float(parts[3].rstrip("%")) if len(parts) == 4 else 1.0
        if len(parts) == 4 and "%" in parts[3]:
            alpha /= 100

        l_value = lightness + 0.3963377774 * axis_a + 0.2158037573 * axis_b
        m_value = lightness - 0.1055613458 * axis_a - 0.0638541728 * axis_b
        s_value = lightness - 0.0894841775 * axis_a - 1.2914855480 * axis_b
        l_value, m_value, s_value = l_value**3, m_value**3, s_value**3
        linear = (
            4.0767416621 * l_value - 3.3077115913 * m_value + 0.2309699292 * s_value,
            -1.2684380046 * l_value + 2.6097574011 * m_value - 0.3413193965 * s_value,
            -0.0041960863 * l_value - 0.7034186147 * m_value + 1.7076147010 * s_value,
        )

        def to_srgb(channel: float) -> float:
            encoded = (
                12.92 * channel
                if channel <= 0.0031308
                else 1.055 * channel ** (1 / 2.4) - 0.055
            )
            return max(0.0, min(1.0, encoded))

        return (*(to_srgb(channel) for channel in linear), alpha)

    raise CheckFailure(f"unsupported computed color: {value}")


def contrast_ratio(foreground: str, background: str) -> float:
    foreground_rgba = parse_css_color(foreground)
    background_rgba = parse_css_color(background)
    require(
        foreground_rgba[3] >= 0.999 and background_rgba[3] >= 0.999,
        f"contrast colors must be opaque: {foreground} on {background}",
    )

    def luminance(rgb: tuple[float, float, float, float]) -> float:
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in rgb[:3]
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    foreground_luminance = luminance(foreground_rgba)
    background_luminance = luminance(background_rgba)
    light = max(foreground_luminance, background_luminance)
    dark = min(foreground_luminance, background_luminance)
    return (light + 0.05) / (dark + 0.05)


def require_control_contrast(
    scenario_id: str, state_name: str, state: dict[str, Any]
) -> None:
    ratio = contrast_ratio(state.get("color", ""), state.get("backgroundColor", ""))
    require(
        ratio >= 4.5,
        f"{scenario_id}/{state_name}: primary contrast {ratio:.2f} is below 4.5",
    )


def check_source(root: Path) -> None:
    required = [
        "DESIGN.md",
        "tokens.json",
        "STATES.md",
        "states.json",
        "states.html",
        "docs/brand-os/first-batch-20260921/README.zh-CN.md",
        "docs/brand-os/first-batch-20260921/ACTION_MIGRATION.zh-CN.md",
        "docs/brand-os/first-batch-20260921/PREVIEWS.zh-CN.md",
        "docs/brand-os/first-batch-20260921/HISTORICAL_REFERENCE.zh-CN.md",
        "docs/brand-os/first-batch-20260921/VALIDATION.zh-CN.md",
        "docs/brand-os/first-batch-20260921/source-snapshot.json",
    ]
    for relative in required:
        require((root / relative).is_file(), f"missing authority file: {relative}")

    tokens = read_json(root / "tokens.json")
    require(tokens.get("baseline") == EXPECTED_BASELINE, "tokens baseline mismatch")
    require(
        tokens.get("appearance", {}).get("defaultPalette") == "mist",
        "default palette must be mist",
    )
    require(
        tokens.get("appearance", {}).get("workspaceSurfacesArePaletteInvariant")
        is True,
        "workspace surfaces must remain palette-invariant",
    )
    require(
        tokens.get("responsive", {}).get("measurement") == "content-host-width",
        "responsive measurement must use content host width",
    )
    require(
        tokens.get("betaAccess", {}).get("onlyPath")
        == ["设置", "关于", "高级", "Beta Access"],
        "Beta Access path changed",
    )

    graph = read_json(root / "states.json")
    require(graph.get("baseline") == EXPECTED_BASELINE, "states baseline mismatch")
    require(graph.get("createsRuntimeState") is False, "states must not create Runtime")
    state_entries = graph.get("states")
    transitions = graph.get("transitions")
    require(isinstance(state_entries, list) and state_entries, "states must be a list")
    require(isinstance(transitions, list) and transitions, "transitions must be a list")
    state_ids = {entry.get("id") for entry in state_entries}
    require(None not in state_ids, "state without id")
    require(len(state_ids) == len(state_entries), "duplicate state id")
    mapped_events: dict[str, set[str]] = {state_id: set() for state_id in state_ids}
    transition_keys: set[tuple[str, str]] = set()
    for transition in transitions:
        source = transition.get("from")
        target = transition.get("to")
        event = transition.get("event")
        require(source in state_ids, f"undefined transition source: {source}")
        require(target in state_ids, f"undefined transition target: {target}")
        require(isinstance(event, str) and event and event != "*", "invalid event")
        require(
            isinstance(transition.get("guards"), list) and transition["guards"],
            f"{source}/{event}: guards required",
        )
        require(
            isinstance(transition.get("actions"), list) and transition["actions"],
            f"{source}/{event}: actions required",
        )
        key = (source, event)
        require(key not in transition_keys, f"ambiguous transition: {source}/{event}")
        transition_keys.add(key)
        mapped_events[source].add(event)
    for entry in state_entries:
        declared = set(entry.get("exitEvents", []))
        require(
            declared == mapped_events[entry["id"]],
            f"{entry['id']}: declared exits differ from mapped transitions",
        )

    state_html = (root / "states.html").read_text(encoding="utf-8")
    require('fetch("./states.json"' in state_html, "states.html must load states.json")
    require("state.id" in state_html, "states.html must render state ids")

    snapshot = read_json(
        root / "docs/brand-os/first-batch-20260921/source-snapshot.json"
    )
    require(snapshot.get("baseline") == EXPECTED_BASELINE, "snapshot baseline mismatch")
    entries = snapshot.get("files")
    require(isinstance(entries, list) and entries, "snapshot files missing")
    snapshot_lines: list[str] = []
    for entry in entries:
        relative = entry.get("path")
        require(isinstance(relative, str) and not relative.startswith("/"), "bad path")
        target = root / relative
        require(target.is_file(), f"snapshot file missing: {relative}")
        actual_hash = file_sha256(target)
        require(actual_hash == entry.get("sha256"), f"snapshot hash mismatch: {relative}")
        require(target.stat().st_size == entry.get("size"), f"snapshot size mismatch: {relative}")
        snapshot_lines.append(f"{relative}\0{actual_hash}")
    snapshot_digest = hashlib.sha256("\n".join(snapshot_lines).encode()).hexdigest()
    require(snapshot.get("digest") == snapshot_digest, "snapshot digest mismatch")

    text_files = [
        root / "DESIGN.md",
        root / "tokens.json",
        root / "STATES.md",
        root / "states.json",
        root / "states.html",
        root / "console/scripts/capture-ui-review-chrome.mjs",
        root / "scripts/check_brand_os_first_batch.py",
        *(root / "docs/brand-os/first-batch-20260921").glob("*.md"),
        root / "docs/brand-os/first-batch-20260921/source-snapshot.json",
    ]
    author_home_marker = "/" + "Users/"
    for path in text_files:
        content = path.read_text(encoding="utf-8")
        require(
            author_home_marker not in content,
            f"author absolute path found: {path}",
        )

    print(f"SOURCE PASS: {len(state_ids)} states, {len(transitions)} transitions")


def check_browser(root: Path) -> None:
    preview_root = root / "docs/brand-os/first-batch-20260921/previews"
    manifest = read_json(preview_root / "brand-os-first-batch-manifest.json")
    require(manifest.get("profile") == "brand-os", "wrong capture profile")
    require(manifest.get("renderer") == "chrome-headless-cdp", "wrong renderer")
    snapshot = read_json(
        root / "docs/brand-os/first-batch-20260921/source-snapshot.json"
    )
    integration_head = snapshot.get("integrationHead")
    require(
        isinstance(integration_head, str) and re.fullmatch(r"[0-9a-f]{40}", integration_head),
        "snapshot integration HEAD missing",
    )
    fingerprint = manifest.get("sourceFingerprint", {})
    require(
        fingerprint.get("gitHead") == integration_head,
        "capture HEAD differs from integration snapshot",
    )
    require(fingerprint.get("algorithm") == "sha256", "capture hash missing")
    fingerprint_entries = fingerprint.get("files")
    require(
        isinstance(fingerprint_entries, list) and fingerprint_entries,
        "capture source files missing",
    )
    fingerprint_lines: list[str] = []
    for entry in fingerprint_entries:
        relative = entry.get("path")
        require(
            isinstance(relative, str) and not relative.startswith("/"),
            "capture source path invalid",
        )
        target = root / relative
        require(target.is_file(), f"capture source missing: {relative}")
        actual_hash = file_sha256(target)
        require(
            actual_hash == entry.get("sha256"),
            f"capture source changed after render: {relative}",
        )
        fingerprint_lines.append(f"{relative}\0{actual_hash}")
    fingerprint_digest = hashlib.sha256(
        "\n".join(fingerprint_lines).encode()
    ).hexdigest()
    require(
        fingerprint.get("digest") == fingerprint_digest,
        "capture source digest mismatch",
    )

    scenarios = manifest.get("scenarios")
    require(isinstance(scenarios, list), "manifest scenarios missing")
    by_id = {item.get("route", {}).get("id"): item for item in scenarios}
    require(set(by_id) == EXPECTED_SCENARIOS, "browser scenarios are incomplete")

    for scenario_id, scenario in by_id.items():
        require(scenario.get("settled") is True, f"{scenario_id}: not settled")
        require(
            scenario.get("actionSettled") is True,
            f"{scenario_id}: action not settled",
        )
        require(
            scenario.get("finalSettled") is True,
            f"{scenario_id}: final state not settled",
        )
        require(not scenario.get("consoleErrors"), f"{scenario_id}: console errors")
        page = scenario.get("pageState", {})
        require(page.get("shellVisible") is True, f"{scenario_id}: shell hidden")
        require(
            page.get("horizontalOverflow", 999) <= 1,
            f"{scenario_id}: horizontal overflow",
        )
        require(
            page.get("unlabeledInteractiveCount") == 0,
            f"{scenario_id}: unlabeled controls",
        )
        require(
            scenario.get("keyboardAudit", {}).get("passed") is True,
            f"{scenario_id}: keyboard audit failed",
        )
        image = preview_root / scenario.get("file", "")
        require(image.is_file() and image.stat().st_size > 10_000, f"bad image: {image}")

    for scenario_id in ("brand-composer-light", "brand-composer-dark"):
        scenario = by_id[scenario_id]
        require(find_audit(scenario, "composer-focus").get("focused") is True, "focus lost")
        submit = find_audit(scenario, "submit")
        require(submit.get("exists") is True, "submit style not applied")
        require(
            submit.get("backgroundColor") not in {"rgba(0, 0, 0, 0)", "transparent", ""},
            f"{scenario_id}: submit background missing",
        )
        control = find_control_audit(scenario, "submit")
        states = control.get("states", {})
        require(
            set(states) == {"normal", "hover", "pressed", "focus", "disabled"},
            f"{scenario_id}: incomplete computed control states",
        )
        for state_name in ("normal", "hover", "pressed", "focus"):
            require(states[state_name].get("disabled") is False, f"{scenario_id}/{state_name}: disabled")
            require_control_contrast(scenario_id, state_name, states[state_name])
        require(
            states["hover"].get("backgroundColor")
            != states["normal"].get("backgroundColor"),
            f"{scenario_id}: hover has no computed background change",
        )
        require(
            states["pressed"].get("backgroundColor")
            != states["normal"].get("backgroundColor"),
            f"{scenario_id}: pressed has no computed background change",
        )
        require(
            states["focus"].get("boxShadow") != states["normal"].get("boxShadow"),
            f"{scenario_id}: focus has no computed ring",
        )
        require(
            states["disabled"].get("disabled") is True
            and float(states["disabled"].get("opacity", "1")) < 1,
            f"{scenario_id}: disabled state is not visibly distinct",
        )

    require(
        find_audit(by_id["brand-tools-light"], "tool-menu").get("exists"),
        "tool menu missing",
    )
    approval = by_id["brand-approval-light"]
    require(find_audit(approval, "allow-once").get("exists"), "allow once missing")
    require(find_audit(approval, "cancel-task").get("exists"), "cancel task missing")
    require(
        find_audit(by_id["brand-approval-narrow-dark"], "cancel-task").get("exists"),
        "narrow cancel task missing",
    )
    loading = find_audit(
        by_id["brand-approval-loading-light"], "allow-once-loading"
    )
    require(
        loading.get("disabled") is True
        and "loading" in str(loading.get("className", "")),
        "approval loading state is not the live component state",
    )
    require_control_contrast(
        "brand-approval-loading-light", "loading", loading
    )

    artifact_split = find_audit(by_id["brand-artifact-split-light"], "artifact-panel")
    artifact_overlay = find_audit(by_id["brand-artifact-overlay-dark"], "artifact-panel")
    require(artifact_split.get("shellLayout") == "split", "artifact split mismatch")
    require(artifact_split.get("ariaModal") == "", "artifact split became modal")
    require(artifact_overlay.get("shellLayout") == "overlay", "artifact overlay mismatch")
    require(
        artifact_overlay.get("role") == "dialog" and artifact_overlay.get("ariaModal") == "true",
        "artifact overlay dialog semantics missing",
    )
    require(
        find_audit(by_id["brand-artifact-overlay-dark"], "artifact-close").get("focused")
        is True,
        "artifact overlay focus missing",
    )

    kdocs_split = find_audit(by_id["brand-kdocs-split-light"], "kdocs-panel")
    kdocs_overlay = find_audit(by_id["brand-kdocs-overlay-dark"], "kdocs-panel")
    require(kdocs_split.get("layout") == "split", "KDocs split mismatch")
    require(kdocs_split.get("ariaModal") == "", "KDocs split became modal")
    require(kdocs_overlay.get("layout") == "overlay", "KDocs overlay mismatch")
    require(
        kdocs_overlay.get("role") == "dialog" and kdocs_overlay.get("ariaModal") == "true",
        "KDocs overlay dialog semantics missing",
    )
    require(
        find_audit(by_id["brand-kdocs-overlay-dark"], "kdocs-close").get("focused")
        is True,
        "KDocs overlay focus missing",
    )
    palette = find_audit(by_id["brand-appearance-light"], "palette-grid")
    rect = palette.get("rect", {})
    require(rect.get("y", 9999) + rect.get("height", 9999) <= 900, "palette grid outside viewport")

    print(f"BROWSER PASS: {len(scenarios)} live component captures")


def check_package(package_path: Path | None) -> None:
    if package_path is None:
        print("PACKAGE NOT_RUN: pass --package-path after Release Coordinator builds a candidate")
        return
    package_path = package_path.expanduser().resolve()
    require(package_path.exists(), f"package path does not exist: {package_path}")
    require(package_path.suffix == ".app" and package_path.is_dir(), "expected a .app directory")
    require((package_path / "Contents/Info.plist").is_file(), "Info.plist missing")
    require((package_path / "Contents/MacOS").is_dir(), "app executable directory missing")
    print(f"PACKAGE STRUCTURE PASS: {package_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument(
        "--scope",
        choices=("source", "browser", "package", "all"),
        default="all",
    )
    parser.add_argument("--package-path", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.project_root.expanduser().resolve()
    try:
        require((root / ".git").exists(), f"not a Git worktree: {root}")
        if args.scope in {"source", "all"}:
            check_source(root)
        if args.scope in {"browser", "all"}:
            check_browser(root)
        if args.scope in {"package", "all"}:
            check_package(args.package_path)
    except CheckFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
