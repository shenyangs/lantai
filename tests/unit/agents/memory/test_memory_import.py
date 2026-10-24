# -*- coding: utf-8 -*-

import json

import pytest

from lantai.agents.memory.canonical_ledger import CanonicalMemoryLedger
from lantai.agents.memory.memory_import import (
    MemoryImportConflictError,
    commit_memory_import,
    get_memory_import_prompt,
    get_recent_memory_import,
    preview_memory_import,
    undo_memory_import,
)
from lantai.agents.memory.structured_user_memory import StructuredUserMemoryStore


def _sample_export() -> str:
    return """```json
{
  "memories": [
    {
      "type": "style",
      "content": "用户长期偏好简洁、自然、克制的中文表达"
    },
    {
      "type": "instruction",
      "content": "用户希望以后不要机械地用有事直接说收尾"
    },
    {
      "type": "workflow",
      "content": "请分析这次截图并写一份报告"
    },
    {
      "type": "identity",
      "content": "用户的身份证是 123456"
    }
  ]
}
```"""


def test_import_prompt_uses_detailed_provider_neutral_profile_contract():
    payload = get_memory_import_prompt()
    prompt = payload["prompt"]

    assert "个人使用画像" in prompt
    assert "【基本信息】" in prompt
    assert "【工作背景】" in prompt
    assert "【个人背景】" in prompt
    assert "【协作偏好】" in prompt
    assert "【长期记忆】" in prompt
    assert "缺失的信息统一写「无」" in prompt
    assert "全程以「用户」指代" in prompt
    assert "只输出纯文本" in prompt
    assert "JSON" not in prompt


def test_preview_parses_plain_text_profile_and_skips_missing_fields(tmp_path):
    preview = preview_memory_import(
        tmp_path,
        text="""【基本信息】
称呼/别名：阿云
语言偏好：中文
地区/时区：Asia/Shanghai

【工作背景】
职位与职责：负责产品设计与前端体验
所在公司/团队：无
核心专业技能：交互设计；React

【个人背景】
家庭/人际关系：无
兴趣爱好：摄影；徒步
个人项目：长期维护个人知识库

【协作偏好】
回复风格：偏好简洁、自然、克制的中文表达
常设规则或指令：执行破坏性操作前必须确认
不喜欢或需要纠正之处：不喜欢机械重复结论

【长期记忆】
稳定行为模式或重要事项：习惯先核对证据，再推进实施
""",
        provider="其他 AI",
        agent_id="default",
    )

    contents = [item["content"] for item in preview["candidates"]]
    types = {item["type"] for item in preview["candidates"]}

    assert any("阿云" in item for item in contents)
    assert any("Asia/Shanghai" in item for item in contents)
    assert any("执行破坏性操作前必须确认" in item for item in contents)
    assert not any("：无" in item for item in contents)
    assert {"identity", "workflow", "style", "instruction"} <= types


def test_import_preserves_declared_field_ownership_and_unknown_wording(tmp_path):
    preview = preview_memory_import(tmp_path, text="职位与职责：担任编辑，负责选题\n个人项目：HUMAN API 提供开放接口\n经历说明：担任编辑，也喜欢摄影", agent_id="default")
    items = preview["candidates"]
    career = next(item for item in items if "负责选题" in item["content"])
    project = next(item for item in items if "HUMAN API" in item["content"])
    unknown = next(item for item in items if "摄影" in item["content"])
    assert career["type"] == "identity"
    assert project["subject"] == "project"
    assert project["scope"] == "project"
    assert unknown["subject"] == "unknown"
    assert "长期偏好" not in unknown["content"]
    assert unknown["selected_by_default"] is False


def test_json_import_round_trips_explicit_project_subject(tmp_path):
    preview = preview_memory_import(tmp_path, text='{"memories":[{"type":"identity","content":"HUMAN API 提供开放接口","subject":"project","scope":"project","scope_id":"human-api"}]}', agent_id="default")
    candidate = preview["candidates"][0]
    result = commit_memory_import(tmp_path, batch_id=preview["batch_id"], selected_ids=[candidate["id"]], agent_id="default")
    entry = StructuredUserMemoryStore(tmp_path).load_entries()[0]
    assert entry.subject == "project"
    assert entry.scope_id == "human-api"


def test_json_import_does_not_promote_project_specific_user_preference(tmp_path):
    preview = preview_memory_import(tmp_path, text='{"memories":[{"type":"style","content":"用户偏好简洁中文","subject":"user","scope":"project","scope_id":"atlas"}]}', agent_id="default")
    candidate = preview["candidates"][0]
    assert candidate["scope"] == "project"


