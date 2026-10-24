# -*- coding: utf-8 -*-
"""Tool registry contracts owned by Lantai Runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .effects import RuntimeEffectRecoveryCapability

ToolOrigin = Literal["builtin", "coding", "plugin", "memory", "mcp", "skill"]
ToolPolicyAction = Literal["allow", "deny", "needs_approval"]
ToolPolicyDecision = ToolPolicyAction
ToolExecutor = Callable[..., Any]
RUNTIME_BLOCKED_TOOL_NAMES_CONTEXT_KEY = "runtime_blocked_tool_names"


class ToolOutputSemantics(BaseModel):
    """Versioned, machine-readable meaning of a successful tool output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    artifact_role: Literal["none", "candidate", "primary", "delivery"] = "none"
    argument_path_fields: tuple[str, ...] = ()
    result_path_fields: tuple[str, ...] = ()
    content_media_types: tuple[Literal["file", "image", "audio", "video"], ...] = ()
    source_adapter: Literal["declared", "legacy_tool_adapter"] = "declared"
    legacy_workspace_diff: bool = False


def runtime_blocked_tool_names(
    request_context: dict[str, Any] | None,
) -> frozenset[str]:
    """Return normalized tool names blocked for this runtime turn."""

    raw = (request_context or {}).get(
        RUNTIME_BLOCKED_TOOL_NAMES_CONTEXT_KEY,
        (),
    )
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(
        name
        for item in raw
        if (name := str(item or "").strip())
    )


class ToolSpec(BaseModel):
    """Stable metadata for a tool exposed to Lantai Runtime."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    origin: ToolOrigin = "builtin"
    enabled: bool = True
    async_execution: bool = False
    # Conservative by default: dynamic/plugin/MCP tools must opt in explicitly.
    read_only: bool = False
    concurrency_safe: bool = False
    destructive: bool = False
    max_result_size: int | None = Field(default=None, ge=1)
    effect_recovery: RuntimeEffectRecoveryCapability = Field(
        default_factory=RuntimeEffectRecoveryCapability,
    )
    output_semantics: ToolOutputSemantics = Field(
        default_factory=ToolOutputSemantics,
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fail_closed_effect_recovery(self) -> "ToolSpec":
        """Treat every undeclared mutating tool as an ambiguous side effect."""

        if (
            not self.read_only
            and self.effect_recovery == RuntimeEffectRecoveryCapability()
        ):
            self.effect_recovery = RuntimeEffectRecoveryCapability(
                external_side_effect=True,
                idempotency="not_idempotent",
                probe="none",
                compensation="none",
                on_ambiguity="manual_disambiguation",
                notes=(
                    "Fail-closed default for a tool that is not explicitly "
                    "declared read-only."
                ),
            )
        return self


class ToolCall(BaseModel):
    """Normalized request to execute a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Normalized result returned by a tool executor."""

    call_id: str
    name: str
    ok: bool = True
    content: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolPolicyContext(BaseModel):
    """Runtime context used when evaluating a tool call policy."""

    agent_id: str | None = None
    session_id: str | None = None
    root_session_id: str | None = None
    chat_id: str | None = None
    turn_id: str | None = None
    workspace_dir: str | None = None
    workspace_grant_id: str | None = None
    workspace_access_mode: Literal["read_only", "read_write"] = "read_write"
    workspace_trust_state: Literal["trusted", "restricted"] = "restricted"
    permission_profile: str = "workspace"
    permission_mode: str | None = None
    approval_level: str = "AUTO"
    distribution: str | None = None
    request_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolPolicyOutcome(BaseModel):
    """Normalized policy result consumed by Native Runtime."""

    action: ToolPolicyAction = "allow"
    reason: str = ""
    risk: str = "low"
    requires_approval: bool = False
    approval_copy: str | None = None
    remembered_approval: dict[str, Any] | None = None
    policy_payload: dict[str, Any] | None = None
    guard_payload: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillSpec(BaseModel):
    """Workspace skill registration captured without toolkit coupling."""

    name: str
    path: str
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class McpClientSpec(BaseModel):
    """MCP client registration captured for adapter-specific projection."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    client: Any
    execution_timeout: float | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolPolicyMiddleware(Protocol):
    """Policy hook used by Native Runtime before tool execution."""

    async def evaluate(
        self,
        tool_call: ToolCall,
        spec: ToolSpec,
        context: ToolPolicyContext,
    ) -> ToolPolicyOutcome:
        """Return whether the call is allowed, denied, or requires approval."""
        ...


