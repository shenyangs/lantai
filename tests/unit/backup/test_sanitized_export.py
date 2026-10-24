import json

import pytest
from pydantic import ValidationError

from lantai.backup._ops.create_helpers import (
    REDACTED_FILE_BYTES,
    REDACTED_SECRET_VALUE,
    sanitized_file_payload,
)
from lantai.backup.models import BackupMeta, BackupScope, RestoreBackupRequest
from lantai.backup._ops.restore import (
    _preserve_sanitized_files,
    _restore_redacted_values,
    _validate_requested_restore_scope,
)


def test_sanitized_json_removes_nested_credentials(tmp_path):
    source = tmp_path / "agent.json"
    source.write_text(
        json.dumps(
            {
                "name": "writer",
                "channels": {
                    "telegram": {
                        "bot_token": "real-token",
                        "accessToken": "camel-token",
                        "enabled": True,
                    }
                },
                "mcp": {"clients": [{"authorization": "Bearer secret"}]},
                "provider": {"key": "AIzaSyExampleCredential123456789"},
            }
        ),
        encoding="utf-8",
    )

    payload = sanitized_file_payload(source, "agent.json")

    assert payload is not None
    assert b"real-token" not in payload
    assert b"camel-token" not in payload
    assert b"AIzaSyExampleCredential" not in payload
    assert b"Bearer secret" not in payload
    redacted = json.loads(payload)
    assert (
        redacted["channels"]["telegram"]["bot_token"] == REDACTED_SECRET_VALUE
    )
    assert (
        redacted["channels"]["telegram"]["accessToken"]
        == REDACTED_SECRET_VALUE
    )
    assert redacted["provider"]["key"] == REDACTED_SECRET_VALUE
    assert redacted["name"] == "writer"


def test_sanitized_json_preserves_non_secret_key_fields(tmp_path):
    source = tmp_path / "layout.json"
    source.write_text(
        json.dumps(
            {
                "card_template_key": "content",
                "shortcut": {"key": "Enter"},
            }
        ),
        encoding="utf-8",
    )

    assert sanitized_file_payload(source, "layout.json") is None


def test_sanitized_structured_text_preserves_non_secret_key(tmp_path):
    source = tmp_path / "layout.yaml"
    source.write_text("key: title\nmode: compact\n", encoding="utf-8")

    assert sanitized_file_payload(source, "layout.yaml") is None


def test_sanitized_structured_text_fails_closed_on_unstructured_secret(
    tmp_path,
):
    source = tmp_path / "mixed.yaml"
    source.write_text(
        "api_key: sk-examplecredential123456\n"
        "note: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature\n",
        encoding="utf-8",
    )

    assert sanitized_file_payload(source, "mixed.yaml") == REDACTED_FILE_BYTES


def test_sanitized_sensitive_file_uses_marker(tmp_path):
    source = tmp_path / ".env"
    source.write_text("TOKEN=secret", encoding="utf-8")
    assert sanitized_file_payload(source, ".env") == REDACTED_FILE_BYTES


def test_sanitized_structured_text_redacts_only_credential_value(tmp_path):
    source = tmp_path / "settings.yaml"
    source.write_text(
        "clientSecret: abcdefgh12345678\nmode: safe\n", encoding="utf-8"
    )

    payload = sanitized_file_payload(source, "settings.yaml")
    assert payload is not None
    assert b"abcdefgh12345678" not in payload
    assert b"clientSecret: __LANTAI_REDACTED_SECRET__" in payload
    assert b"mode: safe" in payload


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("settings.yaml", "auth: AIzaSyExampleCredential123456789\n"),
        (
            "notes.txt",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature\n",
        ),
        ("config.toml", 'dsn="postgres://alice:password123@db.example/app"\n'),
    ],
)
def test_sanitized_text_scanner_removes_common_secret_material(
    tmp_path,
    filename,
    content,
):
    source = tmp_path / filename
    source.write_text(content, encoding="utf-8")

    payload = sanitized_file_payload(source, filename)
    assert payload is not None
    assert content.encode("utf-8").strip() not in payload


