from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lantai.runtime.loops import LoopContract, compute_loop_contract_hash
from lantai.work_methods import (
    WorkMethodDraft,
    WorkMethodRepository,
    compile_work_method_contract,
    get_builtin_work_methods,
    render_work_method_prompt,
)
from lantai.app.private_editor import PrivateEditorStore
from lantai.runtime.work_method_resolution import (
    effective_work_method_id_from_metadata,
)


def test_builtin_work_methods_compile_to_hashed_loop_contracts():
    methods = get_builtin_work_methods()

    assert [method.id for method in methods] == [
        "standard",
        "deep_research",
        "research_delivery",
        "sustained_execution",
        "collaboration",
        "private_editor",
    ]
    assert all(method.built_in for method in methods)

    contract = compile_work_method_contract(
        methods[1],
        objective="比较三个中文搜索信源并给出证据充分的建议",
    )

    assert isinstance(contract, LoopContract)
    assert contract.contract_id.startswith("work-method:deep_research:")
    assert contract.contract_hash == compute_loop_contract_hash(contract)
    assert contract.budgets.max_steps is None
    assert contract.budgets.max_tool_calls is None
    assert contract.budgets.max_wall_seconds is None
    assert contract.acceptance == methods[1].acceptance


def test_work_method_prompt_is_resolved_by_id_from_workspace(tmp_path: Path):
    repository = WorkMethodRepository(tmp_path)
    repository.upsert_sync(
        "editorial_review",
        WorkMethodDraft(
            name="编辑审校",
            summary="先核事实，再统一表达。",
            instructions=["列出可核验事实", "标记证据不足处", "完成语言审校"],
            scope=["事实核验", "结构与表达"],
            acceptance=["事实有来源", "结论与证据一致"],
            max_steps=12,
            max_tool_calls=30,
        ),
    )

    prompt = render_work_method_prompt(
        "editorial_review",
        workspace_dir=tmp_path,
    )

    assert "工作法：编辑审校" in prompt
    assert "列出可核验事实" in prompt
    assert "必须实际执行并满足下列验收" in prompt
    assert "不得用普通回答代替" in prompt
    assert "不要越过当前权限" in prompt


def test_standard_work_method_does_not_taskify_ordinary_chat(tmp_path: Path):
    assert render_work_method_prompt("standard", workspace_dir=tmp_path) == ""


@pytest.mark.asyncio
async def test_custom_work_methods_persist_and_cannot_shadow_builtins(
    tmp_path: Path,
):
    repository = WorkMethodRepository(tmp_path)
    saved = await repository.upsert(
        "weekly_review",
        WorkMethodDraft(
            name="周复盘",
            summary="汇总结果、偏差与下一步。",
            instructions=["收集本周结果", "识别偏差", "提出下一步"],
            scope=["复盘"],
            acceptance=["包含结果、偏差和下一步"],
            max_steps=10,
            max_tool_calls=20,
        ),
    )

    reloaded = await WorkMethodRepository(tmp_path).get("weekly_review")
    assert reloaded == saved
    assert reloaded is not None and reloaded.built_in is False

    with pytest.raises(ValueError, match="built-in"):
        await repository.upsert(
            "standard",
            WorkMethodDraft(
                name="覆盖默认",
                summary="不允许",
                instructions=["覆盖"],
                scope=["全部"],
                acceptance=["完成"],
            ),
        )

    assert await repository.delete("weekly_review") is True
    assert await repository.get("weekly_review") is None


@pytest.mark.asyncio
async def test_work_method_router_supports_catalog_crud_and_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    router_module = importlib.import_module("lantai.app.routers.work_methods")
    repository = WorkMethodRepository(tmp_path)

    async def fake_repository(_request):
        return repository

    monkeypatch.setattr(router_module, "_repository", fake_repository)
    app = FastAPI()
    app.include_router(router_module.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        catalog = await client.get("/work-methods")
        assert catalog.status_code == 200
        assert [item["id"] for item in catalog.json()] == [
            "standard",
            "deep_research",
            "research_delivery",
            "sustained_execution",
            "collaboration",
        ]

        saved = await client.put(
            "/work-methods/editorial_review",
            json={
                "name": "编辑审校",
                "summary": "先核事实，再统一表达。",
                "instructions": ["列出可核验事实", "完成语言审校"],
                "scope": ["事实核验", "结构与表达"],
                "acceptance": ["事实有来源", "结论与证据一致"],
                "max_steps": 12,
                "max_tool_calls": 30,
                "max_wall_seconds": 900,
                "max_no_progress_iterations": 3,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["id"] == "editorial_review"

        compiled = await client.post(
            "/work-methods/editorial_review/contract",
            json={"objective": "审校这份产品说明"},
        )
        assert compiled.status_code == 200
        assert compiled.json()["contract_hash"]

        deleted = await client.delete("/work-methods/editorial_review")
        assert deleted.status_code == 204
        missing = await client.post(
            "/work-methods/editorial_review/contract",
            json={"objective": "不会执行"},
        )
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_private_editor_work_method_is_hidden_until_server_flag_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    router_module = importlib.import_module("lantai.app.routers.work_methods")
    repository = WorkMethodRepository(tmp_path)

    async def fake_repository(_request):
        return repository

    monkeypatch.setattr(router_module, "_repository", fake_repository)
    app = FastAPI()
    app.include_router(router_module.router)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        disabled = await client.get("/work-methods")
        assert "private_editor" not in [item["id"] for item in disabled.json()]

        PrivateEditorStore(tmp_path).update_settings(enabled=True)
        enabled = await client.get("/work-methods")
        assert "private_editor" in [item["id"] for item in enabled.json()]


def test_private_editor_requires_explicit_selection_and_does_not_add_loop_budgets(
    tmp_path: Path,
) -> None:
    method = next(
        item for item in get_builtin_work_methods() if item.id == "private_editor"
    )
    PrivateEditorStore(tmp_path).update_settings(enabled=True)
    prompt = render_work_method_prompt("private_editor", workspace_dir=tmp_path)
    contract = compile_work_method_contract(method, objective="继续修改同一篇文章")

    assert "先理解作品意图" in prompt
    assert "模型推测" in prompt
    assert "当前稿未变" in prompt
    assert "找一个新切口" in prompt
    assert "只使用当前作品已授权材料" in prompt
    assert "默认不联网、不跨项目、不创建定时任务" in prompt
    assert "材料没有新价值时" in prompt
    assert "空结果" in prompt
    for proposal_field in (
        "方向",
        "为何值得做",
        "材料依据",
        "与旧稿差别",
        "下一步最小动作",
        "待核实事项",
    ):
        assert proposal_field in prompt
    assert "上下文不足" in prompt
    assert "已读范围" in prompt
    assert "未读范围" in prompt
    assert contract.budgets.max_steps is None
    assert contract.budgets.max_tool_calls is None
    assert contract.budgets.max_wall_seconds is None

    explicit = effective_work_method_id_from_metadata(
        {"task_contract": {"work_method_id": "private_editor"}}
    )
    model_suggested = effective_work_method_id_from_metadata(
        {
            "task_contract": {"work_method_id": "standard"},
            "turn_intelligence": {
                "status": "classified",
                "confidence": 1.0,
                "requested_work_method_id": "private_editor",
            },
        }
    )
    assert explicit == "private_editor"
    assert model_suggested == "standard"
