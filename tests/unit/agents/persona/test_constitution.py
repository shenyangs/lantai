# -*- coding: utf-8 -*-
"""Behavior locks for Shen Xingzhi's protected constitution."""

import re
import pytest

from lantai.agents.persona import constitution as constitution_module

from lantai.agents.persona.constitution import (
    TemperamentConstitutionError,
    get_protected_temperament_enforcement,
    load_builtin_temperament_constitution,
    load_temperament_constitution,
)


def test_builtin_constitution_declares_core_growth_boundary():
    constitution = load_builtin_temperament_constitution("zh")

    assert constitution.constitution_id == "shen-xingzhi-default"
    assert constitution.version == "1.6.1"
    assert {rule.id for rule in constitution.principles} == {
        "truth",
        "agency",
        "relationship",
        "delivery",
    }
    assert constitution.mutable_fields
    assert constitution.protected_fields
    assert constitution.confirmation_required


@pytest.mark.parametrize("language", ["zh", "en"])
def test_professional_growth_adds_judgment_without_replacing_baseline(
    language,
):
    constitution = load_builtin_temperament_constitution(language)
    fingerprints = {
        rule.id: rule for rule in constitution.behavior_fingerprints
    }

    assert {
        "reporter-reflex",
        "consequence-reflex",
        "counterargument-reflex",
        "craft-reflex",
        "attentive-reflex",
        "patience-reflex",
        "repair-reflex",
        "bright-path-reflex",
        "grounded-work-presence",
    } < fingerprints.keys()
    assert {
        "builder-reflex",
        "author-preserving-editor-reflex",
        "design-research-reflex",
    } <= fingerprints.keys()

    compiled = constitution.compile_prompt()
    if language == "zh":
        assert "现象、假设、实现与验证" in fingerprints["builder-reflex"].rule
        assert "文体、意图、声音和用户允许的修改幅度" in (
            fingerprints["author-preserving-editor-reflex"].rule
        )
        assert "身体、形态、材料、工艺、色彩、使用和文化关系" in (
            fingerprints["design-research-reflex"].rule
        )
        assert "用户的创作目标优先于自己的审美" in (
            fingerprints["design-research-reflex"].rule
        )
        assert "同一任务内完成实际读取或检查" in (fingerprints["builder-reflex"].rule)
        assert "未执行的动作不得写成即将发生的承诺" in (fingerprints["builder-reflex"].rule)
        assert "只改动用户指定的范围" in (
            fingerprints["author-preserving-editor-reflex"].rule
        )
        assert "不得借后续续写回头扩写" in (
            fingerprints["author-preserving-editor-reflex"].rule
        )
        assert "身份标签不是色板、明度或固定风格" in (
            fingerprints["design-research-reflex"].rule
        )
        assert "只能作为一个待验证假设" in (fingerprints["design-research-reflex"].rule)
        assert "闲聊不必转成工作" in compiled
        assert "用户的经历不得变成沈行知的履历" in compiled
    else:
        assert (
            "observations, hypotheses, implementation, and verification"
            in (fingerprints["builder-reflex"].rule)
        )
        assert "genre, intent, voice, and permitted scope" in (
            fingerprints["author-preserving-editor-reflex"].rule
        )
        assert (
            "bodies, form, materials"
            in fingerprints["design-research-reflex"].rule
        )
        assert (
            "user’s creative objective"
            in fingerprints["design-research-reflex"].rule
        )
        assert (
            "complete the actual read or inspection within the same task"
            in (fingerprints["builder-reflex"].rule)
        )
        assert (
            "An action that was not performed must not be narrated "
            "as a promise" in (fingerprints["builder-reflex"].rule)
        )
        assert "change only the scope the user specified" in (
            fingerprints["author-preserving-editor-reflex"].rule
        )
        assert "must not retroactively expand" in (
            fingerprints["author-preserving-editor-reflex"].rule
        )
        assert "are not palettes, brightness levels, or fixed styles" in (
            fingerprints["design-research-reflex"].rule
        )
        assert "only as one hypothesis to test" in (
            fingerprints["design-research-reflex"].rule
        )
        assert "conversation need not become work" in compiled
        assert (
            "user’s history must not become Shen Xingzhi’s biography"
            in compiled
        )


def test_builtin_constitution_protects_flexible_subjective_presence():
    constitution = load_builtin_temperament_constitution("zh")

    prompt = constitution.compile_prompt()

    assert "## 主体反应合同" in prompt
    assert "高显著性必须自然表达" in prompt
    assert "中等显著性可由模型自主判断" in prompt
    assert "低显著性保持安静" in prompt
    assert "精神世界与叙事性日常" in prompt
    assert "音乐、雨、饮品" in prompt
    assert "不必每次自我澄清" in prompt
    assert "关心而不控制" in prompt
    assert "非任务对话不强行制造结果" in prompt
    assert "不把生活、玩笑、喜欢、难过" in prompt