def test_same_text_in_separate_import_scopes_is_not_a_duplicate(tmp_path):
    text = json.dumps({"memories": [{"type": "style", "content": "采用简洁中文表达", "subject": "project", "scope": "project", "scope_id": scope} for scope in ("a", "b")]})
    preview = preview_memory_import(tmp_path, text=text, agent_id="default")
    assert len(preview["candidates"]) == 2
    commit_memory_import(tmp_path, batch_id=preview["batch_id"], selected_ids=[item["id"] for item in preview["candidates"]], agent_id="default")
    assert len(StructuredUserMemoryStore(tmp_path).load_entries()) == 2
    third = preview_memory_import(tmp_path, text=text.replace('"b"', '"c"'), agent_id="default")
    assert [item["bucket"] for item in third["candidates"]] == ["duplicate", "review"]


def test_preview_filters_low_value_and_does_not_store_raw_text(tmp_path):
    preview = preview_memory_import(
        tmp_path,
        text=_sample_export(),
        provider="ChatGPT",
        agent_id="default",
    )

    assert preview["stats"] == {
        "recommended": 2,
        "review": 0,
        "duplicate": 0,
        "rejected": 2,
    }
    assert preview["raw_stored"] is False
    batch_path = tmp_path / "memory-imports" / f"{preview['batch_id']}.json"
    batch_text = batch_path.read_text(encoding="utf-8")
    assert "123456" not in batch_text  # rejected sensitive values are redacted
    assert "```json" not in batch_text  # pasted source document is discarded
    payload = json.loads(batch_text)
    assert payload["raw_stored"] is False
    assert len(payload["raw_sha256"]) == 64


def test_commit_marks_provenance_and_batch_can_be_undone(tmp_path):
    preview = preview_memory_import(
        tmp_path,
        text=_sample_export(),
        provider="Claude",
        agent_id="default",
    )
    selected_ids = [
        item["id"]
        for item in preview["candidates"]
        if item["selected_by_default"]
    ]

    result = commit_memory_import(
        tmp_path,
        batch_id=preview["batch_id"],
        selected_ids=selected_ids,
        agent_id="default",
    )

    assert result["imported_count"] == 2
    entries = StructuredUserMemoryStore(tmp_path).load_entries()
    assert len(entries) == 2
    assert all(entry.confirmed for entry in entries)
    assert all(entry.source_kind == "import" for entry in entries)
    assert all(entry.source_title == "来自 Claude 的记忆" for entry in entries)
    assert CanonicalMemoryLedger(tmp_path).list_pending() == []
    stored_batch = json.loads(
        (
            tmp_path
            / "memory-imports"
            / f"{preview['batch_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert stored_batch["candidates"] == []
    assert all("content" not in item for item in stored_batch["imported"])
    recent = get_recent_memory_import(tmp_path, agent_id="default")["batch"]
    assert recent["can_undo"] is True

    undone = undo_memory_import(
        tmp_path,
        batch_id=preview["batch_id"],
        agent_id="default",
    )
    assert undone["removed_count"] == 2
    assert StructuredUserMemoryStore(tmp_path).load_entries() == []
    active_facts = CanonicalMemoryLedger(tmp_path).list_facts(
        statuses={"confirmed", "pending", "observed", "conflict"}
    )
    assert active_facts == []


def test_reviewed_project_import_keeps_project_scope_in_both_stores(tmp_path):
    preview = preview_memory_import(
        tmp_path,
        text="""{
          "memories": [{
            "type": "workflow",
            "content": "用户长期在当前项目使用 pytest 做回归测试"
          }]
        }""",
        provider="Claude",
        agent_id="project-agent",
    )
    candidate = preview["candidates"][0]
    assert candidate["scope"] == "project"
    assert candidate["selectable"] is True

    commit_memory_import(
        tmp_path,
        batch_id=preview["batch_id"],
        selected_ids=[candidate["id"]],
        agent_id="project-agent",
    )

    entry = StructuredUserMemoryStore(tmp_path).load_entries()[0]
    fact = CanonicalMemoryLedger(tmp_path).confirmed_facts()[0]
    assert entry.scope == "project"
    assert fact.scope == "project"
    assert entry.scope_id == "workspace:project-agent"
    assert fact.scope_id == "workspace:project-agent"


def test_undo_refuses_to_overwrite_a_post_import_edit(tmp_path):
    preview = preview_memory_import(
        tmp_path,
        text=_sample_export(),
        provider="Gemini",
        agent_id="default",
    )
    selected_id = next(
        item["id"]
        for item in preview["candidates"]
        if item["selected_by_default"]
    )
    result = commit_memory_import(
        tmp_path,
        batch_id=preview["batch_id"],
        selected_ids=[selected_id],
        agent_id="default",
    )
    entry_id = result["imported"][0]["entry_id"]
    StructuredUserMemoryStore(tmp_path).update_entry(
        entry_id,
        content="用户长期偏好更完整但仍然克制的中文表达。",
    )

    with pytest.raises(MemoryImportConflictError):
        undo_memory_import(
            tmp_path,
            batch_id=preview["batch_id"],
            agent_id="default",
        )
