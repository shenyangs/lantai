import json
import os
import random
import tracemalloc

import pytest

from lantai.app.experience import events as event_store
from lantai.app.runner import api


def _row(seq, identity, *, size=0, chat_id="chat"):
    return {
        "id": identity,
        "seq": seq,
        "ts": "2026-10-13T00:00:00Z",
        "agent_id": "default",
        "chat_id": chat_id,
        "run_id": "run",
        "kind": "chat.message.delta",
        "type": "message.delta",
        "status": "running",
        "summary": "synthetic",
        "payload": {"text_delta": "x" * size},
    }


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_projector_restore_does_not_retain_history_payloads(tmp_path):
    _write(
        event_store.events_path_for_workspace(tmp_path),
        (_row(i, f"e-{i}", size=8192) for i in range(1, 2049)),
    )
    event_store._runtime_event_stream_metadata(tmp_path)
    tracemalloc.start()
    try:
        api._public_reasoning_projector_at_cursor(tmp_path, "chat", 2048)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f"synthetic_projector_restore_peak_bytes={peak}")
    assert peak < 4 * 1024 * 1024


def test_projector_replay_preserves_global_order_and_ties(
    tmp_path, monkeypatch
):
    rows = [_row(i % 40 + 1, f"e-{i}") for i in range(100)]
    rows += [_row(i, f"other-{i}", chat_id="other") for i in range(1, 40)]
    random.Random(29).shuffle(rows)
    _write(event_store.events_path_for_workspace(tmp_path), rows[:70])
    _write(event_store.legacy_events_path_for_workspace(tmp_path), rows[70:])
    expected = [
        e.model_dump(mode="json")
        for e in event_store.build_runtime_events_response(
            tmp_path, "chat"
        ).events
        if e.seq <= 25
    ]
    seen = []

    class RecordingProjector:
        def project(self, event):
            seen.append(event.model_dump(mode="json"))

    monkeypatch.setattr(api, "PublicReasoningProjector", RecordingProjector)
    api._public_reasoning_projector_at_cursor(tmp_path, "chat", 25)
    assert seen == expected


def test_replay_pins_active_inode_across_rotation(tmp_path):
    path = event_store.events_path_for_workspace(tmp_path)
    _write(path, [_row(i, f"old-{i}") for i in range(1, 4)])
    replay = event_store.iter_runtime_events_through_seq(tmp_path, "chat", 3)
    assert next(replay).id == "old-1"
    path.rename(path.with_suffix(".rotated"))
    _write(path, [_row(i, f"new-{i}") for i in range(1, 4)])
    assert [event.id for event in replay] == ["old-2", "old-3"]


def _observe_fds(monkeypatch):
    original = event_store._open_ledger_read_fd
    descriptors = set()
    maximum = []

    def still_open(fd):
        try:
            os.fstat(fd)
            return True
        except OSError:
            return False

    def tracked_open(path):
        fd = original(path)
        descriptors.add(fd)
        maximum.append(sum(still_open(item) for item in descriptors))
        return fd

    monkeypatch.setattr(event_store, "_open_ledger_read_fd", tracked_open)
    return descriptors, maximum, still_open


def test_replay_bounds_file_handles_and_closes_early(tmp_path, monkeypatch):
    paths = [tmp_path / f"segment-{i}.jsonl" for i in range(30)] + [
        event_store.events_path_for_workspace(tmp_path),
        event_store.legacy_events_path_for_workspace(tmp_path),
    ]
    for i, path in enumerate(paths):
        _write(path, [_row(i + 1, f"e-{i}")])
    monkeypatch.setattr(
        event_store,
        "_event_ledger_paths_for_workspace_after_seq",
        lambda *_: paths,
    )
    descriptors, maximum, still_open = _observe_fds(monkeypatch)
    replay = event_store.iter_runtime_events_through_seq(tmp_path, "chat", 100)
    assert next(replay).id == "e-0"
    assert max(maximum) <= 3
    replay.close()
    assert not any(still_open(fd) for fd in descriptors)


def test_replay_rejects_replaced_immutable_segment(tmp_path, monkeypatch):
    paths = [tmp_path / "one.jsonl", tmp_path / "two.jsonl"]
    _write(paths[0], [_row(1, "one"), _row(3, "three")])
    _write(paths[1], [_row(2, "two")])
    monkeypatch.setattr(
        event_store,
        "_event_ledger_paths_for_workspace_after_seq",
        lambda *_: paths,
    )
    descriptors, _, still_open = _observe_fds(monkeypatch)
    replay = event_store.iter_runtime_events_through_seq(tmp_path, "chat", 3)
    assert next(replay).id == "one"
    replacement = tmp_path / "replacement.jsonl"
    _write(replacement, [_row(1, "new-one"), _row(3, "new-three")])
    replacement.replace(paths[0])
    assert next(replay).id == "two"
    with pytest.raises(RuntimeError, match="segment changed"):
        next(replay)
    assert not any(still_open(fd) for fd in descriptors)


def test_replay_parses_each_row_at_most_twice(tmp_path, monkeypatch):
    path = event_store.events_path_for_workspace(tmp_path)
    _write(path, [_row(i, f"e-{i}") for i in range(1, 2001)])
    monkeypatch.setattr(
        event_store,
        "_event_ledger_paths_for_workspace_after_seq",
        lambda *_: [path],
    )
    original = event_store.ChatExperienceEvent.model_validate
    count = 0

    def counted(cls, value, *args, **kwargs):
        nonlocal count
        count += 1
        return original(value, *args, **kwargs)

    monkeypatch.setattr(
        event_store.ChatExperienceEvent,
        "model_validate",
        classmethod(counted),
    )
    assert (
        sum(
            1
            for _ in event_store.iter_runtime_events_through_seq(
                tmp_path,
                "chat",
                1000,
            )
        )
        == 1000
    )
    assert count == 3000


def test_projector_failure_closes_replay_files(tmp_path, monkeypatch):
    path = event_store.events_path_for_workspace(tmp_path)
    _write(path, [_row(1, "one"), _row(2, "two")])
    event_store._runtime_event_stream_metadata(tmp_path)
    descriptors, _, still_open = _observe_fds(monkeypatch)

    class FailingProjector:
        def project(self, _):
            raise RuntimeError("synthetic projection failure")

    monkeypatch.setattr(api, "PublicReasoningProjector", FailingProjector)
    with pytest.raises(RuntimeError, match="synthetic projection failure"):
        api._public_reasoning_projector_at_cursor(tmp_path, "chat", 2)
    assert not any(still_open(fd) for fd in descriptors)
