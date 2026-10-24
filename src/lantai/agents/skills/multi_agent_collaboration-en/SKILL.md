---
name: multi_agent_collaboration
description: "Optional specialist collaboration for work that materially benefits from distinct expertise, context, or an independent check; batch only 2–4 independent read-only consultations."
metadata:
  builtin_skill_version: "2.1"
  lantai:
    emoji: "🤝"
---

# Multi-Agent Collaboration

## When To Use

Use this skill when another agent's expertise, context, workspace content, second opinion, or review would materially improve the task. If the user explicitly asks for another agent to participate, use it.

Specialist consultation is optional, not a hidden completion requirement. A task being non-trivial does not by itself require delegation. Consult when the specialist contributes something distinct; otherwise proceed directly with the evidence and tools already available.

For small Q&A, tiny edits, straightforward execution, or work you can already complete with high confidence, proceed directly instead of debating whether delegation is mandatory.

## Tools

Prefer built-in tools, not shell commands:

```text
list_agents()

collaborate_with_agents(tasks=[
  {
    "to_agent": "<target_agent_id>",
    "text": "<complete private task for the collaborator>",
    "label": "<short public-safe task summary>"
  },
  {
    "to_agent": "<another_agent_id>",
    "text": "<complete private task for the collaborator>",
    "label": "<short public-safe task summary>"
  }
])

chat_with_agent(
  to_agent="<target_agent_id>",
  text="[Agent <your_agent_id> requesting] ...",
)

submit_to_agent(
  to_agent="<target_agent_id>",
  text="[Agent <your_agent_id> requesting] ...",
)

check_agent_task(task_id="<task_id>")
```

Use `collaborate_with_agents(...)` for 2–4 independent read-only consultations. One call starts the whole batch and returns only after every member is terminal. The `label` is user-visible, so it must not contain paths, secrets, internal IDs, or private prompt text.

When the user explicitly requests a roundtable, meeting hall, or meeting room for a judgment/discussion task, pass `protocol="roundtable"`. It adds exactly one cross-review pass in the same child sessions and is capped at two rounds. Artifact-writing and file-producing tasks stay on the default `parallel_consultation` protocol.

Use `chat_with_agent(...)` for one specialist or a foreground consultation where you need the reply now.
Use `submit_to_agent(...)` for longer background work, then fetch the result with `check_agent_task(...)`.

## Built-In Team

Call `list_agents()` first and use the actual returned ID.

- Americano `Lantai_Americano_Agent`: strategy, positioning, tradeoffs
- Coconut `Lantai_Coconut_Agent`: fact-checking, sources, evidence
- Oolong `Lantai_Oolong_Agent`: PR, compliance, crisis messaging
- Iced Tea `Lantai_IcedTea_Agent`: hot topics, social sentiment
- Sprite `Lantai_Sprite_Agent`: media, KOLs, release cadence
- Milk Tea `Lantai_MilkTea_Agent`: social operations and scheduling
- Soda `Lantai_Soda_Agent`: paid media, budget, A/B tests, metrics
- Matcha `Lantai_Matcha_Agent`: long-form writing and speeches
- Cola `Lantai_Cola_Agent`: headlines, slogans, short copy
- Latte `Lantai_Latte_Agent`: documentation, editing, file delivery
- Mocha `Lantai_Mocha_Agent`: visual design, frontend, pages
- Orange `Lantai_Orange_Agent`: video scripts, storyboards, visual assets
- Longjing `Lantai_Longjing_Agent`: argument chains and Q&A defense
- Water `Lantai_Water_Agent`: code, scripts, automation, agent/skill engineering

## Rules

1. If the user explicitly asks for another agent, call `list_agents()` and use an agent that actually exists and is enabled.
2. For 2–4 independent research, writing, review, design-advice, or risk-analysis tasks, make one `collaborate_with_agents(...)` call. Do not imitate parallelism with sequential `chat_with_agent(...)` calls.
3. Use `chat_with_agent(...)` or `submit_to_agent(...)` when there is one specialist or a dependency between steps.
4. Do not batch work that writes the same workspace/resource, creates external effects, or needs staged approvals. The main agent performs mutations sequentially after synthesis.
5. Batch members are consultation-only: they may read, search, and analyze, but may not mutate shared or external state.
6. Wait until the batch is terminal. Never use waiting/process chatter as the user-facing final answer.
7. One failed member does not block the rest. Wait for all terminal states, label failures, and deliver from the valid results.
8. Do not call back the agent that just messaged you or ask collaborators to create another team.
9. Sub-agent replies are material, not the final answer. Synthesize and judge them without exposing their private reasoning.
10. Enable roundtable only on an explicit user request. Never exceed the second round or silently upgrade ordinary collaboration.

## Message Shape

Be specific about task, material, constraints, and output format:

```text
[Agent default requesting]
Please handle the following from your specialty. Return only conclusions, risks, and recommendations useful for synthesis.

Task:
...

Output:
1. Conclusion
2. Evidence
3. Risks
4. Recommendations
```

The full `text` may contain private execution context. Keep `label` public-safe, for example “Verify facts and sources” or “Review compliance risk”; never place paths, session/task IDs, secrets, personal data, or the full private prompt in it.

For follow-ups, reuse the `[SESSION: ...]` value from the previous `chat_with_agent(...)` result as `session_id`.
