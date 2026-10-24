# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lantai.app.kdocs_documents import KdocsDocumentError, KdocsDocumentService


class _FakeCli:
    def __init__(self, executable: Path, responses: list[dict]) -> None:
        self.executable = executable
        self.responses = list(responses)
        self.commands: list[list[str]] = []

    def run(
        self,
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        payload = self.responses.pop(0)
        return subprocess.CompletedProcess(
            command,
            int(payload.pop("returncode", 0)),
            json.dumps(payload, ensure_ascii=False),
            "",
        )


def _service(tmp_path: Path, responses: list[dict]) -> tuple[KdocsDocumentService, _FakeCli]:
    executable = tmp_path / "kdocs-cli"
    executable.touch()
    fake = _FakeCli(executable, responses)
    return (
        KdocsDocumentService(
            command_runner=fake.run,
            cli_finder=lambda: executable,
        ),
        fake,
    )


def test_list_root_normalizes_direct_cli_shape_and_stable_identity(tmp_path: Path) -> None:
    service, _fake = _service(
        tmp_path,
        [
            {
                "code": 0,
                "data": {
                    "drive_id": "drive-a",
                    "parent_id": "0",
                    "next_page_token": "next-root",
                    "items": [
                        {
                            "id": "same-id",
                            "drive_id": "drive-a",
                            "parent_id": "0",
                            "name": "项目资料",
                            "type": "folder",
                            "mtime": 1710000000,
                            "created_by": {"id": "u1", "name": "沈行知"},
                        },
                        {
                            "id": "same-id",
                            "drive_id": "drive-b",
                            "parent_id": "0",
                            "name": "项目资料.docx",
                            "type": "docx",
                            "version": 9,
                            "hash": "sha256-doc",
                            "link_url": "https://www.kdocs.cn/l/example",
                        },
                    ],
                },
                "message": "ok",
            }
        ],
    )

    result = service.list_documents(view="my", page_size=50)

    assert result["next_page_token"] == "next-root"
    assert result["parent"] == {"drive_id": "drive-a", "file_id": "0"}
    assert [item["key"] for item in result["items"]] == [
        "drive-a:same-id",
        "drive-b:same-id",
    ]
    assert result["items"][0]["is_folder"] is True
    assert result["items"][0]["owner"] == {"id": "u1", "name": "沈行知"}
    assert result["items"][1]["version"] == "9"


def test_permission_preview_is_exposed_as_read_access(tmp_path: Path) -> None:
    service, _fake = _service(
        tmp_path,
        [
            {
                "code": 0,
                "data": {
                    "drive_id": "drive-a",
                    "items": [
                        {
                            "id": "file-a",
                            "name": "只读资料.docx",
                            "type": "docx",
                            "permission": {"preview": True, "update": False},
                        }
                    ],
                },
            }
        ],
    )

    result = service.list_documents(view="my")

    assert result["items"][0]["permissions"] == {
        "can_read": True,
        "can_edit": False,
    }


def test_recent_and_search_unwrap_nested_cli_shape(tmp_path: Path) -> None:
    service, fake = _service(
        tmp_path,
        [
            {
                "code": 0,
                "data": {
                    "code": 0,
                    "data": {
                        "next_page_token": "next-recent",
                        "items": [
                            {
                                "file": {
                                    "id": "file-1",
                                    "drive_id": "drive-1",
                                    "name": "周报.docx",
                                    "type": "docx",
                                },
                                "file_src": {"name": "我的云文档", "type": "personal"},
                            }
                        ],
                    },
                    "msg": "ok",
                },
            },
            {
                "code": 0,
                "data": {
                    "code": 0,
                    "data": {"items": [], "next_page_token": ""},
                },
            },
        ],
    )

    recent = service.list_documents(view="recent", page_size=20)
    search = service.list_documents(view="search", query="周报", page_size=20)

    assert recent["items"][0]["location"] == "我的云文档"
    assert recent["next_page_token"] == "next-recent"
    search_args = json.loads(fake.commands[1][3])
    assert fake.commands[1][1:3] == ["drive", "search-files"]
    assert search_args == {
        "keyword": "周报",
        "page_size": 20,
        "type": "file_name",
        "with_drive": True,
        "with_link": True,
        "with_permission": True,
    }


def test_list_folder_keeps_drive_and_pagination_parameters(tmp_path: Path) -> None:
    service, fake = _service(
        tmp_path,
        [{"code": 0, "data": {"items": [], "next_page_token": ""}}],
    )

    service.list_documents(
        view="folder",
        drive_id="drive-a",
        parent_id="folder-b",
        page_token="cursor-c",
        page_size=500,
    )

    args = json.loads(fake.commands[0][3])
    assert fake.commands[0][1:3] == ["drive", "list-files"]
    assert args["drive_id"] == "drive-a"
    assert args["parent_id"] == "folder-b"
    assert args["page_token"] == "cursor-c"
    assert args["page_size"] == 100


def test_file_info_uses_drive_from_wrapped_response(tmp_path: Path) -> None:
    service, _fake = _service(
        tmp_path,
        [
            {
                "code": 0,
                "data": {
                    "drive": {"id": "drive-a"},
                    "file": {
                        "id": "file-a",
                        "name": "方案.docx",
                        "type": "docx",
                    },
                },
            }
        ],
    )

    result = service.file_info(file_id="file-a")

    assert result["key"] == "drive-a:file-a"


def test_read_document_reports_active_task_and_bounds_large_content(tmp_path: Path) -> None:
    service, fake = _service(
        tmp_path,
        [
            {"code": 0, "data": {"status": "running", "task_id": "task-1"}},
            {
                "code": 0,
                "data": {
                    "status": "done",
                    "name": "长文档.docx",
                    "content_format": "markdown",
                    "content": "甲" * 40,
                    "warnings": ["表格只返回了首屏"],
                },
            },
        ],
    )

    pending = service.read_document(file_id="file-1")
    complete = service.read_document(
        file_id="file-1",
        task_id="task-1",
        max_chars=16,
    )

    assert pending == {
        "status": "running",
        "task_id": "task-1",
        "content": "",
        "content_format": "",
        "name": "",
        "truncated": False,
        "warnings": [],
    }
    assert complete["content"] == "甲" * 16
    assert complete["truncated"] is True
    assert complete["warnings"] == ["表格只返回了首屏"]
    read_args = json.loads(fake.commands[1][3])
    assert read_args["task_id"] == "task-1"
    assert read_args["enable_upload_medias"] is False


def test_read_document_omits_embedded_image_payloads_before_bounding_content(
    tmp_path: Path,
) -> None:
    markdown_image = "![image](data:image/png;base64," + ("A" * 400) + ")"
    html_image = '<img alt="chart" src="data:image/jpeg;base64,' + ("B" * 400) + '">'
    service, _fake = _service(
        tmp_path,
        [
            {
                "code": 0,
                "data": {
                    "status": "done",
                    "content_format": "markdown",
                    "content": f"开头\n{markdown_image}\n{html_image}\n结尾",
                },
            }
        ],
    )

    result = service.read_document(file_id="file-with-images", max_chars=64)

    assert result["content"] == "开头\n[图片]\n[图片]\n结尾"
    assert result["truncated"] is False
    assert "data:image" not in result["content"]
    assert result["warnings"] == ["已省略文档中的内嵌图片数据。"]


@pytest.mark.parametrize(
    ("code", "expected_code", "status_code"),
    [
        (400006, "authentication_required", 401),
        (403, "permission_denied", 403),
        (404, "not_found", 404),
        (429, "rate_limited", 429),
        (429001, "rate_limited", 429),
        (429002, "rate_limited", 429),
    ],
)
def test_cli_errors_have_recoverable_public_codes(
    tmp_path: Path,
    code: int,
    expected_code: str,
    status_code: int,
) -> None:
    service, _fake = _service(
        tmp_path,
        [{"code": code, "message": "private provider detail", "data": {}}],
    )

    with pytest.raises(KdocsDocumentError) as exc_info:
        service.list_documents(view="my")

    assert exc_info.value.code == expected_code
    assert exc_info.value.status_code == status_code
    assert "private provider detail" not in exc_info.value.public_message


def test_nonzero_cli_exit_preserves_structured_provider_error(tmp_path: Path) -> None:
    service, _fake = _service(
        tmp_path,
        [
            {
                "returncode": 1,
                "code": 403,
                "message": "private permission detail",
                "data": {},
            }
        ],
    )

    with pytest.raises(KdocsDocumentError) as exc_info:
        service.list_documents(view="my")

    assert exc_info.value.code == "permission_denied"
    assert exc_info.value.status_code == 403
    assert "private permission detail" not in exc_info.value.public_message


def test_read_preserves_upstream_truncation_and_image_limits(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, [{"code": 0, "data": {
        "content": "正文 ![图](https://example.com/image.png)", "truncated": True,
    }}])
    result = service.read_document(file_id="file-a")
    assert result["truncated"] is True
    assert result["coverage"]["body"] == "partial"
    assert "images" in result["coverage"]["limitations"]


def test_local_continuation_uses_same_snapshot_and_rejects_other_file(tmp_path: Path) -> None:
    service, fake = _service(tmp_path, [{"code": 0, "data": {"content": "abcdefghij"}}])
    first = service.read_document(file_id="a", max_chars=4)
    assert first["continuation"]
    with pytest.raises(KdocsDocumentError):
        service.read_document(file_id="b", continuation=first["continuation"])
    second = service.read_document(file_id="a", continuation=first["continuation"], max_chars=4)
    third = service.read_document(file_id="a", continuation=second["continuation"], max_chars=4)
    assert first["content"] + second["content"] + third["content"] == "abcdefghij"
    assert third["continuation"] == ""
    assert third["coverage"]["body"] == "complete"
    assert len(fake.commands) == 1


def test_cached_snapshots_are_scoped_and_invalidated_on_connection_change():
    from lantai.app.kdocs_read_cache import cache_generation, clear_read_cache, read_page, store_read
    old_generation = cache_generation()
    result = {"content": "abcdef", "truncated": False, "coverage": {"body": "complete", "limitations": []}}
    token = store_read("workspace-a", "file-a", result, old_generation)
    assert read_page("workspace-b", "file-a", f"{token}:0", 3) is None
    assert read_page("workspace-a", "file-a", f"{token}:0", 3)["content"] == "abc"
    clear_read_cache()
    assert read_page("workspace-a", "file-a", f"{token}:0", 3) is None
    assert store_read("workspace-a", "file-a", result, old_generation) == ""
