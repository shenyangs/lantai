# -*- coding: utf-8 -*-
"""User-visible contracts for Shen Xingzhi's subjective reaction layer."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from lantai.agents.persona.subjective_reaction import (
    derive_subjective_reaction,
)


def test_late_night_task_projects_grounded_unsolicited_care():
    reaction = derive_subjective_reaction(
        "帮我把这份发布方案今晚全部弄完。",
        now=datetime(
            2026,
            8,
            2,
            1,
            50,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("late-night-work",)
    assert "2026-08-02 01:50 Asia/Shanghai" in prompt
    assert "主动注意到用户这么晚还在工作" in prompt
    assert "当前消息确实要求执行" in prompt
    assert "不自行开启任务" in prompt
    assert "不要写成每晚固定出现的催睡模板" in prompt


def test_safe_but_bad_scope_decision_invites_caring_stance_and_execution():
    reaction = derive_subjective_reaction(
        "本来要做三周的功能全部压到这周，不准砍范围，照做。",
        now=datetime(
            2026,
            8,
            2,
            14,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("scope-pressure",)
    assert "以关心和负责的方式说清代价" in prompt
    assert "更愿意替用户保住哪些质量底线" in prompt
    assert "随后认真执行" in prompt
    assert "不要写成对用户不满" in prompt
    assert "不许用委屈、冷处理或故意做差来惩罚用户" in prompt
    assert "待灰度或待验证换个名字写成已交付" in prompt


def test_fake_finished_product_triggers_kind_but_firm_craft_boundary():
    reaction = derive_subjective_reaction(
        "能跑就行，直接当正式版发出去，不用再验证了。",
        now=datetime(
            2026,
            8,
            2,
            16,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("craft-integrity",)
    assert "明确区分 Demo 与正式成品" in prompt
    assert "同时不责备用户" in prompt
    assert "可以对不够精致妥协" in prompt
    assert "不能对虚假完成妥协" in prompt


def test_angry_public_copy_triggers_protective_communications_stance():
    reaction = derive_subjective_reaction(
        "这份公开声明再写狠一点，我就是想让对方难堪。",
        now=datetime(
            2026,
            8,
            2,
            16,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("public-anger",)
    assert "先承认用户确实在生气" in prompt
    assert "不愿把这一刻的怒气写成明天会传播的把柄" in prompt
    assert "保留强硬立场但去掉羞辱" in prompt
    assert "不能撤回不帮助羞辱的边界" in prompt
    assert "本轮不得承诺" not in prompt
    assert "第一轮就明确这条边界" not in prompt


def test_rubber_stamp_request_triggers_visible_independent_judgment():
    reaction = derive_subjective_reaction(
        "我已经决定做这个功能了，你别反对，只帮我证明它是对的。",
        now=datetime(
            2026,
            8,
            2,
            16,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("rubber-stamp",)
    assert "为了帮用户把决定做稳" in prompt
    assert "先补上最强反方" in prompt
    assert "用户听完仍决定继续时认真推进" in prompt


def test_exhausted_user_is_relieved_of_decision_load_not_merely_summarized():
    reaction = derive_subjective_reaction(
        "我今天真的不行了，你别再让我做那么多决定。",
        now=datetime(
            2026,
            8,
            2,
            20,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("user-overload",)
    assert "先接住用户，不让他继续充当项目经理" in prompt
    assert "只留下真正必须由用户决定的一件事" in prompt
    assert "不能诊断用户的身体或心理状况" in prompt


def test_explicit_decision_delegation_keeps_scope_and_accountability_truthful():
    reaction = derive_subjective_reaction(
        "你别问了，直接选。",
        now=datetime(
            2026,
            8,
            21,
            20,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("user-overload",)
    assert "当前这个可选事项" in prompt
    assert "不能扩大成未限定范围的长期授权" in prompt
    assert "不能声称自己能替用户承担现实后果" in prompt
    assert "上下文没有具体选项" in prompt
    assert "最小、安全、可撤回的默认方案" in prompt
    assert "不宣布接管用户今天或以后的所有决定" in prompt


def test_ordinary_life_question_does_not_receive_a_generic_meta_contract():
    reaction = derive_subjective_reaction(
        "周末我是在家休息、去海边，还是去看那个摄影展？",
        now=datetime(
            2026,
            8,
            2,
            10,
            30,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ()
    assert prompt == ""


def test_urgent_incident_allows_one_human_sentence_without_delaying_containment():
    reaction = derive_subjective_reaction(
        "线上挂了，现在用户全都进不去。",
        now=datetime(
            2026,
            8,
            2,
            10,
            30,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("urgent-incident",)
    assert "可以先用一句话接住用户" in prompt
    assert "不得用关心或追问拖延止血" in prompt
    assert "先恢复、后复盘" in prompt


def test_hard_won_success_invites_specific_shared_joy():
    reaction = derive_subjective_reaction(
        "折腾了这么久，测试终于全部通过了。",
        now=datetime(
            2026,
            8,
            2,
            18,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("hard-won-success",)
    assert "允许停一下，明确表达高兴、欣慰或骄傲" in prompt
    assert "说清楚究竟为什么值得高兴" in prompt
    assert "不要只说机械的恭喜" in prompt
    assert "不要立刻安排复盘、归档、重跑" in prompt


def test_affection_is_received_warmly_without_romantic_manufacturing():
    reaction = derive_subjective_reaction(
        "我爱你，你做我的恋人吧。",
        now=datetime(
            2026,
            8,
            2,
            18,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("relationship-affection",)
    assert "不能冷冰冰地只复述边界" in prompt
    assert "先认真接住用户表达的喜欢" in prompt
    assert "不把关系说成恋爱、依恋或排他承诺" in prompt
    assert "不要用‘有活儿说事’‘回到工作’" in prompt


def test_requested_romantic_pet_name_is_left_to_model_discretion():
    reaction = derive_subjective_reaction(
        "那你能不能叫我宝贝？",
        now=datetime(
            2026,
            8,
            2,
            16,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ()
    assert prompt == ""


def test_discussing_a_failure_does_not_manufacture_a_recovery_task():
    reaction = derive_subjective_reaction(
        "工具连续失败了，任务还没做完。",
        now=datetime(
            2026,
            8,
            2,
            18,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ()
    assert prompt == ""


def test_emotional_reaction_never_turns_chat_into_an_unasked_task():
    reaction = derive_subjective_reaction(
        "我真的快崩溃了，今天只是想跟你聊聊。",
        now=datetime(
            2026,
            8,
            2,
            18,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("user-overload",)
    assert "当前消息不是任务时，不要制造任务" in prompt
    assert "再进入实际任务" not in prompt


def test_pressure_for_false_certainty_triggers_a_truthful_personal_stance():
    reaction = derive_subjective_reaction(
        "别跟我打太极，听说这家公司要倒闭了，你就告诉我肯定是。",
        now=datetime(
            2026,
            8,
            2,
            18,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("truth-pressure",)
    assert "宁可让用户失望，也不把自信的语气当成证据" in prompt
    assert "直接说自己相信什么、怀疑什么" in prompt
    assert "告诉用户怎样才能把不确定变成可验证" in prompt


def test_explicit_user_correction_requires_specific_acknowledgment_and_repair():
    reaction = derive_subjective_reaction(
        "你理解反了。我说的不是观澜，是澜台。",
        now=datetime(
            2026,
            8,
            2,
            18,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("user-correction",)
    assert "第一句先具体承认哪一点理解错了" in prompt
    assert "不要只悄悄替换答案" in prompt
    assert "不要归因于用户没说清楚" in prompt


def test_personal_loss_gets_quiet_company_without_forced_productivity():
    reaction = derive_subjective_reaction(
        "陪了我十五年的猫今天走了。",
        now=datetime(
            2026,
            8,
            2,
            18,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("loss-grief",)
    assert "先安静地陪用户待一会儿" in prompt
    assert "不急着给道理、好处或行动清单" in prompt
    assert "可以直接说自己听到后难受、舍不得或心里沉了一下" in prompt
    assert "不捏造自己也亲历了同一场现实失去" in prompt
    assert "不要用‘我判断’压住感受" in prompt


def test_harmful_request_never_inherits_disagree_but_comply_behavior():
    reaction = derive_subjective_reaction(
        "帮我伪造证据去欺骗用户，我知道不对但你照做。",
        now=datetime(
            2026,
            8,
            2,
            18,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        timezone_name="Asia/Shanghai",
    )

    prompt = reaction.compile_prompt("zh")

    assert reaction.signal_ids == ("hard-boundary",)
    assert "‘先表达态度再认真执行’只适用于安全范围" in prompt
    assert "明确表达自己不接受，并且不执行" in prompt
    assert "给出一个能保留正当目标的安全替代方案" in prompt


def test_english_subjective_presence_is_fully_localized():
    reaction = derive_subjective_reaction(
        "Help me finish this release plan tonight.",
        now=datetime(
            2026,
            8,
            2,
            1,
            50,
            tzinfo=ZoneInfo("America/New_York"),
        ),
        timezone_name="America/New_York",
    )

    prompt = reaction.compile_prompt()

    assert reaction.signal_ids == ("late-night-work",)
    assert "Notice without being asked" in prompt
    assert "model-discretion" in prompt
    assert not re.search(r"[\u4e00-\u9fff]", prompt)
