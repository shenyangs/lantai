import json
import random
import tracemalloc

from lantai.app.experience import events as event_store


def _row(seq, *, identity=None, chat_id="chat", size=0):
    return {
        "id": identity or f"event-{seq}",
        "seq": seq,
        "ts": "2026-10-13T00:00:00Z",
        "agent_id": "default",
        "chat_id": chat_id,
        "run_id": "run",
        "kind": "chat.message.delta",
        "type": "message.delta",
        "status": "running",
        "summary": "synthetic",
        "payload": {"text_delta": "x" * size, "attempt_id": "attempt"},
    }


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_small_runtime_page_does_not_materialize_entire_ledger(tmp_path):
    path = event_store.events_path_for_workspace(tmp_path)
    _write(path, (_row(i, size=8192) for i in range(1, 2049)))
    # Separate the existing metadata index cost from payload page retention.
    event_store._runtime_event_stream_metadata(tmp_path)
    tracemalloc.start()
    try:
        response = event_store.build_runtime_events_response(
            tmp_path, "chat", limit=20
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [row.seq for row in response.events] == list(range(1, 21))
    assert response.has_more
    print(f"synthetic_page_peak_bytes={peak}")
    assert (
        peak < 4 * 1024 * 1024
    ), "one small page retained the full ledger payload"


def test_paged_replay_preserves_out_of_order_and_equal_sequence_rows(tmp_path):
    rows = [_row(i, identity=f"canonical-{i}") for i in range(1, 75)]
    rows += [_row(20, identity=f"tie-{i}") for i in range(10)]
    rows += [
        _row(i, identity=f"other-{i}", chat_id="other") for i in range(1, 75)
    ]
    random.Random(7).shuffle(rows)
    canonical = rows[:90]
    legacy = rows[90:]
    _write(event_store.events_path_for_workspace(tmp_path), canonical)
    _write(event_store.legacy_events_path_for_workspace(tmp_path), legacy)
    expected = sorted(
        (r for r in canonical + legacy if r["chat_id"] == "chat"),
        key=lambda r: r["seq"],
    )
    actual = []
    cursor = 0
    while True:
        page = event_store.build_runtime_events_response(
            tmp_path,
            "chat",
            after_seq=cursor,
            limit=7,
            event_types={"message.delta"},
            attempt_id="attempt",
        )
        actual.extend(row.id for row in page.events)
        assert page.next_after_seq > cursor
        cursor = page.next_after_seq
        if not page.has_more:
            break
    assert actual == [row["id"] for row in expected]
    assert cursor == 74

