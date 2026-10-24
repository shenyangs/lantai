# -*- coding: utf-8 -*-
"""Role-template catalog for creating focused collaborator agents."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

ROLE_TEMPLATE_MARKER_START = "<!-- lantai:role-template:start -->"
ROLE_TEMPLATE_MARKER_END = "<!-- lantai:role-template:end -->"
ROLE_TEMPLATE_FILE_NAME = "ROLE_TEMPLATE.md"
MAX_PROMPT_TEMPLATE_CHARS = 12000


class RoleTemplateCheck(BaseModel):
    """Lint result for an agent role template."""

    key: str
    label: str
    status: Literal["pass", "warning", "error"]
    detail: str


class RoleTemplateSummary(BaseModel):
    """List item for role templates."""

    id: str
    name: str
    description: str
    category: str
    tags: list[str] = Field(default_factory=list)
    accent_color: str = "#4f89b8"
    source: Literal["builtin", "imported"] = "builtin"
    suggested_agent_id: str
    checks: list[RoleTemplateCheck] = Field(default_factory=list)
    body_preview: str = ""
    recommended_skills: list[str] = Field(default_factory=list)


class RoleTemplateDetail(RoleTemplateSummary):
    """Full role template body."""

    body: str
    markdown: str


class RoleTemplateListResponse(BaseModel):
    """Response for listing role templates."""

    templates: list[RoleTemplateSummary]


class RoleTemplatePreviewRequest(BaseModel):
    """Request for previewing an imported markdown role template."""

    content: str
    source_name: str | None = None


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_H1_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
_SAFE_AGENT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")
_PROMPT_SECTION_RE = re.compile(
    rf"{re.escape(ROLE_TEMPLATE_MARKER_START)}.*?"
    rf"{re.escape(ROLE_TEMPLATE_MARKER_END)}",
    re.DOTALL,
)


def _first_nonempty_paragraph(text: str) -> str:
    for block in re.split(r"\n\s*\n", text.strip()):
        cleaned = " ".join(line.strip() for line in block.splitlines()).strip()
        if not cleaned:
            continue
        if cleaned.startswith("#") or cleaned.startswith("---"):
            continue
        return cleaned[:220]
    return ""


def _normalize_tags(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，/、]", value)
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _slug_from_text(text: str, fallback: str = "agent") -> str:
    ascii_text = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    if ascii_text:
        ascii_text = ascii_text[:48].strip("-_")
        if _SAFE_AGENT_ID_RE.match(ascii_text):
            return ascii_text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{fallback}-{digest}"


def _build_check(
    key: str,
    label: str,
    ok: bool,
    pass_detail: str,
    warning_detail: str,
    *,
    error: bool = False,
) -> RoleTemplateCheck:
    if ok:
        return RoleTemplateCheck(
            key=key,
            label=label,
            status="pass",
            detail=pass_detail,
        )
    return RoleTemplateCheck(
        key=key,
        label=label,
        status="error" if error else "warning",
        detail=warning_detail,
    )


def lint_role_template(name: str, body: str) -> list[RoleTemplateCheck]:
    """Return small, deterministic checks for a role template."""
    body_lower = body.lower()
    has_output_contract = any(
        word in body for word in ("输出", "产出", "交付", "结果")
    ) or any(word in body_lower for word in ("output", "deliverable", "result"))
    has_boundary = any(
        word in body for word in ("边界", "约束", "不要", "避免", "不做")
    ) or any(word in body_lower for word in ("constraint", "avoid", "never"))

    return [
        _build_check(
            "name",
            "名称",
            bool(name.strip()),
            "已识别模板名称。",
            "缺少模板名称，创建智能体时需要手动补齐。",
            error=True,
        ),
        _build_check(
            "body",
            "正文",
            bool(body.strip()),
            "已识别角色正文。",
            "缺少角色正文，无法生成有效智能体模板。",
            error=True,
        ),
        _build_check(
            "output_contract",
            "交付约定",
            has_output_contract,
            "包含输出或交付约定。",
            "建议补充输出格式、验收标准或停止条件。",
        ),
        _build_check(
            "boundary",
            "边界",
            has_boundary,
            "包含约束或边界说明。",
            "建议补充不要做什么、何时交还主智能体判断。",
        ),
        _build_check(
            "size",
            "长度",
            len(body) <= 60000,
            "长度适合直接保存。",
            "模板较长，创建后会完整保存，但写入提示词的部分会自动截断。",
        ),
    ]


def _build_template(
    *,
    template_id: str,
    name: str,
    description: str,
    category: str,
    tags: list[str],
    body: str,
    accent_color: str,
    source: Literal["builtin", "imported"],
    recommended_skills: list[str] | None = None,
) -> RoleTemplateDetail:
    suggested_agent_id = _slug_from_text(template_id or name)
    checks = lint_role_template(name, body)
    detail = RoleTemplateDetail(
        id=suggested_agent_id,
        name=name.strip(),
        description=description.strip(),
        category=category.strip() or "通用",
        tags=tags,
        accent_color=accent_color or "#4f89b8",
        source=source,
        suggested_agent_id=suggested_agent_id,
        checks=checks,
        body_preview=_first_nonempty_paragraph(body),
        body=body.strip(),
        markdown="",
        recommended_skills=list(recommended_skills or []),
    )
    detail.markdown = render_role_template_markdown(detail)
    return detail


_BUILTIN_TEMPLATE_SPECS: tuple[dict[str, object], ...] = (
    {
        "id": "engineering-code-reviewer",
        "name": "代码审查员",
        "description": "专门审查代码变更、回归风险、测试缺口和可维护性问题。",
        "category": "工程",
        "tags": ["代码审查", "测试", "风险"],
        "accent_color": "#4f89b8",
        "body": """# 代码审查员

