"""Runtime Effect execution context contracts."""

from lantai.runtime.effect_context import (
    RuntimeEffectContext,
    bind_runtime_effect_context,
    current_runtime_effect_context,
)


def test_runtime_effect_context_is_scoped_and_restored() -> None:
    outer = RuntimeEffectContext(
        run_id="run-1",
        attempt_id="attempt-1",
        effect_id="effect-1",
        state_workspace_dir="/tmp/workspace-1",
    )
    inner = RuntimeEffectContext(
        run_id="run-2",
        attempt_id="attempt-2",
        effect_id="effect-2",
        state_workspace_dir="/tmp/workspace-2",
    )

    assert current_runtime_effect_context() is None
    with bind_runtime_effect_context(outer):
        assert current_runtime_effect_context() == outer
        with bind_runtime_effect_context(inner):
            assert current_runtime_effect_context() == inner
        assert current_runtime_effect_context() == outer
    assert current_runtime_effect_context() is None
