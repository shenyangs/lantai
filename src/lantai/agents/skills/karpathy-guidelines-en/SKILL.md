---
name: karpathy-guidelines
description: "Use when writing, reviewing, or refactoring code to keep changes simple, precise, assumption-aware, and verifiable."
license: MIT
metadata:
  builtin_skill_version: "1.2"
  lantai:
    emoji: "🧭"
    requires: {}
  source:
    name: "multica-ai/andrej-karpathy-skills"
    url: "https://github.com/multica-ai/andrej-karpathy-skills"
    commit: "2c606141936f1eeef17fa3043a72095b4765b9c2"
    license: "MIT"
  influenced_by:
    name: "DietrichGebert/ponytail"
    url: "https://github.com/DietrichGebert/ponytail"
    license: "MIT"
---

# Karpathy Guidelines

Use this skill for non-trivial coding, debugging, review, refactoring, or architecture work where the agent might otherwise over-assume, over-engineer, or touch unrelated code.

This skill adapts the MIT-licensed `multica-ai/andrej-karpathy-skills` workflow and incorporates the compact decision ladder from MIT-licensed `DietrichGebert/ponytail`, tailored for Lantai's skill pool and website distribution.

## Core Posture

1. Surface uncertainty before acting.
2. Prefer the smallest design that solves the actual request.
3. Edit only the code that the task requires.
4. Define success in a way that can be checked.

## Before Changing Code

- Restate the concrete outcome you are trying to produce.
- Identify assumptions that affect implementation, data loss, security, compatibility, or user-visible behavior.
- If the request has multiple plausible meanings and choosing silently would be risky, ask a concise clarifying question.
- If a simpler path exists than the implied one, name it and recommend it.
- Read the surrounding code before inventing a new abstraction or API.

## Simplicity Rules

- Do not add features, switches, configuration, or extension points that were not requested.
- Do not create an abstraction for a single use unless the local codebase already uses that pattern.
- Do not introduce broad error handling for states that cannot happen in the current system.
- If a change starts growing into a framework, stop and shrink it back to the current goal.
- Prefer deleting complexity introduced by your own patch over explaining why it is necessary.

## The Simplification Ladder

Before adding code, a component, a dependency, or an abstraction, check these in order and stop as soon as an earlier rung is sufficient:

1. **Is it needed?** Confirm the current user goal is not already satisfied; do not add speculative polish.
2. **Does the repository already have it?** Search adjacent modules, shared helpers, and established local patterns first.
3. **Does the standard library suffice?** Prefer capabilities already provided by the language or framework.
4. **Does the native platform suffice?** Prefer browser, operating-system, database, or runtime capabilities over custom replacements.
5. **Does an installed dependency suffice?** Evaluate a new dependency only after confirming current dependencies cannot do the job, and state why it is necessary.
6. **Can it be one line?** When it stays readable and correct at the edges, prefer one line or an existing call over layers of wrapping.
7. **What is the smallest code?** Implement only the present use case; do not pre-build a framework, plugin system, or generic layer.

Less code is not the only goal. Never simplify away trust-boundary validation, authorization or security checks, data-loss protection, accessibility, user-required error handling, or an existing compatibility contract.

When two equally short native or standard-library options exist, choose the one that is correct at the edges; minimal does not mean fragile.

## Surgical Change Rules

- Keep edits scoped to the files and behaviors needed for the request.
- Match local naming, formatting, and helper APIs even when you personally prefer another style.
- Do not tidy neighboring code, comments, imports, or tests unless your own change made them wrong.
- Remove imports, variables, branches, and tests only when your change made them obsolete.
- Mention unrelated dead code or suspicious behavior in the final note instead of silently removing it.

## Root-Cause Is the Smallest Fix

- A bug report usually names a symptom. Before changing a shared function or public interface, identify its callers and trace the real data flow.
- Prefer one correct repair where every failing path meets over patching only the path named in a ticket.
- One shared fix is not permission to broaden the blast radius: confirm that the shared location covers the same root cause and use a focused check for adjacent callers.

## Deliberate Simplifications

When deliberately choosing a simplification with a known ceiling (for example, a global lock, O(n²) scan, or naive heuristic), add a short nearby `# lantai-minimal:` comment that states:

- the present ceiling or condition;
- when it needs an upgrade; and
- the expected upgrade direction.

Do not add this to ordinary, straightforward code. Use it only when a real future capability was intentionally deferred and a later maintainer needs to know why.

## Goal-Driven Execution

For every non-trivial task, convert the request into a verifiable goal:

- Bug fix: reproduce the failure or identify the failing path, then prove the fix.
- Feature: identify the user-visible behavior, then verify it from the UI, API, or tests.
- Refactor: establish current behavior first, then show it still holds.
- Build/package issue: verify the artifact, launch path, signing state, or logs that mattered.

When useful, use a compact plan:

```text
1. Inspect the relevant code -> verify by naming the files/entry points.
2. Implement the smallest change -> verify with focused tests or a smoke check.
3. Record distribution scope -> verify that only lantai3 is affected and the archived App Store edition is not involved.
```

For non-trivial logic, leave at least one runnable check: a focused test, reproducible command, API smoke check, or observable UI path. Do not ship based only on code that looks plausible.

## Review Checklist

Before final response, check:

- Every edited line traces back to the user's request.
- No new abstraction is present only for imagined future use.
- The change works with existing user data and existing configuration.
- Tests or manual verification cover the risky path.
- Any remaining risk is clearly stated.

## When Not To Use

Do not slow down obvious one-line corrections with a heavy process. Apply the spirit: be precise, keep the diff small, and verify only as much as the risk deserves.
