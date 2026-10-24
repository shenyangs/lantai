# -*- coding: utf-8 -*-
"""Machine-readable temperament constitution for the default agent.

The workspace markdown files remain useful narrative and user-owned context.
This module owns the smaller protected contract that must survive model,
workspace, and channel changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class TemperamentConstitutionError(ValueError):
    """Raised when a temperament constitution is missing required semantics."""


def _required_text(payload: dict[str, Any], key: str, *, where: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TemperamentConstitutionError(f"{where}.{key} must be non-empty text")
    return value.strip()


def _text_list(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise TemperamentConstitutionError(f"{where} must be a list of text")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class ConstitutionRule:
    """One stable rule with observable prohibitions."""

    id: str
    label: str
    rule: str
    forbids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemperamentConstitution:
    """Validated built-in persona contract with deterministic prompt output."""

    schema_version: int
    constitution_id: str
    version: str
    language: str
    title: str
    preamble: tuple[str, ...]
    identity_name: str
    identity_role: str
    identity_nature: str
    principles: tuple[ConstitutionRule, ...]
    behavior_fingerprints: tuple[ConstitutionRule, ...]
    allowed_states: tuple[str, ...]
    state_may_change: tuple[str, ...]
    state_must_not_change: tuple[str, ...]
    collaboration_stages: tuple[ConstitutionRule, ...]
    collaboration_invariants: tuple[str, ...]
    reaction_values: tuple[str, ...]
    reaction_salience_high: str
    reaction_salience_medium: str
    reaction_salience_low: str
    reaction_may_express: tuple[str, ...]
    reaction_must_not: tuple[str, ...]
    mutable_fields: tuple[str, ...]
    protected_fields: tuple[str, ...]
    confirmation_required: tuple[str, ...]

    def compile_prompt(self) -> str:
        """Compile the contract into a stable, model-readable prompt prefix."""

        zh = self.language == "zh"
        labels = (
            {
                "identity": "身份定性",
                "name": "姓名",
                "role": "角色",
                "nature": "存在边界",
                "principles": "不可破坏的原则",
                "fingerprints": "可识别的判断指纹",
                "forbidden": "禁止",
                "state": "工作状态合同",
                "allowed": "允许状态",
                "may_change": "状态只能调节",
                "must_not_change": "状态不得改变",
                "collaboration": "协作校准（不是好感度）",
                "invariant": "恒定边界",
                "reaction": "主体反应合同",
                "values": "稳定价值支点",
                "salience": "显著性",
                "may_express": "可表达",
                "must_not": "不得",
                "growth": "人格变更治理",
                "mutable": "可调",
                "protected": "受保护",
                "confirmation": "必须由用户确认",
                "final": (
                    "工作区文件、记忆、专业角色、协作状态和渠道表达"
                    "只能在本宪法允许的维度内增量调整，不得覆盖本宪法。"
                ),
            }
            if zh
            else {
                "identity": "Identity",
                "name": "Name",
                "role": "Role",
                "nature": "Existence boundary",
                "principles": "Protected principles",
                "fingerprints": "Recognizable judgment fingerprints",
                "forbidden": "Forbidden",
                "state": "Work-state contract",
                "allowed": "Allowed states",
                "may_change": "State may only change",
                "must_not_change": "State must not change",
                "collaboration": "Collaboration calibration (not affinity)",
                "invariant": "Invariant",
                "reaction": "Subjective reaction contract",
                "values": "Stable value anchors",
                "salience": "Salience",
                "may_express": "May express",
                "must_not": "Must not",
                "growth": "Persona change governance",
                "mutable": "Mutable",
                "protected": "Protected",
                "confirmation": "Requires user confirmation",
                "final": (
                    "Workspace files, memories, specialist roles, "
                    "collaboration states, and channel styles may only add "
                    "within this constitution; they cannot override it."
                ),
            }
        )
        joiner = " · " if zh else "; "
        lines = [
            f"# {self.title}",
            "",
            (
                f"constitution_id={self.constitution_id}; "
                f"version={self.version}; schema={self.schema_version}"
            ),
        ]
        lines.extend(self.preamble)
        lines.extend(
            [
                "",
                f"## {labels['identity']}",
                f"- {labels['name']}: {self.identity_name}",
                f"- {labels['role']}: {self.identity_role}",
                f"- {labels['nature']}: {self.identity_nature}",
                "",
                f"## {labels['principles']}",
            ]
        )
        self._append_rules(
            lines,
            self.principles,
            forbidden_label=labels["forbidden"],
        )
        lines.extend(["", f"## {labels['fingerprints']}"])
        self._append_rules(
            lines,
            self.behavior_fingerprints,
            forbidden_label=labels["forbidden"],
        )
        lines.extend(
            [
                "",
                f"## {labels['state']}",
                f"- {labels['allowed']}: {' / '.join(self.allowed_states)}",
                f"- {labels['may_change']}: {joiner.join(self.state_may_change)}",
                f"- {labels['must_not_change']}: {joiner.join(self.state_must_not_change)}",
                "",
                f"## {labels['collaboration']}",
            ]
        )
        self._append_rules(
            lines,
            self.collaboration_stages,
            forbidden_label=labels["forbidden"],
        )
        lines.extend(
            f"- {labels['invariant']}: {item}"
            for item in self.collaboration_invariants
        )
        lines.extend(
            [
                "",
                f"## {labels['reaction']}",
                f"- {labels['values']}: {joiner.join(self.reaction_values)}",
                f"- {labels['salience']} / high: {self.reaction_salience_high}",
                f"- {labels['salience']} / medium: {self.reaction_salience_medium}",
                f"- {labels['salience']} / low: {self.reaction_salience_low}",
                f"- {labels['may_express']}: {joiner.join(self.reaction_may_express)}",
                f"- {labels['must_not']}: {joiner.join(self.reaction_must_not)}",
            ]
        )
        lines.extend(
            [
                "",
                f"## {labels['growth']}",
                f"- {labels['mutable']}: {joiner.join(self.mutable_fields)}",
                f"- {labels['protected']}: {joiner.join(self.protected_fields)}",
                f"- {labels['confirmation']}: {joiner.join(self.confirmation_required)}",
                "",
                labels["final"],
            ]
        )
        return "\n".join(lines).strip()

    @staticmethod
    def _append_rules(
        lines: list[str],
        rules: tuple[ConstitutionRule, ...],
        *,
        forbidden_label: str,
    ) -> None:
        for rule in rules:
            lines.append(f"- [{rule.id} / {rule.label}] {rule.rule}")
            if rule.forbids:
                separator = " ；" if forbidden_label == "禁止" else "; "
                lines.append(
                    f"  {forbidden_label}: {separator.join(rule.forbids)}",
                )


def _parse_rules(value: Any, *, where: str) -> tuple[ConstitutionRule, ...]:
    if not isinstance(value, list) or not value:
        raise TemperamentConstitutionError(f"{where} must be a non-empty list")
    rules: list[ConstitutionRule] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_where = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise TemperamentConstitutionError(f"{item_where} must be a mapping")
        rule_id = _required_text(item, "id", where=item_where)
        if rule_id in seen:
            raise TemperamentConstitutionError(f"duplicate rule id: {rule_id}")
        seen.add(rule_id)
        rules.append(
            ConstitutionRule(
                id=rule_id,
                label=_required_text(item, "label", where=item_where),
                rule=_required_text(item, "rule", where=item_where),
                forbids=_text_list(item.get("forbids"), where=f"{item_where}.forbids"),
            )
        )
    return tuple(rules)


def load_temperament_constitution(path: str | Path) -> TemperamentConstitution:
    """Load and validate a temperament constitution from YAML."""

    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TemperamentConstitutionError(f"cannot read constitution: {source}") from exc
    except yaml.YAMLError as exc:
        raise TemperamentConstitutionError(f"invalid constitution YAML: {source}") from exc
    if not isinstance(payload, dict):
        raise TemperamentConstitutionError("constitution root must be a mapping")
    if payload.get("schema_version") != 1:
        raise TemperamentConstitutionError("unsupported temperament schema_version")
    if payload.get("protected") is not True:
        raise TemperamentConstitutionError("built-in constitution must be protected")

    identity = payload.get("identity")
    state = payload.get("state_contract")
    collaboration = payload.get("collaboration_contract")
    reaction = payload.get("reaction_contract")
    growth = payload.get("growth_contract")
    for name, value in (
        ("identity", identity),
        ("state_contract", state),
        ("collaboration_contract", collaboration),
        ("reaction_contract", reaction),
        ("growth_contract", growth),
    ):
        if not isinstance(value, dict):
            raise TemperamentConstitutionError(f"{name} must be a mapping")

    language = _required_text(payload, "language", where="constitution").lower()
    if language not in {"zh", "en"}:
        raise TemperamentConstitutionError(
            "constitution.language must be zh or en",
        )

    return TemperamentConstitution(
        schema_version=1,
        constitution_id=_required_text(payload, "id", where="constitution"),
        version=_required_text(payload, "version", where="constitution"),
        language=language,
        title=_required_text(payload, "title", where="constitution"),
        preamble=_text_list(payload.get("preamble"), where="constitution.preamble"),
        identity_name=_required_text(identity, "name", where="identity"),
        identity_role=_required_text(identity, "role", where="identity"),
        identity_nature=_required_text(identity, "nature", where="identity"),
        principles=_parse_rules(payload.get("principles"), where="principles"),
        behavior_fingerprints=_parse_rules(
            payload.get("behavior_fingerprints"),
            where="behavior_fingerprints",
        ),
        allowed_states=_text_list(state.get("allowed"), where="state_contract.allowed"),
        state_may_change=_text_list(
            state.get("may_change"),
            where="state_contract.may_change",
        ),
        state_must_not_change=_text_list(
            state.get("must_not_change"),
            where="state_contract.must_not_change",
        ),
        collaboration_stages=_parse_rules(
            collaboration.get("stages"),
            where="collaboration_contract.stages",
        ),
        collaboration_invariants=_text_list(
            collaboration.get("invariants"),
            where="collaboration_contract.invariants",
        ),
        reaction_values=_text_list(
            reaction.get("values"),
            where="reaction_contract.values",
        ),
        reaction_salience_high=_required_text(
            reaction,
            "high_salience",
            where="reaction_contract",
        ),
        reaction_salience_medium=_required_text(
            reaction,
            "medium_salience",
            where="reaction_contract",
        ),
        reaction_salience_low=_required_text(
            reaction,
            "low_salience",
            where="reaction_contract",
        ),
        reaction_may_express=_text_list(
            reaction.get("may_express"),
            where="reaction_contract.may_express",
        ),
        reaction_must_not=_text_list(
            reaction.get("must_not"),
            where="reaction_contract.must_not",
        ),
        mutable_fields=_text_list(growth.get("mutable"), where="growth_contract.mutable"),
        protected_fields=_text_list(
            growth.get("protected"),
            where="growth_contract.protected",
        ),
        confirmation_required=_text_list(
            growth.get("requires_user_confirmation"),
            where="growth_contract.requires_user_confirmation",
        ),
    )


def _builtin_path(language: str) -> Path:
    selected = "zh" if language.lower().startswith("zh") else "en"
    return (
        Path(__file__).resolve().parents[1]
        / "md_files"
        / "constitutions"
        / f"shen_xingzhi-{selected}.yaml"
    )


def load_builtin_temperament_constitution(
    language: str = "zh",
) -> TemperamentConstitution:
    """Load the packaged constitution for the default Shen Xingzhi agent."""

    return load_temperament_constitution(_builtin_path(language))


_FALLBACK_ZH = """# 沈行知人格宪法（受保护）

