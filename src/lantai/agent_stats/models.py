# -*- coding: utf-8 -*-
"""Agent statistics models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChannelStats(BaseModel):
    channel: str
    session_count: int
    user_messages: int
    assistant_messages: int
    total_messages: int


class DailyStats(BaseModel):
    date: str
    chats: int
    active_sessions: int
    user_messages: int
    assistant_messages: int
    total_messages: int
    prompt_tokens: int
    completion_tokens: int
    llm_calls: int
    tool_calls: int


class ProviderUsageStats(BaseModel):
    provider_id: str
    model_count: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    call_count: int


class ModelUsageStats(BaseModel):
    provider_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    call_count: int


class ToolUsageStats(BaseModel):
    name: str
    total_calls: int
    completed_calls: int
    failed_calls: int
    interrupted_calls: int
    average_duration_ms: int
    last_used_at: str | None = None
    last_error: str | None = None


class ToolHealthSummary(BaseModel):
    total_calls: int = 0
    completed_calls: int = 0
    failed_calls: int = 0
    interrupted_calls: int = 0
    success_rate: float = 0
    average_duration_ms: int = 0
    by_tool: list[ToolUsageStats] = Field(default_factory=list)


class StorageCategoryStats(BaseModel):
    key: str
    label: str
    bytes: int
    file_count: int
    directory_count: int
    cleanable: bool = False


class StorageSummary(BaseModel):
    root_path: str
    workspace_path: str
    used_bytes: int = 0
    disk_total_bytes: int = 0
    disk_free_bytes: int = 0
    file_count: int = 0
    directory_count: int = 0
    scanned_at: str
    truncated: bool = False
    categories: list[StorageCategoryStats] = Field(default_factory=list)


class AgentStatsSummary(BaseModel):
    total_active_sessions: int
    total_messages: int
    total_user_messages: int
    total_assistant_messages: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_llm_calls: int
    total_tool_calls: int
    total_cache_tokens: int = 0
    cache_reuse_rate: float = 0
    provider_stats: list[ProviderUsageStats] = Field(default_factory=list)
    model_stats: list[ModelUsageStats] = Field(default_factory=list)
    tool_health: ToolHealthSummary = Field(default_factory=ToolHealthSummary)
    storage: StorageSummary | None = None
    by_date: list[DailyStats]
    channel_stats: list[ChannelStats]
    start_date: str
    end_date: str
