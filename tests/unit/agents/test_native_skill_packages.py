from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import frontmatter

from lantai.agents.skills_manager import _validate_skill_content


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = PROJECT_ROOT / "src" / "lantai" / "agents" / "skills"

NEW_SKILLS = {
    "agent-designer-zh": "agent-designer",
    "agent-run-doctor-zh": "agent-run-doctor",
    "github-pr-ci-zh": "github-pr-ci",
    "mcp-integration-builder-zh": "mcp-integration-builder",
    "image-table-extractor-zh": "image-table-extractor",
    "web-app-builder-zh": "web-app-builder",
    "skill-creator-zh": "skill-creator",
    "skill-health-zh": "skill-health",
    "content-publisher-zh": "content-publisher",
    "geo-growth-zh": "geo-growth",
    "browser-testing-zh": "browser-testing",
    "data-visualization-zh": "data-visualization",
    "app-icon-designer-zh": "app-icon-designer",
}

OFFICE_IMPORTS = {
    "pandas",
    "openpyxl",
    "pypdf",
    "pdfplumber",
    "pypdfium2",
    "reportlab",
    "docx",
    "pptx",
    "markitdown",
}


def test_native_skill_packages_validate_and_have_no_placeholders() -> None:
    for folder, expected_name in NEW_SKILLS.items():
        skill_md = SKILLS_ROOT / folder / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        name, description = _validate_skill_content(content)
        assert name == expected_name
        assert len(description) >= 30
        assert "[TODO" not in content
        assert not re.search(r"(?im)^\s*TODO(?:\s|:)", content)


