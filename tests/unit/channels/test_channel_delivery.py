# -*- coding: utf-8 -*-
"""Durable ingress, retry and dead-letter behavior locks."""

from __future__ import annotations

from lantai.app.channels.delivery import ChannelDeliveryLedger


def _payload(message_id: str = "m-1") -> dict:
    return {
        "sender_id": "u-1",
        "content_parts": [{"type": "text", "text": "hello"}],
        "meta": {"message_id": message_id, "reply_future": object()},
    }


def test_external_message_id_is_idempotent_across_reopen(tmp_path):
    path = tmp_path / "deliveries.sqlite3"
    first = ChannelDeliveryLedger(path)

    accepted = first.register("telegram", "telegram:u-1", 20, _payload())
    duplicate = first.register("telegram", "telegram:u-1", 20, _payload())

    assert accepted.accepted is True
    assert accepted.delivery_id
    assert duplicate.accepted is False
    assert duplicate.delivery_id == accepted.delivery_id

    reopened = ChannelDeliveryLedger(path)
    persisted = reopened.list_pending()
    assert [item.delivery_id for item in persisted] == [
        accepted.delivery_id,
    ]
    # Ephemeral futures are deliberately excluded from crash replay data.
    assert "reply_future" not in persisted[0].payload["meta"]


def test_failed_delivery_moves_to_dead_letter_and_can_be_requeued(tmp_path):
    ledger = ChannelDeliveryLedger(tmp_path / "deliveries.sqlite3", max_attempts=3)
    delivery = ledger.register("feishu", "feishu:u-1", 20, _payload("m-2"))

    assert ledger.record_failure(delivery.delivery_id, "boom-1") == "pending"
    assert ledger.record_failure(delivery.delivery_id, "boom-2") == "pending"
    assert ledger.record_failure(delivery.delivery_id, "boom-3") == "dead"

    dead = ledger.list_dead_letters()
    assert len(dead) == 1
    assert dead[0].attempts == 3
    assert dead[0].last_error == "boom-3"

    replay = ledger.requeue(dead[0].delivery_id)
    assert replay.status == "pending"
    assert replay.attempts == 0
    assert ledger.list_dead_letters() == []


def test_completed_delivery_is_not_replayed(tmp_path):
    ledger = ChannelDeliveryLedger(tmp_path / "deliveries.sqlite3")
    delivery = ledger.register("discord", "discord:u-1", 20, _payload("m-3"))

    ledger.mark_delivered(delivery.delivery_id)

    assert ledger.list_pending() == []
    duplicate = ledger.register("discord", "discord:u-1", 20, _payload("m-3"))
    assert duplicate.accepted is False
    assert duplicate.status == "delivered"
