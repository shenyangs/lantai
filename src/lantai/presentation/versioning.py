# -*- coding: utf-8 -*-
"""Single-writer version ownership for native presentation artifacts.

The PPTX on disk is authoritative.  DeckIR is a revision/provenance model, not
permission to overwrite a newer file.  Every writer must first prove that its
base matches the latest recorded PPTX and DeckIR revision.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .models import DeckIR, PresentationVersion, QualityReport
from .quality import file_sha256, is_verified_delivery
from .render import slide_image_paths


PresentationWriter = Literal[
    "native_pptx",
    "pptxgenjs_v3",
    "legacy_python_pptx",
    "lantai_ai_edit",
    "genoffice_human",
    "office_cli",
    "external_reconcile",
    "version_rollback",
]


class StalePresentationVersionError(RuntimeError):
    """Raised when a writer would overwrite a newer or unreconciled PPTX."""


@dataclass(frozen=True)
class PresentationPublicationCheckpoint:
    """Exact ledger state before one lease's unpublished result."""

    parent: PresentationVersion | None
    current_bytes: bytes | None
    history_bytes: bytes | None


def deck_ir_version(deck: DeckIR) -> str:
    """Return a deterministic version id for the semantic deck checkpoint."""

    return _payload_version(_version_payload(deck))


def _version_payload(deck: DeckIR) -> dict:
    payload = deck.model_dump(mode="json")
    # ``show_legend`` was added after version-ledger snapshots were already in
    # circulation.  Pydantic materializes the new optional default as ``null``
    # when an older checkpoint is read, even though the persisted semantics did
    # not change.  Ignore only that absent-value migration noise so an existing
    # owned PPTX remains writable; explicit ``True``/``False`` values still
    # participate in the hash and therefore create a new semantic revision.
    for slide in payload.get("slides", []):
        for obj in slide.get("objects", []):
            if obj.get("kind") != "chart":
                continue
            if obj.get("show_legend") is None:
                obj.pop("show_legend", None)
            # Empty proofs add no calculation semantics, even after JSON reload.
            # Nonempty proofs remain part of the semantic fingerprint.
            if obj.get("calculation_proofs") == []:
                obj.pop("calculation_proofs", None)
    return payload


def _payload_version(payload: dict) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _absent_native_defaults_payload(deck: DeckIR) -> dict:
    """Reconstruct only absent, neutral defaults from pre-native snapshots."""

    payload = _version_payload(deck)
    for slide, slide_payload in zip(deck.slides, payload["slides"], strict=True):
        if "semantic_layout" not in slide.model_fields_set and slide.semantic_layout is None:
            slide_payload.pop("semantic_layout", None)
        for obj, obj_payload in zip(slide.objects, slide_payload["objects"], strict=True):
            defaults = {
                "text": {"native_metric_text": None},
                "shape": {"runs": [], "native_style": None},
                "chart": {"category_axis": None, "value_axis": None, "plot_layout": None},
                "table": {"cell_styles": []},
            }.get(obj.kind, {})
            for field, neutral in defaults.items():
                if field not in obj.model_fields_set and obj_payload.get(field) == neutral:
                    obj_payload.pop(field, None)
            if obj.kind == "shape" and obj.native_style is not None:
                style = obj.native_style
                style_payload = obj_payload["native_style"]
                for field in (
                    "align", "valign", "margin_top", "margin_right", "margin_bottom", "margin_left",
                ):
                    if field not in style.model_fields_set and style_payload.get(field) is None:
                        style_payload.pop(field, None)
    return payload


