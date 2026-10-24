# -*- coding: utf-8 -*-
from __future__ import annotations

# Script-unit tests deliberately exercise the smoke runner's private seams.
# pylint: disable=protected-access

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts" / "run_running_app_persona_semantic_smoke.py"
PROMPT_REVIEW = (
    ROOT
    / "docs"
    / "remediation"
    / "evidence"
    / "persona-profile-template-format-review-2026-09-21.json"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_running_app_persona_semantic_smoke",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_running_backend_parser_keeps_secret_out_of_public_fields() -> None:
    module = _load_module()
    app = Path("/Users/test/Downloads/澜台.app")
    table = (
        " 321 /Users/test/Downloads/澜台.app/Contents/MacOS/lantai-server "
        "app --host 127.0.0.1 --port 50453 "
        "LANTAI_DESKTOP_TOKEN=private-token OTHER=value\n"
    )

    rows = module._parse_running_backends(table, app_path=app)

    assert len(rows) == 1
    assert rows[0].pid == 321
    assert rows[0].port == 50453
    assert rows[0].desktop_token == "private-token"
    assert "private-token" not in rows[0].base_url
    assert "private-token" not in rows[0].executable


def test_process_table_tolerates_unrelated_non_utf8_bytes(monkeypatch) -> None:
    module = _load_module()

    class Completed:
        stdout = b" 12 unrelated-\x98-process\n"

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: Completed(),
    )

    assert "unrelated-\ufffd-process" in module._read_process_table()


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1:50453",
        "http://example.com:50453",
        "http://127.0.0.1",
    ),
)
def test_bridge_rejects_non_loopback_or_implicit_port(url: str) -> None:
    module = _load_module()

    with pytest.raises(ValueError):
        module._validate_loopback_base_url(url)


def test_office_payload_has_no_chat_or_tool_side_effect_inputs() -> None:
    module = _load_module()

    payload = module._office_payload(
        prompt="system",
        messages=[{"role": "user", "text": "hello"}],
        provider_id="provider",
        model="model",
        max_tokens=512,
    )

    assert payload["source_chat_id"] == ""
    assert payload["tools"] == []
    assert payload["model_binding"] == {
        "provider_id": "provider",
        "model": "model",
        "source": "persona_semantic_smoke",
    }


def test_case_selection_requires_explicit_scope() -> None:
    module = _load_module()
    suite = {
        "single_turn_cases": [
            {"id": "B01", "type": "single_turn", "user_input": "one"},
        ],
        "multi_turn_scenarios": [
            {"id": "MT01", "type": "multi_turn", "turns": [{"user": "two"}]},
        ],
    }

    with pytest.raises(ValueError, match="at least one"):
        module._select_cases(suite, case_ids=[], run_all=False)
    assert [
        case["id"]
        for case in module._select_cases(
            suite,
            case_ids=["MT01"],
            run_all=False,
        )
    ] == ["MT01"]
    assert [
        case["id"]
        for case in module._select_cases(suite, case_ids=[], run_all=True)
    ] == ["B01", "MT01"]


def test_semantic_suite_is_loaded_from_package_without_extraction(
    tmp_path: Path,
) -> None:
    module = _load_module()
    package = tmp_path / "package.zip"
    expected = {
        "base_head": "abc",
        "single_turn_cases": [],
        "multi_turn_scenarios": [],
    }
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "professional-growth/evals/semantic_cases.json",
            json.dumps(expected),
        )

    assert module._load_semantic_cases(package) == expected


def test_semantic_suite_can_be_loaded_from_explicit_json(
    tmp_path: Path,
) -> None:
    module = _load_module()
    suite_path = tmp_path / "release-cases.json"
    expected = {
        "schema_version": 1,
        "single_turn_cases": [
            {"id": "B01-rewrite", "type": "single_turn", "user_input": "one"},
        ],
        "multi_turn_scenarios": [],
    }
    suite_path.write_text(json.dumps(expected), encoding="utf-8")

    assert module._load_semantic_cases_json(suite_path) == expected


def test_requested_prompt_arms_are_explicit_and_ordered() -> None:
    module = _load_module()

    assert module._selected_arm_names("both") == ("baseline", "candidate")
    assert module._selected_arm_names("candidate") == ("candidate",)
    assert module._selected_arm_names("baseline") == ("baseline",)


def test_prompt_arms_keep_historical_review_distinct_from_new_candidate() -> (
    None
):
    module = _load_module()
    review = json.loads(PROMPT_REVIEW.read_text(encoding="utf-8"))

    arms = module._build_prompt_arms(
        ROOT,
        base_ref=module.DEFAULT_BASE_REF,
        language="zh",
    )

    assert arms["baseline"].version == "1.5.3"
    assert arms["candidate"].version == "1.6.1"
    assert arms["baseline"].sha256 == (
        "7e7191b1cc952af4eddded6ff43382dde6ae0cf211f06471c889767ca516bd57"
    )
    assert review["schema"] == (
        "com.lantai.persona-profile-template-format-review.v1"
    )
    assert review["result"] == "local-source-passed"
    assert review["source_commit"] == (
        "4fc21861b4a4e28c1a1a8a385a725012174ec8e5"
    )
    assert review["prompt_arms"]["candidate"] == {
        "version": "1.6.0",
        "previous_sha256": (
            "b80b5136d50a883342fa06b7ff78bc51496a1f0cbbe00275307867562042390c"
        ),
        "sha256": (
            "a6f2f90d6a9127f909a1dd21198f20c28ae4fcddf357ac0a242ab681573252f2"
        ),
    }
    assert (
        arms["candidate"].sha256
        != review["prompt_arms"]["candidate"]["sha256"]
    )
    assert review["qualification"]["real_provider_multi_turn"] == (
        "NOT_RUN_REQUIRES_EXPLICIT_COST_APPROVAL"
    )
    assert arms["baseline"].prompt != arms["candidate"].prompt


