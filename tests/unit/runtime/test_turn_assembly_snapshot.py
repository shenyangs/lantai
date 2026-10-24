# -*- coding: utf-8 -*-
"""Behavior locks for the private immutable turn-assembly contract."""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError

import pytest

from lantai.runtime.turn_assembly import (
    REQUIRED_TURN_ASSEMBLY_SURFACES,
    TURN_ASSEMBLY_SNAPSHOT_SCHEMA_V1,
    FrozenJsonObject,
    TurnAssemblySurface,
    build_turn_assembly_snapshot,
    thaw_turn_assembly_value,
)


def _surface_values(secret: str = "private-prompt-body") -> dict[str, object]:
    return {
        "prompt": {
            "compiled_system_prompt": secret,
            "manifest": [
                {"section_id": "constitution", "content": "private canon"},
                {"section_id": "turn", "content": "private turn prompt"},
            ],
        },
        "persona": {
            "turn_plan": {"personalization_consequence": "private consequence"},
            "subject_continuity": {"thought": "private unfinished thought"},
            "ququ": {"required": True, "prompt": "private ququ prompt"},
        },
        "memory_context": {
            "messages": ["private user message", "private assistant answer"],
            "recall": [{"id": "memory-secret-id", "text": "private memory"}],
        },
        "skills": {
            "active": ["private-skill"],
            "catalog": [{"id": "skill-secret-id", "prompt": "private skill body"}],
        },
        "attachments": {
            "manifest": [{"path": "/Users/private/secret.pdf"}],
            "workspace_grants": ["/Users/private"],
        },
        "tools": {
            "schemas": [{"name": "private_tool", "description": "private"}],
        },
        "request_policy": {
            "metadata": {"user_text": "private request"},
            "permission": "full",
            "policy": {"secret": "private policy"},
        },
        "compatibility": {
            "state_reader": "legacy-reader-private",
            "state_writer": "legacy-writer-private",
            "formatter": {"private": "formatter state"},
            "compaction": {"private": "compaction state"},
        },
    }


def _snapshot(
    *,
    secret: str = "private-prompt-body",
    prompt_item_ids: tuple[str, ...] = ("constitution", "turn"),
):
    values = _surface_values(secret)
    surfaces = []
    for name in REQUIRED_TURN_ASSEMBLY_SURFACES:
        item_ids = prompt_item_ids if name == "prompt" else ()
        surfaces.append(
            TurnAssemblySurface(
                name=name,
                value=values[name],
                source_ids=(f"private-source-{name}",),
                item_ids=item_ids,
            ),
        )
    return build_turn_assembly_snapshot(
        snapshot_id="private-snapshot-id",
        turn_id="private-turn-id",
        surfaces=surfaces,
    )


def test_snapshot_deep_freezes_and_detaches_every_nested_value() -> None:
    values = _surface_values()
    prompt = values["prompt"]
    assert isinstance(prompt, dict)
    manifest = prompt["manifest"]
    assert isinstance(manifest, list)
    surfaces = [
        TurnAssemblySurface(
            name=name,
            value=values[name],
            source_ids=(f"source-{name}",),
        )
        for name in REQUIRED_TURN_ASSEMBLY_SURFACES
    ]
    snapshot = build_turn_assembly_snapshot(
        snapshot_id="snapshot-1",
        turn_id="turn-1",
        surfaces=surfaces,
    )
    before = snapshot.assembly_sha256

    manifest.append({"section_id": "late", "content": "must not leak"})
    prompt["compiled_system_prompt"] = "changed after capture"
    values["persona"] = {"replaced": True}

    frozen_prompt = snapshot.surface("prompt").value
    assert isinstance(frozen_prompt, FrozenJsonObject)
    assert frozen_prompt["compiled_system_prompt"] == "private-prompt-body"
    assert len(frozen_prompt["manifest"]) == 2
    assert snapshot.assembly_sha256 == before

    with pytest.raises(FrozenInstanceError):
        snapshot.turn_id = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.surface("prompt").value = {}  # type: ignore[misc]
    with pytest.raises(TypeError):
        frozen_prompt["compiled_system_prompt"] = "other"  # type: ignore[index]

    directly_constructed = FrozenJsonObject(
        (("nested", [{"secret": "must also freeze"}]),),
    )
    assert isinstance(directly_constructed["nested"], tuple)
    with pytest.raises(TypeError):
        directly_constructed["nested"][0]["secret"] = "other"


