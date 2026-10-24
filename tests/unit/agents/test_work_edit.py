from lantai.agents.work_edit import build_work_edit_prompt
import pytest


def test_work_edit_prompt_is_only_enabled_for_work_sessions() -> None:
    assert build_work_edit_prompt({}) == ""

    prompt = build_work_edit_prompt(
        {
            "work_edit_context": (
                "作品：迷你马里奥\n"
                "主文件：/tmp/work/index.html"
            ),
        },
    )

    assert "无需回读原会话" in prompt
    assert "主文件：/tmp/work/index.html" in prompt
    assert "先检查主文件及相关资源" in prompt


def test_current_chat_artifact_target_keeps_canonical_conversation_context() -> None:
    prompt = build_work_edit_prompt(
        {
            "work_edit_context": (
                "【当前会话成果修改目标】\n"
                "成果：活动方案.pptx\n"
                "当前版本：V2\n"
                "选中范围：第 3 页"
            ),
        },
    )

    assert "继续沿用当前对话上下文" in prompt
    assert "第 3 页" in prompt
    assert "无需回读原会话" not in prompt
    assert "已绑定的可编辑副本" in prompt


@pytest.mark.parametrize("trusted", [False, True])
def test_long_private_editor_records_preserve_the_last_author_decision_only_for_a_trusted_binding(
    trusted: bool,
) -> None:
    last_decision = "- record-final [accepted/user_explicit] 保留作者最后确认的结尾判断。"
    context = "【私人编辑实验作品绑定】\n" + "历史作品记录。\n" * 1300 + last_decision
    request = {
        "work_edit_context": context,
        "work_method_id": "private_editor",
        "_trusted_private_editor_binding": trusted,
        "private_editor_project_id": "project-long",
        "private_editor_original_work_id": "work-original",
        "private_editor_current_work_id": "work-current",
        "private_editor_current_digest": "sha256:current",
        "private_editor_record_revision": 1301,
        "private_editor_revocation_epoch": 0,
        "private_editor_project_status": "active",
    }

    prompt = build_work_edit_prompt(request)

    if trusted:
        assert context in prompt
        assert last_decision in prompt
        assert "当前目录是独立修改分支" not in prompt
    else:
        assert last_decision not in prompt
        assert context[:8000] in prompt
