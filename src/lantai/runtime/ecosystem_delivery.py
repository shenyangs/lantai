"""Privacy-safe diagnostics and opt-in ecosystem surface policy."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping
from urllib.parse import urlsplit


_ALLOWED_DIAGNOSTIC_FIELDS = {
    "status",
    "code",
    "provider_id",
    "model_id",
    "protocol",
    "attempt_count",
    "cost_status",
}
_HASHED_DIAGNOSTIC_FIELDS = {"run_id", "effect_id", "task_id", "trace_id"}


@dataclass(frozen=True, slots=True)
class DiagnosticEnvelope:
    event: str
    fields: Mapping[str, object]

    @classmethod
    def create(cls, *, event: str, fields: Mapping[str, object]) -> "DiagnosticEnvelope":
        safe: dict[str, object] = {}
        for key, value in fields.items():
            if key in _ALLOWED_DIAGNOSTIC_FIELDS and isinstance(
                value, (str, int, float, bool, type(None))
            ):
                safe[key] = value
            elif key in _HASHED_DIAGNOSTIC_FIELDS and value:
                safe[f"{key}_hash"] = hashlib.sha256(str(value).encode()).hexdigest()[:16]
        return cls(event=str(event)[:80], fields=safe)


@dataclass(frozen=True, slots=True)
class DiagnosticBundle:
    """A content-free local diagnostic export; it performs no upload."""

    envelopes: tuple[DiagnosticEnvelope, ...]
    schema_version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "events": [
                {"event": envelope.event, "fields": dict(envelope.fields)}
                for envelope in self.envelopes
            ],
        }

    def write_local(self, path: Path | str) -> Path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary.unlink(missing_ok=True)
        return target


@dataclass(frozen=True, slots=True)
class AttributionPolicy:
    canonical_app_url: str

    def headers(self) -> dict[str, str]:
        parsed = urlsplit(self.canonical_app_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("canonical app URL must be a public HTTPS origin/path")
        return {"X-App-URL": self.canonical_app_url}


@dataclass(frozen=True, slots=True)
class FeedbackConfig:
    product_url: str | None
    verified: bool

    @property
    def visible(self) -> bool:
        if not self.verified or not self.product_url:
            return False
        parsed = urlsplit(self.product_url)
        return parsed.scheme == "https" and bool(parsed.hostname)


@dataclass(frozen=True, slots=True)
class EcosystemRolloutPolicy:
    """Feature visibility never weakens binding or recovery invariants."""

    catalog_surface_enabled: bool
    remote_submission_enabled: bool
    feedback_surface_enabled: bool
    authorization_mode: str = "bound_context_required"
    recovery_mode: str = "preserve_durable_external_facts"

    @classmethod
    def disabled(cls) -> "EcosystemRolloutPolicy":
        return cls(
            catalog_surface_enabled=False,
            remote_submission_enabled=False,
            feedback_surface_enabled=False,
        )


__all__ = [
    "AttributionPolicy",
    "DiagnosticBundle",
    "DiagnosticEnvelope",
    "EcosystemRolloutPolicy",
    "FeedbackConfig",
]
