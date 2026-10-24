"""An append checks its revision without reparsing the entire journal."""

import json
import time

import pytest

import lantai.runtime.effects as effects
from tests.unit.runtime.test_effect_journal import _effect


@pytest.mark.parametrize("record_count", [100, 1000, 10000])
def test_effect_append_validation_scales_with_new_records(tmp_path, monkeypatch, record_count):
    validations = 0
    checksum = effects._checksum_payload

    def counted(payload):
        nonlocal validations
        validations += 1
        return checksum(payload)

    monkeypatch.setattr(effects, "_checksum_payload", counted)
    journal = effects.RuntimeEffectJournal(tmp_path)
    started = time.monotonic()
    for index in range(record_count):
        journal.append_effect(_effect().model_copy(update={"effect_id": f"effect-{index}"}))
    print(json.dumps({"records": record_count, "checksum_validations": validations, "seconds": round(time.monotonic() - started, 4)}))
    assert validations <= record_count * 4
    assert len(journal.list_effects("run-1", "attempt-1")) == record_count


def test_effect_index_rebuilds_after_another_writer_and_restart(tmp_path):
    first = effects.RuntimeEffectJournal(tmp_path)
    other = effects.RuntimeEffectJournal(tmp_path)
    first.append_effect(_effect())
    running = _effect(status="running", revision=1)
    other.append_effect(running)
    assert first.latest_effect("run-1", "attempt-1", "effect-1") == running
    with pytest.raises(effects.RuntimeEffectJournalConflictError):
        first.append_effect(running)
    restarted = effects.RuntimeEffectJournal(tmp_path)
    assert restarted.latest_effect("run-1", "attempt-1", "effect-1") == running


@pytest.mark.parametrize("damage", ["rewrite", "truncate", "replace", "append"])
def test_cached_effect_index_never_hides_journal_damage(tmp_path, damage):
    journal = effects.RuntimeEffectJournal(tmp_path)
    journal.append_effect(_effect())
    path = journal.ledger_path("run-1", "attempt-1")
    original = path.read_bytes()
    if damage == "rewrite":
        path.write_bytes(original.replace(b'"checksum":"sha256:', b'"checksum":"xxxxxx:', 1))
    elif damage == "truncate":
        path.write_bytes(original[:50])
    elif damage == "replace":
        replacement = path.with_suffix(".replacement")
        replacement.write_text("invalid json\n")
        replacement.replace(path)
    else:
        with path.open("ab") as handle:
            handle.write(b"invalid json\n")
    damaged = path.read_bytes()
    with pytest.raises(effects.RuntimeEffectJournalCorruptionError):
        journal.append_effect(_effect(status="running", revision=1))
    assert path.read_bytes() == damaged


def test_rebuilt_index_validates_checksum_and_monotonic_revision(tmp_path):
    journal = effects.RuntimeEffectJournal(tmp_path)
    journal.append_effect(_effect())
    path = journal.ledger_path("run-1", "attempt-1")
    envelope = json.loads(path.read_text())
    with path.open("a") as handle:
        handle.write(json.dumps(envelope) + "\n")
    with pytest.raises(effects.RuntimeEffectJournalCorruptionError, match="strictly increasing"):
        journal.list_effects("run-1", "attempt-1")
