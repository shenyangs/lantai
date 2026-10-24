# -*- coding: utf-8 -*-
"""Contracts shared by the Lantai Coherence Harness."""

from .contracts import (
    AuthorityGrant,
    AuthorityProvenance,
    ExecutionFence,
    PermissionDecision,
    PermissionEnvelope,
    derive_authority,
    merge_permission_decisions,
)
from .surface import (
    ContextBudgetSnapshot,
    ModelRequestManifest,
    ModelRequestManifestStore,
    ProgressWitness,
    SourceReference,
    SourceReferenceStore,
    SurfaceCompactionRecord,
    build_model_request_manifest,
)
from .tool_projection import (
    CanonicalToolEnvelope,
    CanonicalToolValue,
    ModelToolProjection,
    ToolUIIntent,
    build_canonical_tool_envelope,
    parse_or_build_tool_envelope,
)
from .health import (
    HarnessHealthFinding,
    HarnessHealthService,
    HarnessHealthSnapshot,
    HarnessRecoveryAction,
    HarnessRecoveryResult,
)
from .world import (
    ContextViewCompiler,
    ContextViewManifest,
    ContextViewRequest,
    WorldLedgerCompiler,
    WorldLedgerSnapshot,
)
from .replay import (
    ProjectionEventDecision,
    ProjectionReplayReport,
    govern_projection_replay,
)

__all__ = [
    "AuthorityGrant",
    "AuthorityProvenance",
    "ExecutionFence",
    "PermissionDecision",
    "PermissionEnvelope",
    "ContextBudgetSnapshot",
    "ModelRequestManifest",
    "ModelRequestManifestStore",
    "ProgressWitness",
    "SourceReference",
    "SourceReferenceStore",
    "SurfaceCompactionRecord",
    "build_model_request_manifest",
    "CanonicalToolEnvelope",
    "CanonicalToolValue",
    "ModelToolProjection",
    "ToolUIIntent",
    "build_canonical_tool_envelope",
    "parse_or_build_tool_envelope",
    "HarnessHealthFinding",
    "HarnessHealthService",
    "HarnessHealthSnapshot",
    "HarnessRecoveryAction",
    "HarnessRecoveryResult",
    "ContextViewCompiler",
    "ContextViewManifest",
    "ContextViewRequest",
    "WorldLedgerCompiler",
    "WorldLedgerSnapshot",
    "ProjectionEventDecision",
    "ProjectionReplayReport",
    "govern_projection_replay",
    "derive_authority",
    "merge_permission_decisions",
]