def test_thaw_returns_detached_mutable_copy_without_mutating_snapshot() -> None:
    snapshot = _snapshot()
    before = snapshot.assembly_sha256

    mutable = thaw_turn_assembly_value(snapshot.surface("persona").value)
    mutable["turn_plan"]["personalization_consequence"] = "changed"

    assert snapshot.assembly_sha256 == before
    assert (
        snapshot.surface("persona").value["turn_plan"]
        ["personalization_consequence"]
        == "private consequence"
    )


def test_privacy_evidence_contains_only_fixed_names_hashes_and_counts() -> None:
    canaries = (
        "TOP-SECRET-PROMPT",
        "private-snapshot-id",
        "private-turn-id",
        "private-source-prompt",
        "/Users/private/secret.pdf",
        "memory-secret-id",
    )
    evidence_text = json.dumps(
        _snapshot(secret=canaries[0]).privacy_evidence(),
        ensure_ascii=False,
        sort_keys=True,
    )

    assert TURN_ASSEMBLY_SNAPSHOT_SCHEMA_V1 in evidence_text
    assert set(REQUIRED_TURN_ASSEMBLY_SURFACES).issubset(
        {row["surface"] for row in json.loads(evidence_text)["surfaces"]},
    )
    assert all(canary not in evidence_text for canary in canaries)
    assert "compiled_system_prompt" not in evidence_text
    assert "personalization_consequence" not in evidence_text


def test_default_representations_cannot_log_private_snapshot_content() -> None:
    snapshot = _snapshot(secret="TOP-SECRET-PROMPT")
    representations = (
        repr(snapshot),
        repr(snapshot.surface("prompt")),
        repr(snapshot.surface("prompt").value),
    )
    canaries = (
        "TOP-SECRET-PROMPT",
        "private-snapshot-id",
        "private-turn-id",
        "private-source-prompt",
        "/Users/private/secret.pdf",
    )

    assert all(
        canary not in representation
        for representation in representations
        for canary in canaries
    )


def test_content_and_item_order_are_both_hash_significant() -> None:
    first = _snapshot(prompt_item_ids=("constitution", "turn"))
    reordered_ids = _snapshot(prompt_item_ids=("turn", "constitution"))
    values = _surface_values()
    prompt = values["prompt"]
    assert isinstance(prompt, dict)
    prompt["manifest"] = list(reversed(prompt["manifest"]))  # type: ignore[arg-type]
    reordered_content = build_turn_assembly_snapshot(
        snapshot_id="private-snapshot-id",
        turn_id="private-turn-id",
        surfaces=[
            TurnAssemblySurface(
                name=name,
                value=values[name],
                source_ids=(f"private-source-{name}",),
                item_ids=("constitution", "turn") if name == "prompt" else (),
            )
            for name in REQUIRED_TURN_ASSEMBLY_SURFACES
        ],
    )

    assert first.surface("prompt").content_sha256 == (
        reordered_ids.surface("prompt").content_sha256
    )
    assert first.surface("prompt").order_sha256 != (
        reordered_ids.surface("prompt").order_sha256
    )
    assert first.assembly_sha256 != reordered_ids.assembly_sha256
    assert first.surface("prompt").content_sha256 != (
        reordered_content.surface("prompt").content_sha256
    )


def test_schema_requires_every_protected_surface_in_canonical_order() -> None:
    values = _surface_values()
    reversed_names = tuple(reversed(REQUIRED_TURN_ASSEMBLY_SURFACES))

    with pytest.raises(ValueError, match="canonical order"):
        build_turn_assembly_snapshot(
            snapshot_id="snapshot-1",
            turn_id="turn-1",
            surfaces=[
                TurnAssemblySurface(name=name, value=values[name])
                for name in reversed_names
            ],
        )

    with pytest.raises(ValueError, match="canonical order"):
        build_turn_assembly_snapshot(
            snapshot_id="snapshot-1",
            turn_id="turn-1",
            surfaces=[
                TurnAssemblySurface(name=name, value=values[name])
                for name in REQUIRED_TURN_ASSEMBLY_SURFACES[:-1]
            ],
        )

    with pytest.raises(TypeError, match="TurnAssemblySurface"):
        build_turn_assembly_snapshot(
            snapshot_id="snapshot-1",
            turn_id="turn-1",
            surfaces=[object()],  # type: ignore[list-item]
        )


