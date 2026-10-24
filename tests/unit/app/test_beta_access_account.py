from __future__ import annotations

from lantai.app.beta_access import account


def test_uses_raw_member_uuid_and_preserves_account_epoch(monkeypatch) -> None:
    member_id = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(
        account,
        "load_cloud_account",
        lambda: {
            "provider": "watcha",
            "user": {"id": member_id},
            "refresh_token": "encrypted-store-already-decoded",
            "access_token": "short-lived-bearer",
            "account_epoch": 9,
        },
    )

    session = account.load_beta_member_session()

    assert session.context.account_id == member_id
    assert session.context.account_id != f"watcha:{member_id}"
    assert session.context.account_epoch == 9
    assert session.access_token == "short-lived-bearer"


def test_invalid_or_logged_out_account_cannot_select_a_license(monkeypatch) -> None:
    monkeypatch.setattr(
        account,
        "load_cloud_account",
        lambda: {
            "provider": "watcha",
            "user": {"id": "not-a-uuid"},
            "access_token": "bearer-must-not-leak-through",
            "account_epoch": 4,
        },
    )

    session = account.load_beta_member_session()

    assert session.context.account_id is None
    assert session.context.account_epoch == 4
    assert session.access_token is None