def test_collaboration_skills_keep_specialist_consultation_optional() -> None:
    collaboration = (
        SKILLS_ROOT / "multi_agent_collaboration-en" / "SKILL.md"
    ).read_text(encoding="utf-8")
    direct_chat = (SKILLS_ROOT / "chat_with_agent-en" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "consultation is optional" in collaboration
    assert "prefer one quick specialist consultation" not in collaboration
    assert "does not by itself require delegation" in direct_chat


def test_pptxgenjs_is_an_exact_bundled_desktop_dependency() -> None:
    package = json.loads(
        (PROJECT_ROOT / "console" / "package.json").read_text(encoding="utf-8")
    )

    assert package["dependencies"]["pptxgenjs"] == "4.0.1"
    packaged_files = package["build"]["files"]
    assert "node_modules/pptxgenjs/**/*" in packaged_files


def test_native_skill_relative_markdown_links_resolve() -> None:
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    for folder in NEW_SKILLS:
        for markdown in (SKILLS_ROOT / folder).rglob("*.md"):
            content = markdown.read_text(encoding="utf-8")
            for raw_target in link_pattern.findall(content):
                target = raw_target.split("#", 1)[0].strip().strip("<>")
                if not target or target.startswith(
                    ("http://", "https://", "mailto:", "data:")
                ):
                    continue
                assert (markdown.parent / target).resolve().exists(), (
                    f"broken link in {markdown}: {raw_target}"
                )


def test_skill_health_scanner_reports_risks_without_secret_values(
    tmp_path: Path,
) -> None:
    risky = tmp_path / "risky"
    risky.mkdir()
    secret_value = "do-not-print-this-secret"
    (risky / "SKILL.md").write_text(
        (
            "---\n"
            "name: risky\n"
            "description: 检查一个存在平台依赖和断链的测试技能包。\n"
            "---\n"
            "# Risky\n"
            "Use `.wpscomate` and [missing](references/missing.md).\n"
            f'api_key = "{secret_value}"\n'
        ),
        encoding="utf-8",
    )
    scanner = SKILLS_ROOT / "skill-health-zh" / "scripts" / "scan_skills.py"
    result = subprocess.run(
        [
            sys.executable,
            str(scanner),
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    rules = {finding["rule"] for finding in payload["findings"]}
    assert {"wpscomate-path", "broken-link", "embedded-secret"} <= rules
    assert secret_value not in result.stdout


def test_skill_health_scanner_allows_localized_name_pairs(tmp_path: Path) -> None:
    for locale in ("en", "zh"):
        skill_dir = tmp_path / f"reader-{locale}"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            (
                "---\n"
                "name: reader\n"
                f"description: localized reader workflow for {locale} documents\n"
                "---\n"
                "# Reader\n"
            ),
            encoding="utf-8",
        )
    scanner = SKILLS_ROOT / "skill-health-zh" / "scripts" / "scan_skills.py"
    result = subprocess.run(
        [
            sys.executable,
            str(scanner),
            "--root",
            str(tmp_path),
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert not [
        finding
        for finding in payload["findings"]
        if finding["rule"] == "duplicate-name"
    ]


def test_agent_run_doctor_trace_analyzer_redacts_and_summarizes(
    tmp_path: Path,
) -> None:
    trace = tmp_path / "trace.jsonl"
    secret_value = "never-print-this-token"
    trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-08-12T10:00:00Z",
                        "type": "tool_call",
                        "tool_name": "wiki_search",
                        "api_token": secret_value,
                    },
                ),
                json.dumps(
                    {
                        "timestamp": "2026-08-12T10:01:00Z",
                        "type": "tool_call",
                        "tool_name": "wiki_search",
                        "status": "failed",
                        "error": "timeout",
                    },
                ),
            ],
        ),
        encoding="utf-8",
    )
    analyzer = SKILLS_ROOT / "agent-run-doctor-zh" / "scripts" / "analyze_trace.py"
    result = subprocess.run(
        [
            sys.executable,
            str(analyzer),
            "--input",
            str(trace),
            "--format",
            "json",
            "--gap-seconds",
            "30",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    assert payload["event_count"] == 2
    assert payload["repeated_tools"] == {"wiki_search": 2}
    assert payload["long_gaps"][0]["seconds"] == 60.0
    assert payload["errors"][0]["error"] == "timeout"
    assert secret_value not in result.stdout


def test_office_runtime_dependencies_are_declared_and_collected() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    spec = (PROJECT_ROOT / "lantai-server.spec").read_text(encoding="utf-8")
    distribution_names = {
        "pandas": "pandas",
        "openpyxl": "openpyxl",
        "pypdf": "pypdf",
        "pdfplumber": "pdfplumber",
        "pypdfium2": "pypdfium2",
        "reportlab": "reportlab",
        "docx": "python-docx",
        "pptx": "python-pptx",
        "markitdown": "markitdown",
    }
    for import_name in OFFICE_IMPORTS:
        assert f'"{distribution_names[import_name]}' in pyproject
        assert f"'{import_name}'" in spec
    assert "'magika'" in spec


def test_frozen_backend_smoke_exercises_office_round_trips() -> None:
    entry = (PROJECT_ROOT / "electron" / "entry.py").read_text(encoding="utf-8")
    for fragment in (
        "pd.DataFrame",
        "load_workbook",
        "Document(document_buffer)",
        "Presentation(presentation_buffer)",
        "PdfReader(pdf_buffer)",
        "pdfplumber.open",
        "MarkItDown().convert",
    ):
        assert fragment in entry


def test_updated_workflow_descriptions_are_specific() -> None:
    expected_terms = {
        "agent-designer-zh": ("创建 Agent", "只想调用现有 Agent"),
        "agent-run-doctor-zh": ("诊断这次运行", "普通代码排错"),
        "github-pr-ci-zh": ("修复 PR 检查", "未经授权直接推送"),
        "mcp-integration-builder-zh": ("开发/调试 MCP Server", "正常工作的 MCP 工具"),
        "desk-research-zh": ("深度研究", "只查一个简单事实"),
        "ai-taste-rewrite-zh": ("去 AI 味", "事实研究"),
        "lantai-web-data-zh": ("股票行情", "多轮证据综合"),
        "pr-launch-zh": ("记者 Q&A", "虚假造势"),
        "crisis-response-zh": ("危机回应", "普通品牌文案"),
        "lantai-automation-channel-zh": ("定时执行", "一次性立即任务"),
        "lantai-core-tools-zh": ("本地文件", "纯知识问答"),
        "lantai-evolution-feedback-zh": ("从反馈中改进", "单个产品 bug"),
        "lantai-memory-context-zh": ("记住/忘记这个", "普通文档摘要"),
        "lantai-orchestration-zh": ("多 Agent 协作", "单角色可直接完成"),
        "project-postmortem-zh": ("项目复盘", "进行中的状态汇报"),
    }
    for folder, terms in expected_terms.items():
        post = frontmatter.load(SKILLS_ROOT / folder / "SKILL.md")
        description = str(post.get("description") or "")
        for term in terms:
            assert term in description
