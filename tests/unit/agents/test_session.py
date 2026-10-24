# -*- coding: utf-8 -*-
"""Tests for SafeJSONSession JSON corruption resilience."""
# pylint: disable=redefined-outer-name
import json
import os
import pathlib
import tempfile

import pytest

from lantai.exceptions import AgentStateError

from lantai.app.runner.session import (
    SafeJSONSession,
    migrate_legacy_weixin_session_files,
)


@pytest.fixture
def tmp_session_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sess(tmp_session_dir):
    return SafeJSONSession(save_dir=tmp_session_dir)


def _corrupt_file(path, valid_json, tail_garbage):
    """Write a valid JSON object followed by garbage bytes."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(valid_json + tail_garbage)


class FakeModule:
    """Minimal state module mock for testing."""

    def __init__(self):
        self.data = None

    def state_dict(self):
        return self.data

    def load_state_dict(self, d):
        self.data = d


# ── load_session_state ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_valid_json(sess, tmp_session_dir):
    """Normal case: valid JSON loads without error."""
    path = os.path.join(tmp_session_dir, "test--session.json")
    data = {"memory": {"content": ["hello"], "_compressed_summary": ""}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    mod = FakeModule()
    await sess.load_session_state(
        "test:session",
        user_id="",
        memory=mod,
    )
    assert mod.data == data["memory"]


@pytest.mark.asyncio
async def test_load_channel_session_migrates_flat_legacy_file(
    sess,
    tmp_session_dir,
):
    """Channel-scoped loads must keep old flat session files readable."""
    legacy_path = os.path.join(
        tmp_session_dir,
        "user-1_console--legacy.json",
    )
    data = {"memory": {"content": ["old-flat-session"]}}
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    mod = FakeModule()
    await sess.load_session_state(
        "console:legacy",
        user_id="user-1",
        channel="console",
        memory=mod,
    )

    target_path = os.path.join(
        tmp_session_dir,
        "console",
        "user-1_console--legacy.json",
    )
    assert mod.data == data["memory"]
    assert os.path.exists(legacy_path)
    assert os.path.exists(target_path)


@pytest.mark.asyncio
async def test_session_ids_with_sanitizer_collision_use_distinct_storage(
    sess,
    tmp_session_dir,
):
    """External IDs that sanitize alike must not overwrite each other."""

    left = FakeModule()
    left.data = {"content": ["colon"]}
    await sess.save_session_state("a:b", user_id="", memory=left)

    right = FakeModule()
    right.data = {"content": ["dashes"]}
    await sess.save_session_state("a--b", user_id="", memory=right)

    loaded_left = FakeModule()
    loaded_right = FakeModule()
    await sess.load_session_state("a:b", user_id="", memory=loaded_left)
    await sess.load_session_state("a--b", user_id="", memory=loaded_right)

    assert loaded_left.data == {"content": ["colon"]}
    assert loaded_right.data == {"content": ["dashes"]}
    assert len(list(pathlib.Path(tmp_session_dir).glob("*.json"))) == 2


@pytest.mark.asyncio
async def test_new_session_does_not_inherit_another_sessions_agent_state(
    sess,
):
    """A newly created chat starts empty even for the same user and channel."""

    old_agent = FakeModule()
    old_agent.data = {
        "memory": {
            "content": ["private transcript from the older conversation"],
        },
    }
    await sess.save_session_state(
        "console:older-chat",
        user_id="same-user",
        channel="console",
        agent=old_agent,
    )

    new_agent = FakeModule()
    await sess.load_session_state(
        "console:new-chat",
        user_id="same-user",
        channel="console",
        agent=new_agent,
    )

    assert new_agent.data is None
    assert await sess.get_session_state_dict(
        "console:new-chat",
        user_id="same-user",
        channel="console",
    ) == {}
    assert (
        await sess.get_session_state_dict(
            "console:older-chat",
            user_id="same-user",
            channel="console",
        )
    )["agent"] == old_agent.data


@pytest.mark.asyncio
async def test_legacy_raw_system_prompt_is_scrubbed_when_session_is_read(
    sess,
    tmp_session_dir,
):
    private_prompt = "PRIVATE_SYSTEM_PROMPT persona and tool rules"
    path = sess._get_save_path(
        "console:legacy-prompt",
        user_id="same-user",
        channel="console",
    )
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text(
        json.dumps(
            {
                "agent": {
                    "_sys_prompt": private_prompt,
                    "memory": {"content": ["ordinary user history"]},
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    restored = await sess.get_session_state_dict(
        "console:legacy-prompt",
        user_id="same-user",
        channel="console",
    )

    assert restored["agent"]["_sys_prompt"] == ""
    persisted = pathlib.Path(path).read_text(encoding="utf-8")
    assert private_prompt not in persisted
    assert "ordinary user history" in persisted


@pytest.mark.asyncio
async def test_session_channel_name_cannot_escape_save_dir(sess, tmp_session_dir):
    module = FakeModule()
    module.data = {"content": ["safe"]}
    save_root = pathlib.Path(tmp_session_dir).resolve()
    escaped_legacy_path = save_root.parent / "user_session.json"
    escaped_legacy_path.unlink(missing_ok=True)

    await sess.save_session_state(
        "session",
        user_id="user",
        channel="..",
        memory=module,
    )

    json_files = list(save_root.rglob("*.json"))
    assert json_files
    assert all(path.resolve().is_relative_to(save_root) for path in json_files)
    assert not escaped_legacy_path.exists()


@pytest.mark.asyncio
async def test_session_save_rejects_symlink_storage_file(sess, tmp_session_dir):
    module = FakeModule()
    module.data = {"content": ["safe"]}
    path = pathlib.Path(sess._get_save_path("session", user_id="", channel=""))
    outside = pathlib.Path(tmp_session_dir).parent / "outside-session.json"
    outside.write_text('{"outside": true}', encoding="utf-8")
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises((OSError, ValueError)):
        await sess.save_session_state("session", user_id="", memory=module)

    assert outside.read_text(encoding="utf-8") == '{"outside": true}'


def test_migrate_legacy_weixin_session_files_copies_canonical_and_archives(
    tmp_session_dir,
):
    """Legacy weixin session files must remain readable as wechat sessions."""
    legacy_path = os.path.join(
        tmp_session_dir,
        "user-1_weixin--friend@im.wechat.json",
    )
    data = {"memory": {"content": ["legacy-weixin-session"]}}
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    migrate_legacy_weixin_session_files(tmp_session_dir)

    canonical_path = os.path.join(
        tmp_session_dir,
        "user-1_wechat--friend@im.wechat.json",
    )
    archive_path = os.path.join(
        tmp_session_dir,
        ".weixin-legacy",
        "user-1_weixin--friend@im.wechat.json",
    )
    assert not os.path.exists(legacy_path)
    assert os.path.exists(canonical_path)
    assert os.path.exists(archive_path)
    with open(canonical_path, encoding="utf-8") as f:
        assert json.load(f) == data


@pytest.mark.asyncio
async def test_load_corrupted_json_extra_data(sess, tmp_session_dir):
    """Corrupted file with extra data after valid JSON should recover."""
    path = os.path.join(tmp_session_dir, "test--session.json")
    valid = json.dumps(
        {"memory": {"content": [], "_compressed_summary": ""}},
        ensure_ascii=False,
    )
    garbage = '=============="}}'
    _corrupt_file(path, valid, garbage)

    mod = FakeModule()
    await sess.load_session_state(
        "test:session",
        user_id="",
        memory=mod,
    )
    assert mod.data == {"content": [], "_compressed_summary": ""}


@pytest.mark.asyncio
async def test_load_corrupted_json_real_world_tail(sess, tmp_session_dir):
    """Real-world corruption pattern from QQ session (203-char tail)."""
    path = os.path.join(tmp_session_dir, "test--session.json")
    valid = json.dumps(
        {
            "memory": {"content": [], "_compressed_summary": ""},
            "toolkit": {"active_groups": []},
        },
        ensure_ascii=False,
    )
    # Actual garbage observed in production
    garbage = (
        "perform actions. A response without a tool call indicates "
        "the task is complete. To continue a task, you must generate "
        "a tool call or provide useful feedback if you are blocked."
        '\\n\\n===================="}}'
    )
    _corrupt_file(path, valid, garbage)

    mod_mem = FakeModule()
    mod_tool = FakeModule()
    await sess.load_session_state(
        "test:session",
        user_id="",
        memory=mod_mem,
        toolkit=mod_tool,
    )
    assert mod_mem.data == {"content": [], "_compressed_summary": ""}
    assert mod_tool.data == {"active_groups": []}


# ── get_session_state_dict ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_corrupted_json(sess, tmp_session_dir):
    """get_session_state_dict should recover from corrupted files."""
    path = os.path.join(tmp_session_dir, "test--session.json")
    valid = json.dumps(
        {"memory": {"content": ["x"], "_compressed_summary": ""}},
        ensure_ascii=False,
    )
    _corrupt_file(path, valid, "GARBAGE}}")

    result = await sess.get_session_state_dict(
        "test:session",
        user_id="",
    )
    assert "memory" in result
    assert result["memory"]["content"] == ["x"]


# ── update_session_state ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_corrupted_json(sess, tmp_session_dir):
    """update_session_state should recover corrupted file then write clean."""
    path = os.path.join(tmp_session_dir, "test--session.json")
    valid = json.dumps(
        {"memory": {"content": [], "_compressed_summary": ""}},
        ensure_ascii=False,
    )
    _corrupt_file(path, valid, "EXTRA")

    await sess.update_session_state(
        "test:session",
        key="memory.content",
        value=["updated"],
        user_id="",
        channel="",
    )

    # Verify the file is now clean JSON
    with open(path, encoding="utf-8") as f:
        result = json.load(f)
    assert result["memory"]["content"] == ["updated"]


# ── non-existent session ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_nonexistent(sess):
    """Non-existent session should not raise when allow_not_exist=True."""
    await sess.load_session_state(
        "no:exist",
        user_id="",
        memory=FakeModule(),
    )


@pytest.mark.asyncio
async def test_get_nonexistent(sess):
    """Non-existent session should return empty dict."""
    result = await sess.get_session_state_dict(
        "no:exist",
        user_id="",
    )
    assert result == {}


# ── completely corrupted file ──────────────────────────────────────


@pytest.mark.asyncio
async def test_load_completely_corrupted(sess, tmp_session_dir):
    """Damaged existing data must never become a fresh session silently."""
    path = pathlib.Path(tmp_session_dir) / "test--session.json"
    path.write_text('{{{THIS IS NOT JSON AT ALL!!!', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AgentStateError):
        await sess.load_session_state("test:session", memory=FakeModule())
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_get_completely_corrupted(sess, tmp_session_dir):
    """Damaged existing data must never become a fresh session silently."""
    path = pathlib.Path(tmp_session_dir) / "test--session.json"
    path.write_text('NOT JSON {{{{', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AgentStateError):
        await sess.get_session_state_dict("test:session")
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_update_completely_corrupted(sess, tmp_session_dir):
    """Damaged existing data must never become a fresh session silently."""
    path = pathlib.Path(tmp_session_dir) / "test--session.json"
    path.write_text('GARBAGE DATA !!!', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AgentStateError):
        await sess.update_session_state("test:session", "memory.content", ["new"])
    assert path.read_bytes() == original


# ── edge-case: empty file ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_empty_file(sess, tmp_session_dir):
    """Damaged existing data must never become a fresh session silently."""
    path = pathlib.Path(tmp_session_dir) / "test--session.json"
    path.write_text('', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AgentStateError):
        await sess.load_session_state("test:session", memory=FakeModule())
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_get_empty_file(sess, tmp_session_dir):
    """Damaged existing data must never become a fresh session silently."""
    path = pathlib.Path(tmp_session_dir) / "test--session.json"
    path.write_text('', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AgentStateError):
        await sess.get_session_state_dict("test:session")
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_update_empty_file(sess, tmp_session_dir):
    """Damaged existing data must never become a fresh session silently."""
    path = pathlib.Path(tmp_session_dir) / "test--session.json"
    path.write_text('', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AgentStateError):
        await sess.update_session_state("test:session", "memory.content", ["new"])
    assert path.read_bytes() == original


# ── edge-case: binary / null bytes ────────────────────────────────


@pytest.mark.asyncio
async def test_load_null_bytes(sess, tmp_session_dir):
    """Damaged existing data must never become a fresh session silently."""
    path = pathlib.Path(tmp_session_dir) / "test--session.json"
    path.write_text('\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AgentStateError):
        await sess.load_session_state("test:session", memory=FakeModule())
    assert path.read_bytes() == original


# ── edge-case: multiple concatenated JSON objects ──────────────────


@pytest.mark.asyncio
async def test_load_double_write_overlap(sess, tmp_session_dir):
    """Simulates race condition: two full JSON objects concatenated."""
    path = os.path.join(tmp_session_dir, "test--session.json")
    obj1 = json.dumps(
        {"memory": {"content": ["first"], "_compressed_summary": ""}},
    )
    obj2 = json.dumps(
        {"memory": {"content": ["second"], "_compressed_summary": ""}},
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(obj1 + obj2)

    mod = FakeModule()
    await sess.load_session_state("test:session", user_id="", memory=mod)
    # Should recover the FIRST object (raw_decode behavior)
    assert mod.data == {"content": ["first"], "_compressed_summary": ""}


# ── edge-case: only whitespace ────────────────────────────────────


@pytest.mark.asyncio
async def test_load_whitespace_only(sess, tmp_session_dir):
    """Damaged existing data must never become a fresh session silently."""
    path = pathlib.Path(tmp_session_dir) / "test--session.json"
    path.write_text('   \n\n\t  ', encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(AgentStateError):
        await sess.load_session_state("test:session", memory=FakeModule())
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [KeyError, TypeError, ValueError])
async def test_schema_failure_preserves_original_bytes_before_prompt_scrub(sess, error_type):
    path = pathlib.Path(sess._get_save_path('schema-failure', ''))
    original = b'{ "agent": {"_sys_prompt":"legacy private prompt", "history":["keep"]} }'
    path.write_bytes(original)

    class InvalidModule(FakeModule):
        def load_state_dict(self, data):
            assert data['_sys_prompt'] == ''
            raise error_type('memory')

    with pytest.raises(error_type, match='memory'):
        await sess.load_session_state('schema-failure', agent=InvalidModule())
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize('notebook', [None, 'legacy-invalid'])
async def test_notebook_repair_is_only_persisted_after_successful_load(sess, notebook):
    path = pathlib.Path(sess._get_save_path('legacy-notebook', ''))
    original = json.dumps({'agent': {'memory': ['keep'], 'plan_notebook': notebook}}).encode()
    path.write_bytes(original)

    class Notebook:
        def state_dict(self):
            return {'plans': []}

    class Module(FakeModule):
        plan_notebook = Notebook()
        fail = True

        def load_state_dict(self, data):
            assert data['plan_notebook'] == {'plans': []}
            assert data['memory'] == ['keep']
            if self.fail:
                raise KeyError('schema')
            self.data = data

    module = Module()
    with pytest.raises(KeyError):
        await sess.load_session_state('legacy-notebook', agent=module)
    assert path.read_bytes() == original
    module.fail = False
    await sess.load_session_state('legacy-notebook', agent=module)
    await sess.save_session_state('legacy-notebook', agent=module)
    restored = Module()
    restored.fail = False
    await sess.load_session_state('legacy-notebook', agent=restored)
    assert restored.data == module.data


@pytest.mark.asyncio
async def test_later_module_failure_does_not_commit_earlier_migration(sess):
    path = pathlib.Path(sess._get_save_path('multi-module', ''))
    original = b'{"agent":{"_sys_prompt":"old prompt","memory":["keep"]},"later":{}}'
    path.write_bytes(original)

    class Broken(FakeModule):
        def load_state_dict(self, data):
            raise ValueError('later module')

    first = FakeModule()
    with pytest.raises(ValueError, match='later module'):
        await sess.load_session_state('multi-module', agent=first, later=Broken())
    assert first.data['_sys_prompt'] == ''
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["broken", "", " \n\t", "\x00" * 32, "[]", "null", "42", '"text"', b"\xff\xfe"])
@pytest.mark.parametrize("operation", ["load", "get", "update"])
async def test_unrecoverable_session_rejects_without_overwrite(sess, content, operation):
    path = pathlib.Path(sess._get_save_path("unrecoverable", ""))
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    original = path.read_bytes()
    module = FakeModule()
    with pytest.raises(AgentStateError):
        if operation == "load":
            await sess.load_session_state("unrecoverable", memory=module)
        elif operation == "get":
            await sess.get_session_state_dict("unrecoverable")
        else:
            await sess.update_session_state("unrecoverable", "memory.content", ["new"])
    assert path.read_bytes() == original
    assert module.data is None


@pytest.mark.asyncio
async def test_missing_required_module_preserves_file_and_skips_all_modules(sess):
    path = pathlib.Path(sess._get_save_path("missing-module", ""))
    path.write_text(json.dumps({"agent": {"_sys_prompt": "private", "memory": {}}}))
    original = path.read_bytes()
    first = FakeModule()
    with pytest.raises(AgentStateError):
        await sess.load_session_state("missing-module", agent=first, memory=FakeModule())
    assert first.data is None
    assert path.read_bytes() == original
