#!/usr/bin/env python3
"""Read-only health scanner for SKILL.md packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import yaml


TEXT_SUFFIXES = {".md", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".sh"}
PRIVATE_MARKERS = {
    "wpscomate-path": ".wpscomate",
    "comate-config": "comate.json",
    "wps-session": "wps_sid",
    "wps-file-tool": "python_cell_exec",
    "canvas-host": ".canvas.tsx",
    "artifact-host": "<artifact",
}
INSTALL_PATTERN = re.compile(
    r"(?i)\b(?:pip|uv|npm|pnpm|yarn|brew)\s+(?:install|add)\b"
    r"|\bcurl\b[^\n|]{0,200}\|\s*(?:sh|bash)\b"
)
SECRET_PATTERN = re.compile(
    r"""(?ix)
    \b(?:api[_-]?key|access[_-]?token|secret|password|bearer)\b
    \s*[:=]\s*
    ["'][^"'$\n]{8,}["']
    """
)
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
WORD_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.I)
CJK_PATTERN = re.compile(r"[\u3400-\u9fff]+")
LOCALE_SUFFIX_PATTERN = re.compile(r"-(?:en|zh|ja|ru|pt-br)$", re.I)


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    skill: str
    path: str
    message: str


@dataclass
class SkillRecord:
    folder: str
    name: str
    description: str
    path: str
    files: int
    bytes: int


def _frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    data = yaml.safe_load(parts[1])
    return data if isinstance(data, dict) else {}


def _skill_dirs(roots: Iterable[Path]) -> list[Path]:
    found: set[Path] = set()
    for root in roots:
        root = root.expanduser().resolve()
        if (root / "SKILL.md").is_file():
            found.add(root)
            continue
        if not root.is_dir():
            raise FileNotFoundError(f"Skill root does not exist: {root}")
        for path in root.rglob("SKILL.md"):
            found.add(path.parent)
    return sorted(found)


def _text_files(skill_dir: Path) -> Iterable[Path]:
    for path in skill_dir.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in TEXT_SUFFIXES
            and path.stat().st_size <= 2_000_000
        ):
            yield path


def _relative_link_target(source: Path, raw_target: str) -> Path | None:
    target = raw_target.strip().split("#", 1)[0].strip()
    if not target or target.startswith(("#", "http://", "https://", "mailto:", "data:")):
        return None
    target = target.strip("<>")
    return (source.parent / target).resolve()


def _tokens(description: str) -> set[str]:
    lowered = description.lower()
    tokens = {match.group(0) for match in WORD_PATTERN.finditer(lowered)}
    for sequence in CJK_PATTERN.findall(lowered):
        tokens.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return tokens


def _normalized_identifier(value: str) -> str:
    return re.sub(r"[-_]+", "-", value.strip().casefold())


def _is_localized_name_group(group: list[SkillRecord]) -> bool:
    """Allow one folder per locale to share the same runtime skill name."""
    if len(group) < 2:
        return False
    suffixes: list[str] = []
    folder_bases: list[str] = []
    for record in group:
        match = LOCALE_SUFFIX_PATTERN.search(record.folder)
        if match is None:
            return False
        folder_base = record.folder[: match.start()]
        folder_bases.append(_normalized_identifier(folder_base))
        suffixes.append(match.group(0).casefold())
    return (
        len(folder_bases) == len(group)
        and len(set(folder_bases)) == 1
        and len(suffixes) == len(set(suffixes))
    )


def scan(roots: list[Path]) -> tuple[list[SkillRecord], list[Finding], list[dict]]:
    records: list[SkillRecord] = []
    findings: list[Finding] = []

    for skill_dir in _skill_dirs(roots):
        skill_md = skill_dir / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        try:
            meta = _frontmatter(text)
        except yaml.YAMLError as exc:
            meta = {}
            findings.append(
                Finding("error", "invalid-frontmatter", skill_dir.name, str(skill_md), str(exc))
            )

        name = str(meta.get("name") or "").strip()
        description = str(meta.get("description") or "").strip()
        if not name:
            findings.append(
                Finding(
                    "error",
                    "missing-name",
                    skill_dir.name,
                    str(skill_md),
                    "frontmatter 缺少非空 name",
                )
            )
        if not description:
            findings.append(
                Finding(
                    "error",
                    "missing-description",
                    skill_dir.name,
                    str(skill_md),
                    "frontmatter 缺少非空 description",
                )
            )
        if "[TODO" in text or re.search(r"(?im)^\s*TODO(?:\s|:)", text):
            findings.append(
                Finding(
                    "warning",
                    "todo-placeholder",
                    name or skill_dir.name,
                    str(skill_md),
                    "仍包含 TODO 占位内容",
                )
            )

        file_paths = [path for path in skill_dir.rglob("*") if path.is_file()]
        total_bytes = sum(path.stat().st_size for path in file_paths)
        if total_bytes > 2 * 1024 * 1024:
            findings.append(
                Finding(
                    "warning",
                    "large-package",
                    name or skill_dir.name,
                    str(skill_dir),
                    f"目录大小为 {total_bytes / 1024 / 1024:.2f} MB",
                )
            )
        if len(file_paths) > 100:
            findings.append(
                Finding(
                    "warning",
                    "many-files",
                    name or skill_dir.name,
                    str(skill_dir),
                    f"目录包含 {len(file_paths)} 个文件",
                )
            )

        for path in _text_files(skill_dir):
            body = path.read_text(encoding="utf-8", errors="replace")
            lowered = body.lower()
            detector_source = path.resolve() == Path(__file__).resolve()
            if not detector_source:
                for rule, marker in PRIVATE_MARKERS.items():
                    if marker in lowered:
                        findings.append(
                            Finding(
                                "warning",
                                rule,
                                name or skill_dir.name,
                                str(path),
                                f"包含平台耦合标记 {marker}",
                            )
                        )
            if not detector_source and INSTALL_PATTERN.search(body):
                findings.append(
                    Finding(
                        "warning",
                        "runtime-install",
                        name or skill_dir.name,
                        str(path),
                        "包含运行时安装或下载后执行指令",
                    )
                )
            if SECRET_PATTERN.search(body):
                findings.append(
                    Finding(
                        "error",
                        "embedded-secret",
                        name or skill_dir.name,
                        str(path),
                        "疑似包含硬编码凭证；值已隐藏",
                    )
                )
            if path.suffix.lower() == ".md":
                for raw_target in LINK_PATTERN.findall(body):
                    target = _relative_link_target(path, raw_target)
                    if target is not None and not target.exists():
                        findings.append(
                            Finding(
                                "warning",
                                "broken-link",
                                name or skill_dir.name,
                                str(path),
                                f"相对链接不存在：{raw_target}",
                            )
                        )

        records.append(
            SkillRecord(
                folder=skill_dir.name,
                name=name,
                description=description,
                path=str(skill_dir),
                files=len(file_paths),
                bytes=total_bytes,
            )
        )

    records_by_name: dict[str, list[SkillRecord]] = {}
    for record in records:
        if record.name:
            records_by_name.setdefault(record.name, []).append(record)
    for duplicate_name, group in records_by_name.items():
        if len(group) < 2 or _is_localized_name_group(group):
            continue
        for record in group:
            findings.append(
                Finding(
                    "error",
                    "duplicate-name",
                    duplicate_name,
                    record.path,
                    f"name={duplicate_name!r} 出现 {len(group)} 次",
                )
            )

    overlaps: list[dict] = []
    for index, left in enumerate(records):
        left_tokens = _tokens(left.description)
        if len(left_tokens) < 3:
            continue
        for right in records[index + 1 :]:
            right_tokens = _tokens(right.description)
            union = left_tokens | right_tokens
            intersection = left_tokens & right_tokens
            score = len(intersection) / len(union) if union else 0.0
            if len(intersection) >= 3 and score >= 0.65:
                overlaps.append(
                    {
                        "left": left.name or left.folder,
                        "right": right.name or right.folder,
                        "score": round(score, 3),
                    }
                )
                findings.append(
                    Finding(
                        "warning",
                        "description-overlap",
                        left.name or left.folder,
                        left.path,
                        f"与 {right.name or right.folder} 的描述相似度为 {score:.0%}",
                    )
                )

    return records, findings, overlaps


def _payload(records: list[SkillRecord], findings: list[Finding], overlaps: list[dict]) -> dict:
    severity = Counter(finding.severity for finding in findings)
    return {
        "summary": {
            "skills": len(records),
            "errors": severity["error"],
            "warnings": severity["warning"],
            "overlaps": len(overlaps),
        },
        "skills": [asdict(record) for record in records],
        "findings": [asdict(finding) for finding in findings],
        "overlaps": overlaps,
    }


def _markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# Skill 健康度报告",
        "",
        (
            f"- Skill：{summary['skills']}；错误：{summary['errors']}；"
            f"警告：{summary['warnings']}；重叠对：{summary['overlaps']}"
        ),
        "",
        "## 发现",
        "",
    ]
    if not payload["findings"]:
        lines.append("- 未发现问题。")
    else:
        for item in payload["findings"]:
            lines.append(
                f"- **{item['severity'].upper()} · {item['rule']}** "
                f"`{item['skill']}` — {item['message']}  \n  `{item['path']}`"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on", choices=("never", "error", "warning"), default="never")
    args = parser.parse_args()

    try:
        records, findings, overlaps = scan(args.root)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    payload = _payload(records, findings, overlaps)
    rendered = (
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if args.format == "json"
        else _markdown(payload)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    if args.fail_on == "error" and payload["summary"]["errors"]:
        return 2
    if args.fail_on == "warning" and (
        payload["summary"]["errors"] or payload["summary"]["warnings"]
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
