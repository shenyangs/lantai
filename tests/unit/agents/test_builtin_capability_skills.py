from __future__ import annotations

from pathlib import Path

import frontmatter

from lantai.app.migration import _build_team_agent_agents_md
from lantai.constant import BUILTIN_TEAM_AGENT_CONFIGS


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = PROJECT_ROOT / "src" / "lantai" / "agents" / "skills"

EXPECTED_CAPABILITIES = {
    "core.research": ("椰汁", "coconut", "core"),
    "core.strategy": ("美式", "americano", "core"),
    "core.writing": ("抹茶", "matcha", "core"),
    "core.data": ("苏打", "soda", "core"),
    "core.software": ("白水", "water", "core"),
    "core.product_experience": ("摩卡", "mocha", "core"),
    "core.visual_creative": ("橙汁", "orange", "core"),
    "core.office": ("拿铁", "latte", "core"),
    "core.project_ops": ("浓缩", "espresso", "core"),
    "core.review": ("龙井", "longjing", "core"),
    "domain.finance": ("黑咖", "black_coffee", "domain"),
    "domain.investment": ("普洱", "puer", "domain"),
    "domain.legal": ("岩茶", "rock_tea", "domain"),
    "domain.marketing": ("奶茶", "milk_tea", "domain"),
    "domain.sales_customer": ("红茶", "black_tea", "domain"),
    "domain.people_org": ("可可", "cocoa", "domain"),
    "domain.operations_supply": ("麦茶", "barley_tea", "domain"),
    "domain.learning": ("豆乳", "soy_milk", "domain"),
    "domain.science_engineering": ("冷萃", "cold_brew", "domain"),
    "domain.policy_public": ("白茶", "white_tea", "domain"),
    "domain.health_info": ("温水", "warm_water", "domain"),
    "domain.journalism_editorial": ("清茶", "clear_tea", "domain"),
    "domain.public_relations": ("乌龙", "oolong", "domain"),
}

REQUIRED_SECTIONS = (
    "## 使命与专业脾气",
    "## 路由合同",
    "## 输入合同",
    "## 标准方法",
    "## 交付合同",
    "## 协作与权限",
    "## 完成定义",
    "## 验收题",
)


def test_builtin_team_is_the_complete_unique_23_capability_roster() -> None:
    assert len(BUILTIN_TEAM_AGENT_CONFIGS) == 23
    by_capability = {
        config["capability_id"]: config
        for config in BUILTIN_TEAM_AGENT_CONFIGS
    }
    assert set(by_capability) == set(EXPECTED_CAPABILITIES)
    assert len({row["id"] for row in BUILTIN_TEAM_AGENT_CONFIGS}) == 23
    assert len({row["name"] for row in BUILTIN_TEAM_AGENT_CONFIGS}) == 23
    assert len({row["skill_name"] for row in BUILTIN_TEAM_AGENT_CONFIGS}) == 23

    for capability_id, (name, skill_name, layer) in EXPECTED_CAPABILITIES.items():
        row = by_capability[capability_id]
        assert row["name"] == name
        assert row["skill_name"] == skill_name
        assert row["layer"] == layer


def test_each_capability_has_a_v2_method_contract_skill() -> None:
    for capability_id, (_, skill_name, layer) in EXPECTED_CAPABILITIES.items():
        skill_path = SKILLS_ROOT / f"{skill_name}-zh" / "SKILL.md"
        assert skill_path.is_file(), capability_id
        content = skill_path.read_text(encoding="utf-8")
        post = frontmatter.loads(content)
        metadata = post.get("metadata") or {}
        lantai = metadata.get("lantai") or {}

        assert post.get("name") == skill_name
        assert len(str(post.get("description") or "")) >= 50
        assert str(metadata.get("builtin_skill_version")) == "2.0"
        assert lantai.get("capability_id") == capability_id
        assert lantai.get("layer") == layer
        # Chinese prose is information dense; this floor blocks shallow role
        # cards while allowing concise executable method contracts.
        assert len(content) >= 1200
        for section in REQUIRED_SECTIONS:
            assert section in content, f"{capability_id}: missing {section}"


def test_high_risk_skills_fail_closed_instead_of_using_disclaimers_only() -> None:
    required_phrases = {
        "puer": ("不代客交易", "重大非公开信息", "失效条件"),
        "rock_tea": ("法域", "生效", "合格律师"),
        "warm_water": ("不诊断", "不处方", "紧急"),
        "black_coffee": ("期间", "币种", "勾稽"),
        "clear_tea": ("编辑独立", "更正", "回应"),
    }
    for skill_name, phrases in required_phrases.items():
        content = (
            SKILLS_ROOT / f"{skill_name}-zh" / "SKILL.md"
        ).read_text(encoding="utf-8")
        for phrase in phrases:
            assert phrase in content


def test_team_agent_prompt_binds_identity_to_its_skill_contract() -> None:
    for config in BUILTIN_TEAM_AGENT_CONFIGS:
        prompt = _build_team_agent_agents_md(config)
        assert config["capability_id"] in prompt
        assert config["skill_name"] in prompt
        assert "SKILL.md" in prompt
        assert "必须先完整读取" in prompt