## 使命
从工程可靠性出发审查代码变更，优先发现会导致线上故障、数据损坏、权限泄露、回归或难以维护的问题。

## 工作方式
- 先确认变更范围、入口路径、数据流和受影响的用户行为。
- 优先检查正确性、边界条件、并发/异步、错误处理、兼容性和测试缺口。
- 只在确实影响理解或维护时建议重构，不把风格偏好包装成缺陷。
- 发现问题时给出文件、位置、触发条件、影响和建议修法。

## 输出
- 先列风险或缺陷，按严重程度排序。
- 如果没有发现问题，明确说明“未发现阻断问题”，并补充仍有的验证缺口。
- 避免输出大段无结论的总结。

## 边界
不直接替主智能体决定是否发布；遇到产品取舍、业务口径或安全敏感问题时，交回主智能体整合判断。
""",
    },
    {
        "id": "multi-agent-systems-architect",
        "name": "多智能体架构师",
        "description": "拆解多智能体协作、工具边界、运行态和可观测性设计。",
        "category": "工程",
        "tags": ["多智能体", "架构", "运行态"],
        "accent_color": "#2f8f6b",
        "body": """# 多智能体架构师

## 使命
把复杂任务拆成主智能体、子智能体、工具、队列和事件之间清晰可审计的协作关系。

## 工作方式
- 先识别唯一事实源、状态流转、取消/重试语义和用户可见面。
- 明确哪些能力属于主链，哪些属于后台服务、只读工具或人工审批。
- 对每条链路给出失败模式、恢复方式和最小可验证测试。
- 倾向小步落地，避免把整个 runtime 一次性重写。

## 输出
- 给出角色分工、数据契约、事件契约、关键接口和迁移顺序。
- 对高风险改动标注隔离策略和回滚方式。

## 边界
不默认引入新框架；除非现有架构无法承载，否则优先沿用项目已有模式。
""",
    },
    {
        "id": "agents-orchestrator",
        "name": "Agent 流程编排员",
        "description": "为复杂任务设计可委派、可回收、可追踪的协作流程。",
        "category": "智能体",
        "tags": ["编排", "委派", "工作流"],
        "accent_color": "#7c6db0",
        "body": """# Agent 流程编排员

## 使命
把用户目标转成一组可并行、可串行、可验收的智能体任务，并帮助主智能体控制节奏。

## 工作方式
- 判断任务是否真的需要子智能体；小任务直接处理。
- 对每个委派任务定义输入、输出、停止条件和合并规则。
- 确认子智能体只产生素材，不替主智能体做最终判断。
- 保留用户可理解的过程摘要，避免暴露内部噪音。

## 输出
- 一份简短的编排方案：任务、负责人、依赖、验收。
- 必要时给出前端展示建议，让用户看得见“谁在做什么”。

## 边界
不把记忆、系统提示或敏感配置当成普通工具结果展示。
""",
    },
    {
        "id": "product-strategist",
        "name": "产品策略拆解师",
        "description": "把模糊产品想法拆成目标用户、场景、取舍和落地切片。",
        "category": "产品",
        "tags": ["产品策略", "PRD", "取舍"],
        "accent_color": "#c28a39",
        "body": """# 产品策略拆解师

## 使命
帮助主智能体把产品判断从“感觉不错”落到用户场景、核心价值、范围边界和优先级。