def deck_ir_matches_version(deck: DeckIR, expected: str) -> bool:
    """Accept only documented absent-default schema vintages, without migration.

    Keep the current fingerprint unchanged. Pre-template snapshots omitted
    ``template_binding`` and/or the slide ``template_anchors`` defaults. Only
    null values may be omitted. Native-style and metric-text defaults may be
    omitted only when absent in the loaded checkpoint and still neutral;
    explicit values and all other semantics remain hashed. No checkpoint or
    append-only ledger is rewritten.
    """

    payload = _version_payload(deck)
    if _payload_version(payload) == expected:
        return True
    for candidate in (payload, _absent_native_defaults_payload(deck)):
        if _payload_version(candidate) == expected:
            return True
        for omit_binding, omit_anchors in ((True, False), (False, True), (True, True)):
            legacy = dict(candidate)
            if omit_binding and legacy.get("template_binding") is None:
                legacy.pop("template_binding", None)
            if omit_anchors:
                legacy["slides"] = [
                    {
                        key: value for key, value in slide.items()
                        if key != "template_anchors" or value is not None
                    }
                    for slide in candidate.get("slides", [])
                ]
            if _payload_version(legacy) == expected:
                return True
    return False


class PresentationVersionStore:
    """Append-only version ledger scoped to one presentation job."""

    def __init__(self, job_dir: str | Path) -> None:
        self.root = Path(job_dir).resolve() / "versions"
        self.current_path = self.root / "current.json"
        self.history_path = self.root / "history.jsonl"

    def current(self) -> PresentationVersion | None:
        if not self.current_path.is_file():
            return None
        return PresentationVersion.model_validate_json(
            self.current_path.read_text(encoding="utf-8"),
        )

    def history(self) -> list[PresentationVersion]:
        if not self.history_path.is_file():
            return []
        versions: list[PresentationVersion] = []
        for line in self.history_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                versions.append(PresentationVersion.model_validate_json(line))
        return versions

    def get(self, version_id: str) -> PresentationVersion:
        for version in reversed(self.history()):
            if version.version_id == version_id:
                return version
        raise KeyError(f"unknown presentation version: {version_id}")

    def publication_checkpoint(
        self, *, parent: PresentationVersion | None,
    ) -> PresentationPublicationCheckpoint:
        if self.current() != parent:
            raise StalePresentationVersionError("publication parent changed")
        return PresentationPublicationCheckpoint(
            parent=parent,
            current_bytes=self._optional_bytes(self.current_path),
            history_bytes=self._optional_bytes(self.history_path),
        )

    def rollback_unpublished_record(
        self,
        checkpoint: PresentationPublicationCheckpoint,
        *,
        writer: PresentationWriter,
        generation: int,
        base_pptx_sha256: str | None,
        expected_pptx_sha256: str,
    ) -> bool:
        """Undo only this lease's unannounced suffix, never committed history.

        The caller holds the job writer and publication locks. A changed
        prefix, second writer, or different generation fails closed. Immutable
        snapshots of older published versions are never removed.
        """

        current_bytes = self._optional_bytes(self.current_path)
        history_bytes = self._optional_bytes(self.history_path)
        if (current_bytes, history_bytes) == (
            checkpoint.current_bytes, checkpoint.history_bytes,
        ):
            return True
        prefix = checkpoint.history_bytes or b""
        if history_bytes is None or not history_bytes.startswith(prefix):
            return False
        lines = history_bytes[len(prefix):].splitlines()
        if len(lines) != 1:
            return False
        try:
            unpublished = PresentationVersion.model_validate_json(lines[0])
        except ValueError:
            return False
        if (
            unpublished.writer != writer
            or unpublished.generation != generation
            or unpublished.parent_version_id != (
                checkpoint.parent.version_id if checkpoint.parent else None
            )
            or unpublished.base_pptx_sha256 != base_pptx_sha256
            or unpublished.pptx_sha256 != expected_pptx_sha256
            or current_bytes not in (
                checkpoint.current_bytes,
                unpublished.model_dump_json(indent=2).encode("utf-8"),
            )
        ):
            return False
        self._restore_ledger_file(self.current_path, checkpoint.current_bytes)
        self._restore_ledger_file(self.history_path, checkpoint.history_bytes)
        # This bundle belongs exclusively to the removed, never-published
        # entry. Keep the edit preview elsewhere as diagnostic evidence.
        bundle = (self.root / "artifacts" / unpublished.version_id).resolve()
        if bundle.parent == (self.root / "artifacts").resolve() and bundle.is_dir():
            shutil.rmtree(bundle)
        return True

    @staticmethod
    def _optional_bytes(path: Path) -> bytes | None:
        return path.read_bytes() if path.is_file() else None

    @staticmethod
    def _write_ledger_file(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _restore_ledger_file(cls, path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
        else:
            cls._write_ledger_file(path, content)

    def assert_writer_base(
        self,
        pptx_path: str | Path,
        deck: DeckIR,
        *,
        expected_pptx_sha256: str | None = None,
    ) -> PresentationVersion | None:
        """Prove the exact current PPTX and DeckIR are safe mutation inputs."""

        path = Path(pptx_path).resolve()
        current = self.current()
        if current is None:
            if path.exists():
                raise StalePresentationVersionError(
                    "existing PPTX has no version ownership record; reconcile or re-import before writing",
                )
            return None
        if not path.is_file():
            raise StalePresentationVersionError(
                "the latest recorded PPTX is missing; refusing a stale write",
            )
        actual = file_sha256(path)
        if expected_pptx_sha256 and actual != expected_pptx_sha256:
            raise StalePresentationVersionError(
                "the PPTX changed after the requested base version",
            )
        if actual != current.pptx_sha256:
            raise StalePresentationVersionError(
                "the PPTX was modified outside the recorded writer chain; reconcile or re-import before writing",
            )
        if not current.reconciled:
            raise StalePresentationVersionError(
                "the latest PPTX contains unreconciled human or external edits",
            )
        if not deck_ir_matches_version(deck, current.deck_ir_version):
            raise StalePresentationVersionError(
                "DeckIR base hash does not match the latest PPTX version; reconcile or re-import before writing",
            )
        return current

    def record(
        self,
        pptx_path: str | Path,
        deck: DeckIR,
        *,
        writer: PresentationWriter,
        parent: PresentationVersion | None = None,
        generation: int | None = None,
        base_pptx_sha256: str | None = None,
        expected_pptx_sha256: str | None = None,
        quality_report_path: str | Path | None = None,
        render_dir: str | Path | None = None,
    ) -> PresentationVersion:
        """Record a verified writer result after its atomic save completes."""

        path = Path(pptx_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        current = self.current()
        expected_parent = current.version_id if current else None
        supplied_parent = parent.version_id if parent else None
        if expected_parent != supplied_parent:
            raise StalePresentationVersionError(
                "version parent changed while the presentation was being written",
            )
        expected_base = current.pptx_sha256 if current else None
        if base_pptx_sha256 is not None and base_pptx_sha256 != expected_base:
            raise StalePresentationVersionError(
                "writer lease base no longer matches the authoritative version",
            )
        actual_pptx_sha256 = file_sha256(path)
        if (
            expected_pptx_sha256 is not None
            and actual_pptx_sha256 != expected_pptx_sha256
        ):
            raise StalePresentationVersionError(
                "PPTX changed before version record",
            )
        next_generation = (
            generation
            if generation is not None
            else (current.generation + 1 if current else 1)
        )
        if current is not None and next_generation <= current.generation:
            raise StalePresentationVersionError(
                "writer generation is not newer than the authoritative version",
            )
        version = PresentationVersion(
            parent_version_id=supplied_parent,
            pptx_sha256=actual_pptx_sha256,
            deck_ir_version=deck_ir_version(deck),
            writer=writer,
            reconciled=True,
            generation=next_generation,
            base_pptx_sha256=expected_base,
        )
        version = self._snapshot_version(
            version,
            path,
            deck=deck,
            quality_report_path=quality_report_path,
            render_dir=render_dir,
        )
        self._append(version)
        return version

    def bootstrap(
        self,
        pptx_path: str | Path,
        deck: DeckIR,
        *,
        writer: PresentationWriter,
        quality_report_path: str | Path | None = None,
        render_dir: str | Path | None = None,
    ) -> PresentationVersion:
        """Adopt one already-verified legacy artifact exactly once."""

        if self.current() is not None:
            raise StalePresentationVersionError(
                "version ledger already exists",
            )
        return self.record(
            pptx_path,
            deck,
            writer=writer,
            generation=0,
            base_pptx_sha256=None,
            quality_report_path=quality_report_path,
            render_dir=render_dir,
        )

    def register_external_save(
        self,
        pptx_path: str | Path,
        *,
        writer: Literal["genoffice_human", "office_cli"],
        expected_parent_sha256: str | None = None,
        generation: int | None = None,
        base_pptx_sha256: str | None = None,
    ) -> PresentationVersion:
        """Record an external save as authoritative but unreconciled with DeckIR."""

        path = Path(pptx_path).resolve()
        current = self.current()
        if current is None:
            raise StalePresentationVersionError(
                "external saves require an existing owned presentation version",
            )
        if (
            expected_parent_sha256 is not None
            and current.pptx_sha256 != expected_parent_sha256
        ):
            raise StalePresentationVersionError(
                "external editor save was based on a stale presentation version",
            )
        actual = file_sha256(path)
        if actual == current.pptx_sha256:
            raise ValueError("external save did not change the PPTX")
        version = PresentationVersion(
            parent_version_id=current.version_id,
            pptx_sha256=actual,
            deck_ir_version=current.deck_ir_version,
            writer=writer,
            reconciled=False,
            generation=(
                generation
                if generation is not None
                else current.generation + 1
            ),
            base_pptx_sha256=base_pptx_sha256 or current.pptx_sha256,
        )
        if version.base_pptx_sha256 != current.pptx_sha256:
            raise StalePresentationVersionError(
                "external writer lease base no longer matches the authoritative version",
            )
        if version.generation <= current.generation:
            raise StalePresentationVersionError(
                "external writer generation is not newer than the authoritative version",
            )
        version = self._snapshot_version(version, path)
        self._append(version)
        return version

    def rollback_to(
        self,
        version_id: str,
        *,
        pptx_path: str | Path,
        deck_ir_path: str | Path,
        quality_report_path: str | Path,
        render_root: str | Path,
        generation: int,
        base_pptx_sha256: str,
    ) -> tuple[PresentationVersion, DeckIR, QualityReport]:
        """Restore one evidence-bound version and append a rollback version.

        No historical version or candidate is deleted.  The restored PPTX,
        semantic scene, report and preview images all come from the same
        immutable snapshot bundle.
        """

        current = self.current()
        output = Path(pptx_path).resolve()
        if current is None or not output.is_file():
            raise StalePresentationVersionError("no authoritative version to roll back")
        if current.pptx_sha256 != base_pptx_sha256:
            raise StalePresentationVersionError("rollback base is stale")
        if generation <= current.generation:
            raise StalePresentationVersionError(
                "rollback generation is not newer than the authoritative version",
            )
        if file_sha256(output) != current.pptx_sha256:
            raise StalePresentationVersionError(
                "authoritative PPTX changed outside the version chain",
            )
        target = self.get(version_id)
        if target.version_id == current.version_id:
            raise ValueError("target version is already authoritative")
        if not target.reconciled:
            raise StalePresentationVersionError(
                "cannot roll back to an unreconciled external save",
            )
        if not all(
            [
                target.pptx_snapshot,
                target.deck_ir_snapshot,
                target.quality_snapshot,
                target.render_snapshot_dir,
            ],
        ):
            raise StalePresentationVersionError(
                "target version has no complete evidence snapshot",
            )
        source_pptx = self._resolve_snapshot(target.pptx_snapshot)
        source_deck = self._resolve_snapshot(target.deck_ir_snapshot)
        source_quality = self._resolve_snapshot(target.quality_snapshot)
        source_renders = self._resolve_snapshot(target.render_snapshot_dir)
        if file_sha256(source_pptx) != target.pptx_sha256:
            raise StalePresentationVersionError("target PPTX snapshot hash mismatch")
        deck = DeckIR.model_validate_json(source_deck.read_text(encoding="utf-8"))
        report = QualityReport.model_validate_json(
            source_quality.read_text(encoding="utf-8"),
        )
        if not deck_ir_matches_version(deck, target.deck_ir_version):
            raise StalePresentationVersionError("target DeckIR snapshot hash mismatch")
        if report.output_sha256 != target.pptx_sha256:
            raise StalePresentationVersionError(
                "target quality report is not bound to the target PPTX",
            )
        render_set_id = report.render_set_id
        if not render_set_id or Path(render_set_id).name != render_set_id:
            raise StalePresentationVersionError("target render evidence is invalid")
        rendered = slide_image_paths(source_renders)
        if set(rendered) != set(source_renders.glob("*.png")):
            raise StalePresentationVersionError("target render evidence is invalid")
        if not is_verified_delivery(deck, report, source_pptx, rendered):
            raise StalePresentationVersionError(
                "target version does not have complete bound delivery evidence",
            )

        deck_destination = Path(deck_ir_path).resolve()
        quality_destination = Path(quality_report_path).resolve()
        render_destination = Path(render_root).resolve() / render_set_id
        transaction = self.root / f".rollback-{uuid4().hex}"
        transaction.mkdir(parents=True, exist_ok=False)
        shutil.copy2(output, transaction / "presentation.pptx")
        if deck_destination.is_file():
            shutil.copy2(deck_destination, transaction / "deck-ir.json")
        if quality_destination.is_file():
            shutil.copy2(
                quality_destination,
                transaction / "quality-report.json",
            )
        if render_destination.is_dir():
            shutil.copytree(render_destination, transaction / "renders")
        if self.current_path.is_file():
            shutil.copy2(self.current_path, transaction / "current.json")
        if self.history_path.is_file():
            shutil.copy2(self.history_path, transaction / "history.jsonl")
        try:
            self._atomic_copy(source_pptx, output)
            self._atomic_copy(source_deck, deck_destination)
            self._atomic_copy(source_quality, quality_destination)
            self._restore_render_snapshot(source_renders, render_destination)
            restored = self.record(
                output,
                deck,
                writer="version_rollback",
                parent=current,
                generation=generation,
                base_pptx_sha256=base_pptx_sha256,
                expected_pptx_sha256=target.pptx_sha256,
                quality_report_path=quality_destination,
                render_dir=render_destination,
            )
            return restored, deck, report
        except Exception:
            self._atomic_copy(transaction / "presentation.pptx", output)
            self._restore_optional_file(
                transaction / "deck-ir.json",
                deck_destination,
            )
            self._restore_optional_file(
                transaction / "quality-report.json",
                quality_destination,
            )
            self._restore_optional_file(
                transaction / "current.json",
                self.current_path,
            )
            self._restore_optional_file(
                transaction / "history.jsonl",
                self.history_path,
            )
            backup_renders = transaction / "renders"
            if backup_renders.is_dir():
                self._restore_render_snapshot(
                    backup_renders,
                    render_destination,
                )
            elif render_destination.exists():
                shutil.rmtree(render_destination)
            raise
        finally:
            shutil.rmtree(transaction, ignore_errors=True)

    def _snapshot_version(
        self,
        version: PresentationVersion,
        pptx_path: Path,
        *,
        deck: DeckIR | None = None,
        quality_report_path: str | Path | None = None,
        render_dir: str | Path | None = None,
    ) -> PresentationVersion:
        artifacts_root = self.root / "artifacts"
        artifacts_root.mkdir(parents=True, exist_ok=True)
        bundle = artifacts_root / version.version_id
        staging = artifacts_root / f".{version.version_id}.{uuid4().hex}.tmp"
        if bundle.exists():
            raise FileExistsError(bundle)
        staging.mkdir(parents=True, exist_ok=False)
        try:
            staged_pptx = staging / "presentation.pptx"
            shutil.copy2(pptx_path, staged_pptx)
            if file_sha256(staged_pptx) != version.pptx_sha256:
                raise StalePresentationVersionError(
                    "PPTX changed while snapshotting version",
                )
            updates: dict[str, str] = {
                "pptx_snapshot": self._relative_snapshot(
                    bundle / "presentation.pptx",
                ),
            }
            if deck is not None:
                staged_deck = staging / "deck-ir.json"
                staged_deck.write_text(
                    deck.model_dump_json(indent=2),
                    encoding="utf-8",
                )
                updates["deck_ir_snapshot"] = self._relative_snapshot(
                    bundle / "deck-ir.json",
                )
            if quality_report_path is not None:
                quality_source = Path(quality_report_path).resolve()
                if not quality_source.is_file():
                    raise FileNotFoundError(quality_source)
                shutil.copy2(quality_source, staging / "quality-report.json")
                updates["quality_snapshot"] = self._relative_snapshot(
                    bundle / "quality-report.json",
                )
            if render_dir is not None:
                render_source = Path(render_dir).resolve()
                if not render_source.is_dir():
                    raise FileNotFoundError(render_source)
                shutil.copytree(render_source, staging / "renders")
                updates["render_snapshot_dir"] = self._relative_snapshot(
                    bundle / "renders",
                )
            if file_sha256(pptx_path) != version.pptx_sha256:
                raise StalePresentationVersionError(
                    "PPTX changed before version snapshot was committed",
                )
            os.replace(staging, bundle)
            return version.model_copy(update=updates)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _relative_snapshot(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root.parent.resolve()))

    def _resolve_snapshot(self, relative_path: str) -> Path:
        path = (self.root.parent / relative_path).resolve()
        artifacts_root = (self.root / "artifacts").resolve()
        if path != artifacts_root and artifacts_root not in path.parents:
            raise StalePresentationVersionError("version snapshot escaped its job root")
        if not path.exists():
            raise StalePresentationVersionError("version snapshot is missing")
        return path

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)

    @classmethod
    def _restore_optional_file(cls, source: Path, destination: Path) -> None:
        if source.is_file():
            cls._atomic_copy(source, destination)
        else:
            destination.unlink(missing_ok=True)

    @staticmethod
    def _restore_render_snapshot(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
        shutil.copytree(source, temporary)
        stale: Path | None = None
        if destination.exists():
            stale = destination.parent / f".stale-{destination.name}-{uuid4().hex}"
            os.replace(destination, stale)
        try:
            os.replace(temporary, destination)
        except Exception:
            if stale is not None and stale.exists():
                os.replace(stale, destination)
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        if stale is not None:
            shutil.rmtree(stale, ignore_errors=True)

    def _append(self, version: PresentationVersion) -> None:
        checkpoint = self.publication_checkpoint(parent=self.current())
        line = (version.model_dump_json() + "\n").encode("utf-8")
        try:
            # Logical history remains append-only. Replacing the full file
            # atomically prevents a short write from leaving a partial line.
            self._write_ledger_file(
                self.history_path, (checkpoint.history_bytes or b"") + line,
            )
            self._write_ledger_file(
                self.current_path, version.model_dump_json(indent=2).encode("utf-8"),
            )
        except Exception:
            self.rollback_unpublished_record(
                checkpoint, writer=version.writer, generation=version.generation,
                base_pptx_sha256=version.base_pptx_sha256,
                expected_pptx_sha256=version.pptx_sha256,
            )
            raise