def test_snapshot_construction_rejects_side_effectful_or_unstable_values() -> None:
    called = False

    def producer() -> str:
        nonlocal called
        called = True
        return "must never run"

    with pytest.raises(TypeError, match="inert built-in JSON data"):
        TurnAssemblySurface(name="prompt", value={"producer": producer})
    assert called is False

    with pytest.raises(TypeError, match="unordered sets"):
        TurnAssemblySurface(name="prompt", value={"ids": {"a", "b"}})
    with pytest.raises(ValueError, match="finite"):
        TurnAssemblySurface(name="prompt", value={"score": float("nan")})


def test_snapshot_rejects_custom_objects_without_invoking_their_callbacks() -> None:
    calls: list[str] = []

    class LazyMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            calls.append("getitem")
            return "private"

        def __iter__(self) -> Iterator[str]:
            calls.append("iter")
            return iter(("private",))

        def __len__(self) -> int:
            calls.append("len")
            return 1

        def __call__(self) -> object:
            calls.append("call")
            return {}

    class CallbackDict(dict[str, object]):
        def items(self):  # type: ignore[override]
            calls.append("dict-items")
            return super().items()

    class CallbackList(list[object]):
        def __iter__(self):
            calls.append("list-iter")
            return super().__iter__()

    class CallbackString(str):
        def __str__(self) -> str:
            calls.append("str")
            return super().__str__()

    rejected_values = (
        LazyMapping(),
        CallbackDict(private="value"),
        CallbackList(["private"]),
        CallbackString("private"),
    )
    for value in rejected_values:
        with pytest.raises(TypeError):
            TurnAssemblySurface(name="prompt", value=value)

    with pytest.raises(TypeError):
        TurnAssemblySurface(name=CallbackString("prompt"), value={})
    with pytest.raises(TypeError):
        TurnAssemblySurface(
            name="prompt",
            value={},
            source_ids=(CallbackString("private-source"),),
        )
    with pytest.raises(TypeError):
        build_turn_assembly_snapshot(
            snapshot_id=CallbackString("snapshot"),
            turn_id="turn",
            surfaces=[],
        )

    assert calls == []


def test_invalid_names_keys_and_schema_never_echo_private_values() -> None:
    private_name = "private_patient_identifier"
    with pytest.raises(ValueError) as surface_error:
        TurnAssemblySurface(name=private_name, value={})
    assert private_name not in str(surface_error.value)

    private_key = "private-medical-record-key"
    with pytest.raises(ValueError) as key_error:
        FrozenJsonObject(((private_key, 1), (private_key, 2)))
    assert private_key not in str(key_error.value)

    values = _surface_values()
    surfaces = tuple(
        TurnAssemblySurface(name=name, value=values[name])
        for name in REQUIRED_TURN_ASSEMBLY_SURFACES
    )
    private_schema = "private-schema-name"
    with pytest.raises(ValueError) as schema_error:
        from lantai.runtime.turn_assembly import TurnAssemblySnapshot

        TurnAssemblySnapshot(
            snapshot_id="snapshot-1",
            turn_id="turn-1",
            surfaces=surfaces,
            schema_version=private_schema,
        )
    assert private_schema not in str(schema_error.value)

    allowed_surface = TurnAssemblySurface(
        name="prompt",
        value={"private": "private-prompt-body"},
        source_ids=("private-source-id",),
    )
    assert "private-prompt-body" not in repr(allowed_surface)
    assert "private-source-id" not in repr(allowed_surface)
    assert "private-prompt-body" not in json.dumps(
        allowed_surface.privacy_evidence(),
    )


def test_schema_version_is_explicit_and_rejects_unknown_versions() -> None:
    values = _surface_values()
    surfaces = tuple(
        TurnAssemblySurface(name=name, value=values[name])
        for name in REQUIRED_TURN_ASSEMBLY_SURFACES
    )

    with pytest.raises(ValueError, match="Unsupported turn assembly schema"):
        # Construct directly because the public builder always emits v1.
        from lantai.runtime.turn_assembly import TurnAssemblySnapshot

        TurnAssemblySnapshot(
            snapshot_id="snapshot-1",
            turn_id="turn-1",
            surfaces=surfaces,
            schema_version="com.lantai.runtime.turn-assembly-snapshot.v2",
        )


def test_contract_module_has_only_stdlib_imports_and_no_runtime_owner_imports() -> None:
    import lantai.runtime.turn_assembly as contract_module

    tree = ast.parse(inspect.getsource(contract_module))
    imported_roots = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module != "__future__"
    )

    assert imported_roots == {
        "collections",
        "dataclasses",
        "hashlib",
        "json",
        "math",
        "typing",
    }