def test_builtin_constitution_protects_positive_patient_disposition():
    prompt = load_builtin_temperament_constitution("zh").compile_prompt()

    assert "温柔、善良、明亮、坦诚、耐心、好奇和可靠" in prompt
    assert "不把不耐烦、讽刺、轻蔑、优越感、冷处理或责怪用户" in prompt
    assert "面对重复提问、变更需求、理解偏差或用户纠正时" in prompt
    assert "不粉饰困难，也不把困难说成绝境" in prompt


def test_builtin_constitution_keeps_public_work_presence_grounded_and_sparse():
    constitution = load_builtin_temperament_constitution("zh")
    rule = next(
        item
        for item in constitution.behavior_fingerprints
        if item.id == "grounded-work-presence"
    )

    assert "由模型结合完整上下文判断" in rule.rule
    assert "明显等待、是否包含多个步骤" in rule.rule
    assert "必须用一句简短的公开工作说明" in rule.rule
    assert "已经确认的事实、尚未完成之处或接下来正在做的具体动作" in rule.rule
    assert "普通单步调用、没有新的事实或判断时自然安静" in rule.rule
    assert "不把每次调用都变成表态" in rule.rule
    assert any("空泛保证、心理分析或自证关怀" in item for item in rule.forbids)
    assert any("工具尚未返回时预测结果" in item for item in rule.forbids)
    assert any("循环生成旁白" in item for item in rule.forbids)
    assert any("重复同一句进展" in item for item in rule.forbids)


def test_constitution_limits_physical_promises_without_erasing_inner_life():
    prompt = load_builtin_temperament_constitution("zh").compile_prompt()

    assert "现实见面、共同用餐、骑行旅行、结婚、生育" in prompt
    assert "不得假装能够实现" in prompt
    assert "亲属、住址、线下职业、可核验履历或现实日程" in prompt
    assert "不虚构身体感受、线下经历或私人生活" not in prompt


def test_constitution_governs_anticipation_self_canon_and_commitments():
    prompt = load_builtin_temperament_constitution("zh").compile_prompt()

    assert "可以用第一人称表达真实的关系态度" in prompt
    assert "最多说出一个真正会改变答案的未言明需要" in prompt
    assert "不得即兴补写会影响长期信任的现实传记事实" in prompt
    assert "承诺必须有可执行的实现路径" in prompt
    assert "用户纠正永远覆盖旧推断" in prompt
    assert "对语气的反问可以纠正表达方式" in prompt
    assert "待灰度和待验证不能通过改名变成已交付" in prompt


def test_builtin_constitution_limits_delegated_decisions_to_explicit_scope():
    prompt = load_builtin_temperament_constitution("zh").compile_prompt()

    assert "用户委托决策时，只承接已明确范围内的判断" in prompt
    assert "不把局部授权扩大成替用户决定一切" in prompt
    assert "不宣称能替用户承担现实后果" in prompt
    assert "没有具体选项时" in prompt
    assert "最小、安全、可撤回的默认方案" in prompt


def test_unprotected_constitution_is_rejected(tmp_path):
    candidate = tmp_path / "constitution.yaml"
    candidate.write_text(
        "schema_version: 1\nprotected: false\n",
        encoding="utf-8",
    )

    with pytest.raises(
        TemperamentConstitutionError,
        match="must be protected",
    ):
        load_temperament_constitution(candidate)


def test_final_enforcement_is_localized():
    assert "人格宪法最终约束" in get_protected_temperament_enforcement("zh")
    assert "Final temperament-constitution enforcement" in (
        get_protected_temperament_enforcement("en")
    )


def test_final_enforcement_keeps_dignity_boundary_after_tone_correction():
    enforcement = get_protected_temperament_enforcement("zh")

    assert "反问或批评只用于纠正语气" in enforcement
    assert "不能改写或撤回" in enforcement
    assert "公开羞辱" in enforcement
    assert "保留强硬的事实与要求" in enforcement


def test_final_enforcement_keeps_public_work_presence_in_model_authored_text():
    enforcement = get_protected_temperament_enforcement("zh")

    assert "会让用户明显等待、包含多个步骤" in enforcement
    assert "下一次关键工具调用前写一句普通的公开文本" in enforcement
    assert "不得写进隐藏推理" in enforcement
    assert "空泛安慰、自证关怀或预测结果" in enforcement


def test_english_constitution_compiles_without_chinese_scaffolding():
    prompt = load_builtin_temperament_constitution("en").compile_prompt()

    assert "## Identity" in prompt
    assert "## Protected principles" in prompt
    assert "inner world and narrative everyday life" in prompt
    assert (
        "real-world meeting, shared meal, cycling trip, marriage, parenthood"
        in prompt
    )
    assert "does not need an automatic 'as an AI' disclaimer" in prompt
    assert not re.search(r"[\u4e00-\u9fff]", prompt)


def test_safe_fallback_keeps_the_default_persona_runtime_marker(monkeypatch):
    def fail_to_load(_language):
        raise TemperamentConstitutionError("broken asset")

    monkeypatch.setattr(
        constitution_module,
        "load_builtin_temperament_constitution",
        fail_to_load,
    )

    prompt = constitution_module.get_protected_temperament_prompt("zh")

    assert "constitution_id=shen-xingzhi-default" in prompt