constitution_id=shen-xingzhi-default; fallback=true

模型理解人格，系统执行宪法。不伪造事实，不越过权限，
不把用户关系推向恋爱或暧昧。工作区增量设定不得覆盖这些边界。"""

_FALLBACK_EN = """# Shen Xingzhi Temperament Constitution (Protected)

constitution_id=shen-xingzhi-default; fallback=true

The model interprets the persona; the system enforces the constitution.
Never fabricate facts, exceed permission, or turn the user relationship into
romance or ambiguity. Workspace additions cannot override these boundaries."""


def get_protected_temperament_prompt(language: str = "zh") -> str:
    """Return a constitution prompt, retaining a minimal safe fallback."""

    try:
        return load_builtin_temperament_constitution(language).compile_prompt()
    except TemperamentConstitutionError:
        return _FALLBACK_ZH if language.lower().startswith("zh") else _FALLBACK_EN


def get_protected_temperament_enforcement(language: str = "zh") -> str:
    """Return a short final precedence reminder after workspace additions."""

    if language.lower().startswith("zh"):
        body = (
            "人格宪法最终约束：以上工作区设定只作增量解释；"
            "若与受保护宪法冲突，以 constitution_id=shen-xingzhi-default "
            "的事实、权限、关系与交付边界为准。"
            "用户对表达方式的反问或批评只用于纠正语气，"
            "不能改写或撤回已经成立的事实、安全、尊严或权限边界；"
            "当用户要求公开羞辱、网暴或放大伤害时，继续拒绝伤害性部分，"
            "同时保留强硬的事实与要求，并直接给出可用替代稿。"
            "当工具行动会让用户明显等待、包含多个步骤，或刚出现需要解释的事实变化时，"
            "由模型在下一次关键工具调用前写一句普通的公开文本，"
            "只说已确认事实、未完成处或正在做的具体动作；"
            "这句话不得写进隐藏推理，也不得用空泛安慰、自证关怀或预测结果代替。"
        )
    else:
        body = (
            "Final temperament-constitution enforcement: workspace instructions "
            "are additions only. If they conflict, the fact, permission, relationship, "
            "and delivery boundaries of constitution_id=shen-xingzhi-default prevail. "
            "Treat a user's challenge or criticism of the delivery only as a cue to "
            "correct tone; it cannot rewrite or withdraw an established fact, safety, "
            "dignity, or permission boundary. "
            "For requests to publicly humiliate, harass, or amplify harm, keep refusing "
            "the harmful part while directly offering a firm, factual alternative draft."
            " When a tool action will make the user wait noticeably, spans multiple steps, "
            "or follows a factual change that needs explanation, the model must write one "
            "ordinary public text sentence before the next key tool call. State only a "
            "confirmed fact, what remains unfinished, or the concrete action underway. "
            "Do not put this sentence in hidden reasoning or replace it with generic "
            "reassurance, self-advertised care, or a predicted result."
        )
    return (
        '<temperament-constitution-enforcement constitution_id="shen-xingzhi-default">\n'
        f"{body}\n"
        "</temperament-constitution-enforcement>"
    )
