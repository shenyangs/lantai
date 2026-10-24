---
name: lean-code-review
description: "Use when the user asks whether code is over-engineered, what can be deleted, whether a standard/native capability should replace it, or requests a simplification review; return actionable subtraction advice without changing code."
license: MIT
metadata:
  builtin_skill_version: "1.0"
  lantai:
    emoji: "✂️"
    requires: {}
  influenced_by:
    name: "DietrichGebert/ponytail"
    url: "https://github.com/DietrichGebert/ponytail"
    license: "MIT"
---

# Lean Code Review

Use when the user asks whether code is over-engineered, what can be deleted, whether it reinvents an existing capability, or requests a simplification review. This is a complement to normal correctness and security review, not a replacement.

## Review Target

Find only complexity that can be removed, replaced, or shrunk:

- dead code, unused flexibility, or speculative features;
- hand-written behavior already in the standard library;
- a dependency or custom code replacing a native platform feature;
- an abstraction supported by one implementation, caller, or never-set configuration value; and
- equivalent logic that can be materially shorter.

Read the complete diff and the necessary context first so that behavior and boundaries are understood; short is not automatically correct. Tests, input validation, authorization/security, data protection, accessibility, and declared compatibility constraints are not disposable boilerplate.

## Output Contract

Default to read-only; do not apply changes. Put each finding on one line:

```text
<file>:L<line or range>: <delete|stdlib|native|yagni|shrink>: <what to remove>. <replacement or “nothing”.>
```

End with `estimated net reduction: -N lines`; if it cannot be estimated safely, say so. If nothing should be removed, output only: `Lean already. Ship.`

## Boundaries

- Hunt removable complexity only. Route correctness bugs, performance bottlenecks, and security vulnerabilities to normal review instead of mixing them into this list.
- Do not recommend removing focused tests or the smallest smoke check; they are the verification floor for a lean implementation.
- If the user asks to apply a finding, return to the coding workflow, assess each impact, and run the appropriate verification.