class LantaiToolRegistry:
    """Runtime-owned registry for tools, skills, and MCP clients."""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolExecutor]] = {}
        self._skills: dict[str, SkillSpec] = {}
        self._mcp_clients: dict[str, McpClientSpec] = {}
        self._policy_middlewares: list[ToolPolicyMiddleware] = []
        self._blocked_tool_names: set[str] = set()

    def block_tools(self, names: Any) -> None:
        """Hide and reject named tools for this registry instance."""

        if isinstance(names, str):
            names = (names,)
        normalized = {
            name
            for item in names or ()
            if (name := str(item or "").strip())
        }
        self._blocked_tool_names.update(normalized)
        for name in normalized:
            self._tools.pop(name, None)

    def blocked_tool_names(self) -> frozenset[str]:
        """Return tool names blocked for presentation and execution."""

        return frozenset(self._blocked_tool_names)

    def register_tool(
        self,
        spec: ToolSpec,
        executor: ToolExecutor,
    ) -> None:
        """Register a tool executor under its runtime spec."""

        if spec.name in self._blocked_tool_names:
            return
        self._tools[spec.name] = (spec, executor)

    def register_skill(self, spec: SkillSpec) -> None:
        """Register a workspace skill directory."""

        self._skills[spec.name] = spec

    def register_mcp_client(self, spec: McpClientSpec) -> None:
        """Register an MCP client for adapter projection."""

        self._mcp_clients[spec.name] = spec

    def register_policy_middleware(
        self,
        middleware: ToolPolicyMiddleware,
    ) -> None:
        """Register a policy middleware for runtime tool execution."""

        self._policy_middlewares.append(middleware)

    def tool_items(self) -> list[tuple[ToolSpec, ToolExecutor]]:
        """Return enabled tool specs and executors in registration order."""

        return [
            (spec, executor)
            for spec, executor in self._tools.values()
            if spec.enabled
        ]

    def skill_specs(self) -> list[SkillSpec]:
        """Return enabled skill specs in registration order."""

        return [spec for spec in self._skills.values() if spec.enabled]

    def mcp_client_specs(self) -> list[McpClientSpec]:
        """Return enabled MCP client specs in registration order."""

        return [spec for spec in self._mcp_clients.values() if spec.enabled]

    def policy_middlewares(self) -> list[ToolPolicyMiddleware]:
        """Return registered policy middlewares in registration order."""

        return list(self._policy_middlewares)

    def has_async_tools(self) -> bool:
        """True when any enabled tool requests async task execution."""

        return any(spec.async_execution for spec, _executor in self.tool_items())

    def get_tool(self, name: str) -> tuple[ToolSpec, ToolExecutor] | None:
        """Return one registered tool by name."""

        if name in self._blocked_tool_names:
            return None
        return self._tools.get(name)

    def extend(self, other: "LantaiToolRegistry") -> None:
        """Merge another registry into this one preserving registration order."""

        self.block_tools(other.blocked_tool_names())
        for spec, executor in other.tool_items():
            self.register_tool(spec, executor)
        for spec in other.skill_specs():
            self.register_skill(spec)
        for spec in other.mcp_client_specs():
            self.register_mcp_client(spec)
        for middleware in other.policy_middlewares():
            self.register_policy_middleware(middleware)


def tool_effect_recovery_capability(
    spec: ToolSpec,
) -> RuntimeEffectRecoveryCapability:
    """Return the declared recovery capability for a tool spec."""

    return spec.effect_recovery
