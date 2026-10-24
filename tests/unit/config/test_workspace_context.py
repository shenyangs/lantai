from pathlib import Path

from lantai.config.context import (
    WorkspaceContext,
    get_current_state_root,
    get_current_workspace_context,
    get_current_workspace_dir,
    set_current_workspace_context,
)


def test_workspace_context_separates_project_execution_from_runtime_state(
    tmp_path: Path,
) -> None:
    context = WorkspaceContext(
        agent_home=tmp_path / "managed" / "agents" / "default",
        state_root=tmp_path / "managed" / "topics" / "chat-1",
        project_root=tmp_path / "user-projects" / "demo",
        workspace_grant_id="wsg_example",
        access_mode="read_write",
        trust_state="trusted",
    )

    set_current_workspace_context(context)
    try:
        assert get_current_workspace_context() == context
        assert get_current_workspace_dir() == context.project_root
        assert get_current_state_root() == context.state_root
    finally:
        set_current_workspace_context(None)
