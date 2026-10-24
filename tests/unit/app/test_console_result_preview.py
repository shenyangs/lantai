from lantai.app.routers.console import (
    _assistant_result_preview,
    _extract_pending_user_text,
)


def test_assistant_result_preview_compacts_markdown_and_whitespace():
    preview = _assistant_result_preview(
        "## 完成\n\n- 已修复 **三个** 问题\n- [查看报告](https://example.com)"
    )

    assert preview == "完成 已修复 三个 问题 查看报告"


def test_assistant_result_preview_is_bounded():
    preview = _assistant_result_preview("一" * 30, max_chars=12)

    assert preview == ("一" * 11) + "…"


def test_pending_user_text_hides_attachment_role_metadata():
    text = _extract_pending_user_text(
        [
            {"type": "text", "text": "请优化原稿"},
            {
                "type": "text",
                "text": (
                    '<lantai_attachment_roles>\n[{"name":"原稿.docx",'
                    '"role":"edit_target"}]\n</lantai_attachment_roles>'
                ),
            },
        ]
    )

    assert text == "请优化原稿"
