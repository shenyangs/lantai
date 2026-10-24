# -*- coding: utf-8 -*-
"""Unit coverage for the steering UI L4 gap evidence gate."""

# pylint: disable=protected-access

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "packaged_steering_ui_gap_smoke.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "packaged_steering_ui_gap_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _renderer_payload() -> dict:
    control = "PACKAGED_CONTROL_400_deadbeef"
    failed_actions = [
        {"label": f"补充当前任务：{control}", "text": "补充当前", "disabled": False},
        {"label": f"调整方向：{control}", "text": "重试调整方向", "disabled": False},
        {"label": f"排队下一任务：{control}", "text": "下一任务", "disabled": False},
        {"label": f"编辑引导：{control}", "text": "", "disabled": False},
        {"label": f"删除引导：{control}", "text": "", "disabled": False},
    ]
    unknown = "PACKAGED_CONTROL_NETWORK_deadbeef"
    uncertain_actions = [
        {"label": f"补充当前任务：{unknown}", "text": "补充当前", "disabled": True},
        {"label": f"调整方向：{unknown}", "text": "重试调整方向", "disabled": False},
        {"label": f"排队下一任务：{unknown}", "text": "下一任务", "disabled": True},
        {"label": f"编辑引导：{unknown}", "text": "", "disabled": True},
        {"label": f"删除引导：{unknown}", "text": "", "disabled": True},
    ]
    return {
        "deterministic": {
            "primary_active_before_control": True,
            "primary_active_after_control": True,
            "stop_label_after_control": "停止当前任务",
            "guidance": {
                "status": "failed",
                "text": f"{control}输入无法接收 · 请编辑后重试",
                "actions": failed_actions,
            },
        },
        "unknown": {
            "primary_active_before_control": True,
            "primary_active_after_control": True,
            "guidance": {
                "status": "uncertain",
                "text": f"{unknown}接收待确认 · 可安全重试",
                "actions": uncertain_actions,
            },
        },
        "restore": {
            "before": {
                "id": "projection-message-1",
                "command_id": "command-1",
                "timestamp": "2026-09-21T01:02:03Z",
            },
            "after": {
                "id": "projection-message-2",
                "command_id": "command-1",
                "timestamp": "2026-09-21T01:02:03Z",
            },
            "restored": {"queue_status": "admitted"},
            "latest_server_status": "pending",
            "status_requests_before_return": 0,
            "status_requests_after_return": 1,
        },
        "accessibility": {
            "article_label": f"引导 1：{control}",
            "status_role": "status",
            "status_live": "polite",
            "group_label": f"引导操作：{control}",
            "button_labels": [item["label"] for item in failed_actions],
            "focus_before": failed_actions[0]["label"],
            "focus_after": failed_actions[1]["label"],
            "ax_nodes": [
                {"role": "button", "name": item["label"]}
                for item in failed_actions
            ],
        },
        "visuals": {
            "modes": [
                {
                    "theme": "light",
                    "html_class": "",
                    "status_text": "输入无法接收 · 请编辑后重试",
                    "screenshot_sha256": "light",
                },
                {
                    "theme": "dark",
                    "html_class": "dark-mode",
                    "status_text": "输入无法接收 · 请编辑后重试",
                    "screenshot_sha256": "dark",
                },
            ],
            "narrow": {
                "no_horizontal_overflow": True,
                "card_in_viewport": True,
                "status_text": "输入无法接收 · 请编辑后重试",
            },
        },
    }


def test_gap_evidence_requires_all_l4_boundaries() -> None:
    module = _load_module()
    renderer = _renderer_payload()
    evidence = module._build_evidence(
        app_metadata={"short_version": "2.2.12"},
        provenance={"canonicalMain": False, "gitSha": "abc123"},
        renderer=renderer,
        provider_request_count=3,
        expected_git_sha="abc123",
    )

    assert evidence["schema"] == "com.lantai.packaged-steering-ui-gap-smoke.v1"
    assert evidence["ok"] is True
    assert all(evidence["checks"].values())

    already_reconciled = copy.deepcopy(renderer)
    already_reconciled["restore"]["status_requests_before_return"] = 1
    already_reconciled["restore"]["status_requests_after_return"] = 1
    evidence = module._build_evidence(
        app_metadata={"short_version": "2.2.12"},
        provenance={"canonicalMain": False, "gitSha": "abc123"},
        renderer=already_reconciled,
        provider_request_count=3,
        expected_git_sha="abc123",
    )
    assert evidence["checks"]["return_reconciles_with_server"] is True

    never_reconciled = copy.deepcopy(renderer)
    never_reconciled["restore"]["status_requests_before_return"] = 0
    never_reconciled["restore"]["status_requests_after_return"] = 0
    failed = module._build_evidence(
        app_metadata={"short_version": "2.2.12"},
        provenance={"canonicalMain": False, "gitSha": "abc123"},
        renderer=never_reconciled,
        provider_request_count=3,
        expected_git_sha="abc123",
    )
    assert failed["checks"]["return_reconciles_with_server"] is False
    assert failed["ok"] is False

    missing_timestamp = copy.deepcopy(renderer)
    missing_timestamp["restore"]["before"]["timestamp"] = None
    failed = module._build_evidence(
        app_metadata={"short_version": "2.2.12"},
        provenance={"canonicalMain": False, "gitSha": "abc123"},
        renderer=missing_timestamp,
        provider_request_count=3,
        expected_git_sha="abc123",
    )
    assert failed["checks"]["return_preserves_logical_command_and_time"] is False
    assert failed["ok"] is False

    wrong_sha = module._build_evidence(
        app_metadata={"short_version": "2.2.12"},
        provenance={"canonicalMain": False, "gitSha": "old-sha"},
        renderer=renderer,
        provider_request_count=3,
        expected_git_sha="abc123",
    )
    assert wrong_sha["checks"]["exact_artifact_only_candidate"] is False
    assert wrong_sha["ok"] is False