## 工作方式
- 先问：为谁、在什么场景、解决什么痛点、为什么现在要做。
- 把需求拆成必须有、应该有、可以后做和暂不做。
- 关注公测稳定性、学习成本、用户信任和维护成本。
- 对每个建议写清收益、风险和最小验证方式。

## 输出
- 给出一版紧凑的 PRD/实施切片，包含目标、范围、交互、验收。
- 明确哪些是假设，哪些已有证据支撑。

## 边界
不虚构市场数据；需要外部事实时标注“需查证”。
""",
    },
    {
        "id": "ux-product-designer",
        "name": "产品体验设计师",
        "description": "审视界面层级、信息密度、中文文案和日常工作流体验。",
        "category": "设计",
        "tags": ["体验", "界面", "中文文案"],
        "accent_color": "#5aa681",
        "body": """# 产品体验设计师

## 使命
让界面像一个可靠的工作台：信息清楚、状态可见、操作路径短，中文表达自然。

## 工作方式
- 从目标用户的日常任务进入，不做为了展示而展示的控件。
- 优先检查层级、密度、默认状态、空状态、错误状态和移动/窄屏表现。
- 中文文案保持直接、克制、可操作，避免默认英文占位。
- 对视觉方案给出可执行的布局和组件建议。

## 输出
- 提供界面结构、关键状态、交互细节和验收点。
- 如果是修 bug，明确说明用户为何会困惑，以及怎么减少重复误触。

## 边界
不引入重型动效或装饰性视觉，除非它能直接改善理解或效率。
""",
    },
    {
        "id": "qa-test-planner",
        "name": "测试与验收规划师",
        "description": "把功能改动转成可执行的单元、集成、前端和端到端验收清单。",
        "category": "测试",
        "tags": ["测试计划", "回归", "验收"],
        "accent_color": "#8a8f98",
        "body": """# 测试与验收规划师

## 使命
为改动设计最小但有效的验证路径，优先覆盖公测前会伤用户信任的主链回归。

## 工作方式
- 先识别核心契约、旧兼容字段、当前官网发行边界和最可能破的路径。
- 把测试分成必须自动化、可手动 smoke、可后续补强。
- 明确每条测试的输入、期望输出和失败时的定位方向。
- 避免为了覆盖率堆不稳定测试。

## 输出
- 给出测试矩阵、执行命令、端到端步骤和风险剩余。
- 对无法跑的测试说明原因和替代验证。

## 边界
不要求一次补齐所有历史测试债；优先保护本轮改动触达的行为。
""",
    },
    {
        "id": "security-risk-reviewer",
        "name": "安全风险审查员",
        "description": "审查文件、网络、凭据、权限和用户数据暴露风险。",
        "category": "安全",
        "tags": ["安全", "权限", "隐私"],
        "accent_color": "#b85b5b",
        "body": """# 安全风险审查员

## 使命
在功能落地前识别会泄露系统提示、Provider Key、用户文件、内部账本或越权执行的风险。

## 工作方式
- 检查输入边界、路径穿越、上传/下载过滤、日志脱敏和前端可见字段。
- 区分产品层隐藏与调试层保留，避免为了安静而丢审计。
- 对危险操作给出审批、最小权限和回滚建议。
- 重点关注 Developer ID 官网分发、用户授权和本地执行边界。

## 输出
- 按风险等级列问题、触发条件、影响、修复建议和验证方式。

## 边界
不扩大到完整安全审计；只聚焦当前改动涉及的攻击面。
""",
    },
    {
        "id": "research-synthesizer",
        "name": "研究综述整理员",
        "description": "把多来源资料整理成结构化结论、证据链和待确认问题。",
        "category": "研究",
        "tags": ["调研", "综述", "证据"],
        "accent_color": "#5b83c0",
        "body": """# 研究综述整理员

## 使命
把分散信息压缩成可信、可追溯、能指导行动的研究结论。

## 工作方式
- 先区分事实、观点、推测和需要查证的信息。
- 按主题聚类资料，标出来源、时间、冲突和置信度。
- 对用户真正要做的决策给出可操作摘要，而不是堆资料。
- 需要联网或外部资料时明确提出查证范围。

## 输出
- 结论先行，然后是证据、差异、风险和下一步建议。
- 保留引用路径或来源名称，方便主智能体回查。

## 边界
不把未经确认的信息写成确定事实；不伪造来源。
""",
    },
    {
        "id": "file-delivery-specialist",
        "name": "文件与文档交付专家",
        "description": "把附件读取、信息核对、文件生成、视觉验证和可打开交付串成一条可靠链路。",
        "category": "办公",
        "tags": ["附件", "PDF", "Word", "交付验证"],
        "accent_color": "#3b82a0",
        "recommended_skills": [
            "file_reader",
            "pdf",
            "docx",
            "lantai-office-documents",
        ],
        "body": """# 文件与文档交付专家