def test_prompt_assembly_check_ignores_formatting_only_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    prompt_path = tmp_path / "src/lantai/agents/prompt.py"
    constitution_path = tmp_path / "src/lantai/agents/persona/constitution.py"
    prompt_path.parent.mkdir(parents=True)
    constitution_path.parent.mkdir(parents=True)
    prompt_path.write_text(
        'section = PromptSection(version="1.6.1", content="same")\n',
        encoding="utf-8",
    )
    constitution_path.write_text("VALUE = 1\n", encoding="utf-8")

    baselines = {
        "src/lantai/agents/prompt.py": (
            "section=PromptSection(\n"
            '    version="1.5.3", content="same"\n'
            ")  # historical formatting\n"
        ),
        "src/lantai/agents/persona/constitution.py": "VALUE=1\n",
    }
    monkeypatch.setattr(
        module,
        "_git_text",
        lambda _root, _ref, relative: baselines[relative],
    )

    module._assert_prompt_assembly_compatible(tmp_path, "baseline")


def test_prompt_assembly_check_rejects_semantic_changes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _load_module()
    prompt_path = tmp_path / "src/lantai/agents/prompt.py"
    constitution_path = tmp_path / "src/lantai/agents/persona/constitution.py"
    prompt_path.parent.mkdir(parents=True)
    constitution_path.parent.mkdir(parents=True)
    prompt_path.write_text("VALUE = 2\n", encoding="utf-8")
    constitution_path.write_text("SAME = True\n", encoding="utf-8")

    baselines = {
        "src/lantai/agents/prompt.py": "VALUE = 1\n",
        "src/lantai/agents/persona/constitution.py": "SAME = True\n",
    }
    monkeypatch.setattr(
        module,
        "_git_text",
        lambda _root, _ref, relative: baselines[relative],
    )

    with pytest.raises(RuntimeError, match="prompt.py"):
        module._assert_prompt_assembly_compatible(tmp_path, "baseline")


def test_arm_order_is_deterministic_and_balanced_by_case() -> None:
    module = _load_module()

    first = module._arm_order("B01", 1)
    second = module._arm_order("B01", 1)

    assert first == second
    assert sorted(first) == ["baseline", "candidate"]


def test_provider_terminal_error_is_captured_instead_of_raised(
    monkeypatch,
) -> None:
    module = _load_module()

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter(
                io.BytesIO(
                    b'{"type":"error","error":"no visible answer"}\n',
                ).readlines(),
            )

    monkeypatch.setattr(
        module,
        "urlopen",
        lambda *_args, **_kwargs: _Response(),
    )

    result = module._request_office_stream(
        "http://127.0.0.1:1234",
        desktop_token="secret",
        payload={},
        timeout=1.0,
    )

    assert result["status"] == "ERROR"
    assert result["error"] == "no visible answer"
    assert result["text"] == ""


def test_model_binding_defaults_to_effective_active_model() -> None:
    module = _load_module()

    provider_id, model, source = module._select_model_binding(
        {"active_llm": {"provider_id": "kimi-codingplan", "model": "kimi"}},
        [],
        requested_provider_id="",
        requested_model="",
    )

    assert (provider_id, model, source) == (
        "kimi-codingplan",
        "kimi",
        "effective_active_model",
    )


def test_explicit_model_binding_requires_configured_catalog_entry() -> None:
    module = _load_module()
    providers = [
        {
            "id": "deepseek",
            "api_key": "******",
            "require_api_key": True,
            "models": [
                {
                    "id": "deepseek-chat",
                    "show_in_chat": True,
                    "supported_protocols": ["openai-chat"],
                },
            ],
            "extra_models": [],
        },
    ]

    assert module._select_model_binding(
        {},
        providers,
        requested_provider_id="deepseek",
        requested_model="deepseek-chat",
    ) == ("deepseek", "deepseek-chat", "explicit_verified_override")

    with pytest.raises(ValueError, match="Both"):
        module._select_model_binding(
            {},
            providers,
            requested_provider_id="deepseek",
            requested_model="",
        )
    with pytest.raises(ValueError, match="not configured"):
        module._select_model_binding(
            {},
            providers,
            requested_provider_id="missing",
            requested_model="model",
        )
    with pytest.raises(ValueError, match="not an available chat model"):
        module._select_model_binding(
            {},
            providers,
            requested_provider_id="deepseek",
            requested_model="missing",
        )


def test_binding_rejects_provider_without_required_credential() -> None:
    module = _load_module()

    with pytest.raises(ValueError, match="no configured credential"):
        module._select_model_binding(
            {},
            [
                {
                    "id": "deepseek",
                    "api_key": "",
                    "require_api_key": True,
                    "models": [{"id": "deepseek-chat", "show_in_chat": True}],
                },
            ],
            requested_provider_id="deepseek",
            requested_model="deepseek-chat",
        )
