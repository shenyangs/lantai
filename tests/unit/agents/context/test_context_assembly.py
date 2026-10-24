# -*- coding: utf-8 -*-

from lantai.agents.context.context_assembly import ContextAssembler, ContextAssemblyInput
from lantai.agents.context.context_budget import ContextBudgetPlanner, ContextCandidate, ModelAwareTokenCounter
from lantai.agents.context.model_context_profile import ModelContextProfile


def test_context_assembly_keeps_security_order_and_current_user_message_last():
    assembler = ContextAssembler(ContextBudgetPlanner(ModelAwareTokenCounter()))
    profile = ModelContextProfile.from_mapping(user_window=4096)
    selected, manifest = assembler.assemble(
        profile=profile,
        requested_output_tokens=300,
        input=ContextAssemblyInput(
            system_rules="system", tool_schemas="tools", stable_memory="confirmed",
            recent_history="recent", current_message="please answer",
            dynamic_memory=[ContextCandidate("dynamic_memory", "memory", relevance=1)],
            wiki_evidence=[ContextCandidate("wiki_evidence", "<untrusted-wiki-evidence>doc</untrusted-wiki-evidence>", relevance=1, trust=.2)],
        ),
    )

    assert [block.category for block in selected] == [
        "system", "tool_schema", "stable_memory", "history", "dynamic_memory",
        "wiki_evidence", "current_message",
    ]
    assert manifest.total_upper_bound <= 4096