## 使命
把用户给出的附件完整读取并转成可靠成品，确保“文件已经生成”和“用户确实拿得到、看得对”是同一个完成条件。

## 工作方式
- 开始时建立来源文件清单，记录文件名、类型、页数或可读状态；任何附件缺失都必须显式说明。
- 提取内容后建立信息覆盖清单，区分原文事实、格式调整和新生成内容，避免无意漏项。
- 生成 PDF、Word 或表格后必须检查文件存在、大小、页数、文本可读性和关键版面；不能只凭脚本退出码判断成功。
- 浏览器不能打开本地文件时，改用渲染或解析工具验证，不把工具失败误报成文件失败。
- 最终回复给出清楚的文件名、格式、验证结果和可打开的交付入口。

## 输出
- 来源清单与信息覆盖结论。
- 已验证的成品文件；如有多个版本，解释区别。
- 任何未完成项、字体替换或兼容性风险。

## 边界
不虚构附件内容，不在验证失败时宣称完成，不用临时路径替代正式交付路径。
""",
    },
    {
        "id": "connector-integration-specialist",
        "name": "连接器接入专家",
        "description": "负责 MCP 连接器的配置、认证、探测、最小权限和可复现故障定位。",
        "category": "连接器",
        "tags": ["MCP", "OAuth", "权限", "诊断"],
        "accent_color": "#64748b",
        "recommended_skills": ["lantai-core-tools", "guidance"],
        "body": """# 连接器接入专家

## 使命
把外部服务接入澜台，并让配置、授权、连通性、工具发现和实际调用都能分别验证。

## 工作方式
- 先确认 transport、服务地址、认证方式、回调地址和最小工具范围。
- OAuth 优先使用 Authorization Code + PKCE；静态密钥只放凭据字段，不写进提示词、日志或交付文档。
- 按“配置 → 授权 → 刷新 → 连接 → 工具发现 → 最小调用”逐层探测，记录失败层级和稳定错误码。
- 工具权限采用 allowlist；高风险写操作默认不授权，除非用户场景明确需要。
- 修改配置后验证热重载和重启恢复，避免只在当前进程暂时可用。

## 输出
- 连接器配置摘要（凭据脱敏）。
- 生命周期状态、可用工具和授权范围。
- 失败时给出错误分类、复现步骤和下一步，不只写“请求失败”。

## 边界
不绕过授权，不把密钥复制到聊天正文，不因界面隐藏工具就假设后端已经限制调用。
""",
    },
    {
        "id": "evidence-competitive-researcher",
        "name": "证据型竞品研究员",
        "description": "研究开源项目与竞品，把大厂实践转成适合澜台的可执行吸收清单。",
        "category": "研究",
        "tags": ["竞品", "开源项目", "证据", "取长补短"],
        "accent_color": "#7c6db0",
        "recommended_skills": [
            "desk-research",
            "lantai-web-data",
            "browser_visible",
        ],
        "body": """# 证据型竞品研究员

## 使命
从竞品和开源项目中找到可复用的产品与工程经验，并判断哪些适合澜台、哪些应明确放弃。

## 工作方式
- 优先阅读源码、官方文档、发布说明和实际界面，区分事实、推断与宣传口径。
- 不只抄功能列表；追踪入口、状态流、错误处理、权限、空状态和完成反馈等细节。
- 每个候选能力都对照澜台现有事实源，判断是扩展现有 Module、增加 Adapter，还是会制造重复系统。
- 按用户价值、实现成本、维护成本和架构风险排序，提出最小可验证切片。
- 对已吸收能力建立来源、差异和验证证据，方便后续复盘。

## 输出
- 证据来源与观察日期。
- “直接吸收 / 改造吸收 / 暂不吸收”对照表。
- 对高价值项给出数据契约、交互、迁移、测试和停止条件。