def test_sanitized_symlink_uses_marker_without_reading_target(tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("must-not-leak", encoding="utf-8")
    source = tmp_path / "workspace-link.txt"
    source.symlink_to(target)

    assert (
        sanitized_file_payload(source, "workspace-link.txt")
        == REDACTED_FILE_BYTES
    )


def test_sanitized_scope_cannot_include_secret_store():
    with pytest.raises(
        ValidationError, match="cannot include the secret store"
    ):
        BackupScope(sanitized=True, include_secrets=True)


def test_restore_request_cannot_exceed_sanitized_archive_scope():
    meta = BackupMeta(
        name="safe export",
        scope=BackupScope(
            include_agents=True,
            include_global_config=True,
            include_secrets=False,
            include_skill_pool=True,
            sanitized=True,
        ),
    )
    request = RestoreBackupRequest(
        include_agents=False,
        include_global_config=False,
        include_secrets=True,
        include_skill_pool=False,
    )

    with pytest.raises(ValueError, match="include_secrets is unavailable"):
        _validate_requested_restore_scope(meta, request)


def test_sanitized_restore_preserves_local_secrets(tmp_path):
    current = tmp_path / "current"
    staged = tmp_path / "staged"
    current.mkdir()
    staged.mkdir()
    (current / ".env").write_text("TOKEN=local", encoding="utf-8")
    (staged / ".env").write_bytes(REDACTED_FILE_BYTES)
    (current / "agent.json").write_text(
        json.dumps({"name": "old", "api_key": "local-key"}),
        encoding="utf-8",
    )
    (staged / "agent.json").write_text(
        json.dumps({"name": "new", "api_key": REDACTED_SECRET_VALUE}),
        encoding="utf-8",
    )

    _preserve_sanitized_files(staged, current)

    assert (staged / ".env").read_text(encoding="utf-8") == "TOKEN=local"
    restored = json.loads((staged / "agent.json").read_text(encoding="utf-8"))
    assert restored == {"name": "new", "api_key": "local-key"}
    assert _restore_redacted_values(REDACTED_SECRET_VALUE, "kept") == "kept"


def test_sanitized_restore_preserves_local_only_credentials(tmp_path):
    current = tmp_path / "current"
    staged = tmp_path / "staged"
    current.mkdir()
    staged.mkdir()
    (current / ".env.local").write_text("TOKEN=local-only", encoding="utf-8")
    (current / "agent.json").write_text(
        json.dumps(
            {
                "name": "old",
                "providers": [
                    {"id": "openai", "apiKey": "local-key", "model": "old"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (staged / "agent.json").write_text(
        json.dumps(
            {
                "name": "new",
                "providers": [{"id": "openai", "model": "new"}],
            }
        ),
        encoding="utf-8",
    )

    _preserve_sanitized_files(staged, current)

    assert (staged / ".env.local").read_text(
        encoding="utf-8"
    ) == "TOKEN=local-only"
    restored = json.loads((staged / "agent.json").read_text(encoding="utf-8"))
    assert restored["name"] == "new"
    assert restored["providers"] == [
        {"id": "openai", "model": "new", "apiKey": "local-key"}
    ]


def test_sanitized_restore_preserves_strong_secret_under_generic_key(tmp_path):
    current = tmp_path / "current"
    staged = tmp_path / "staged"
    current.mkdir()
    staged.mkdir()
    (current / "provider.json").write_text(
        json.dumps({"key": "AIzaSyLocalCredential123456789", "mode": "old"}),
        encoding="utf-8",
    )
    (staged / "provider.json").write_text(
        json.dumps({"key": REDACTED_SECRET_VALUE, "mode": "new"}),
        encoding="utf-8",
    )

    _preserve_sanitized_files(staged, current)

    restored = json.loads(
        (staged / "provider.json").read_text(encoding="utf-8")
    )
    assert restored == {
        "key": "AIzaSyLocalCredential123456789",
        "mode": "new",
    }


def test_sanitized_restore_merges_structured_text_credentials(tmp_path):
    current = tmp_path / "current"
    staged = tmp_path / "staged"
    current.mkdir()
    staged.mkdir()
    (current / "settings.yaml").write_text(
        "auth: local-credential-123\nmode: old\n",
        encoding="utf-8",
    )
    (staged / "settings.yaml").write_text(
        f"auth: {REDACTED_SECRET_VALUE}\nmode: new\n",
        encoding="utf-8",
    )

    _preserve_sanitized_files(staged, current)

    assert (staged / "settings.yaml").read_text(encoding="utf-8") == (
        "auth: local-credential-123\nmode: new\n"
    )


def test_sanitized_restore_preserves_local_symlink(tmp_path):
    current = tmp_path / "current"
    staged = tmp_path / "staged"
    current.mkdir()
    staged.mkdir()
    (current / "target.txt").write_text("local", encoding="utf-8")
    (current / "link.txt").symlink_to("target.txt")
    (staged / "link.txt").write_bytes(REDACTED_FILE_BYTES)

    _preserve_sanitized_files(staged, current)

    assert (staged / "link.txt").is_symlink()
    assert (staged / "link.txt").readlink().as_posix() == "target.txt"
