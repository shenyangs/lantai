# -*- coding: utf-8 -*-
from __future__ import annotations

import json

from lantai.utils.redaction import redact_sensitive_value


def test_redact_sensitive_value_removes_secrets_and_private_paths() -> None:
    payload = redact_sensitive_value(
        {
            "api_key": "sk-secretsecretsecretsecret",
            "message": (
                "user@example.com opened /Users/example/private/file.txt "
                "https://example.com?a=secret"
            ),
            "id": "a" * 40,
        },
    )

    text = json.dumps(payload)
    assert "user@example.com" not in text
    assert "/Users/sam" not in text
    assert "secretsecret" not in text
    assert "aaaaaaaaaaaaaaaa" not in text