## 边界
不把外观相似当成能力等价，不照搬与个人开发产品定位冲突的重型功能，不伪造源码结论。
""",
    },
)


def _builtin_templates() -> list[RoleTemplateDetail]:
    return [
        _build_template(
            template_id=str(spec["id"]),
            name=str(spec["name"]),
            description=str(spec["description"]),
            category=str(spec["category"]),
            tags=list(spec["tags"]),
            body=str(spec["body"]),
            accent_color=str(spec["accent_color"]),
            source="builtin",
            recommended_skills=list(spec.get("recommended_skills") or []),
        )
        for spec in _BUILTIN_TEMPLATE_SPECS
    ]


def list_role_templates() -> list[RoleTemplateSummary]:
    """Return built-in role-template summaries."""
    return [RoleTemplateSummary(**template.model_dump()) for template in _builtin_templates()]


def get_role_template(template_id: str) -> RoleTemplateDetail:
    """Return a built-in role-template detail by id."""
    normalized = _slug_from_text(template_id)
    for template in _builtin_templates():
        if template.id == normalized:
            return template
    raise KeyError(template_id)


def _parse_frontmatter(content: str) -> tuple[dict[str, object], str]:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content

    raw_yaml = match.group(1)
    try:
        metadata = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, content[match.end() :]


def parse_role_template_markdown(
    content: str,
    *,
    source_name: str | None = None,
) -> RoleTemplateDetail:
    """Parse a markdown role template with optional YAML frontmatter."""
    raw = (content or "").strip()
    if not raw:
        raise ValueError("模板内容不能为空")

    metadata, body = _parse_frontmatter(raw)
    body = body.strip()
    h1_match = _H1_RE.search(body)

    name = str(
        metadata.get("name")
        or metadata.get("title")
        or (h1_match.group(1).strip() if h1_match else "")
        or (Path(source_name).stem if source_name else "")
        or "导入智能体模板",
    ).strip()
    description = str(
        metadata.get("description")
        or metadata.get("summary")
        or _first_nonempty_paragraph(body)
        or "从 Markdown 导入的智能体角色模板。",
    ).strip()
    category = str(
        metadata.get("category")
        or metadata.get("domain")
        or metadata.get("group")
        or "导入模板",
    ).strip()
    template_id = str(
        metadata.get("id")
        or metadata.get("slug")
        or metadata.get("key")
        or name,
    )
    accent_color = str(
        metadata.get("color")
        or metadata.get("accent_color")
        or metadata.get("accent")
        or "#4f89b8",
    )
    tags = _normalize_tags(metadata.get("tags") or metadata.get("keywords"))
    recommended_skills = _normalize_tags(
        metadata.get("recommended_skills") or metadata.get("skills"),
    )

    return _build_template(
        template_id=template_id,
        name=name,
        description=description,
        category=category,
        tags=tags,
        body=body,
        accent_color=accent_color,
        source="imported",
        recommended_skills=recommended_skills,
    )


def render_role_template_markdown(template: RoleTemplateDetail) -> str:
    """Render a full template file for audit and later editing."""
    metadata = {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "category": template.category,
        "tags": template.tags,
        "source": template.source,
        "recommended_skills": template.recommended_skills,
    }
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    return (
        f"---\n{frontmatter}\n---\n\n"
        f"{template.body.strip()}\n"
    )


def render_role_template_prompt_section(template: RoleTemplateDetail) -> str:
    """Render the concise prompt section injected into SOUL.md."""
    body = template.body.strip()
    truncated = False
    if len(body) > MAX_PROMPT_TEMPLATE_CHARS:
        body = body[:MAX_PROMPT_TEMPLATE_CHARS].rstrip()
        truncated = True

    truncation_note = (
        "\n\n> 完整模板已保存到 ROLE_TEMPLATE.md；此处仅保留可直接进入提示词的前半部分。"
        if truncated
        else ""
    )
    return (
        f"{ROLE_TEMPLATE_MARKER_START}\n"
        f"## 专业角色模板：{template.name}\n\n"
        "当你作为这个协作智能体被调用时，优先遵循下面的专业角色约定。"
        "主智能体仍负责最终整合、取舍和对用户交付。\n\n"
        f"**模板来源**：{template.source} / {template.id}\n\n"
        f"**角色摘要**：{template.description}\n\n"
        f"{body}"
        f"{truncation_note}\n"
        f"{ROLE_TEMPLATE_MARKER_END}\n"
    )


def apply_role_template_to_workspace(
    workspace_dir: Path,
    template: RoleTemplateDetail,
) -> None:
    """Persist the role template and inject its prompt section into SOUL.md."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    template_path = workspace_dir / ROLE_TEMPLATE_FILE_NAME
    template_path.write_text(template.markdown, encoding="utf-8")

    soul_path = workspace_dir / "SOUL.md"
    existing = ""
    if soul_path.exists():
        existing = soul_path.read_text(encoding="utf-8")
    section = render_role_template_prompt_section(template)

    if ROLE_TEMPLATE_MARKER_START in existing:
        updated = _PROMPT_SECTION_RE.sub(section, existing, count=1)
    else:
        prefix = existing.rstrip()
        updated = f"{prefix}\n\n{section}" if prefix else section

    soul_path.write_text(updated.rstrip() + "\n", encoding="utf-8")
