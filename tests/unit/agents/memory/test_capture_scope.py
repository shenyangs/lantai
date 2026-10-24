from lantai.agents.capture_scope import should_capture_global_memory


def test_private_editor_turns_do_not_enter_global_memory() -> None:
    assert not should_capture_global_memory({"work_method_id": "private_editor"})
    assert not should_capture_global_memory(
        {"task_contract": {"work_method_id": "private_editor"}}
    )


def test_ordinary_turns_keep_existing_global_memory_capture() -> None:
    assert should_capture_global_memory({})
    assert should_capture_global_memory({"work_method_id": "standard"})
    assert should_capture_global_memory(
        {"task_contract": {"work_method_id": "collaboration"}}
    )
