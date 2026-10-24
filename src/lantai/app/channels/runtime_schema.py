# -*- coding: utf-8 -*-
"""Lantai-owned channel request/response schema."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Annotated, Any, Dict, List, Literal, Optional, TypeAlias, Union
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MessageType:
    MESSAGE = "message"
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"
    PLUGIN_CALL = "plugin_call"
    PLUGIN_CALL_OUTPUT = "plugin_call_output"
    COMPONENT_CALL = "component_call"
    COMPONENT_CALL_OUTPUT = "component_call_output"
    MCP_LIST_TOOLS = "mcp_list_tools"
    MCP_APPROVAL_REQUEST = "mcp_approval_request"
    MCP_TOOL_CALL = "mcp_call"
    MCP_APPROVAL_RESPONSE = "mcp_approval_response"
    MCP_TOOL_CALL_OUTPUT = "mcp_call_output"
    REASONING = "reasoning"
    HEARTBEAT = "heartbeat"
    ERROR = "error"
    A2UI_RESPONSE = "a2ui_response"
    A2UI_ACTION = "a2ui_action"

    @classmethod
    def all_values(cls) -> list[str]:
        return [
            value
            for name, value in vars(cls).items()
            if not name.startswith("_") and isinstance(value, str)
        ]


class ContentType:
    TEXT = "text"
    DATA = "data"
    IMAGE = "image"
    AUDIO = "audio"
    FILE = "file"
    REFUSAL = "refusal"
    VIDEO = "video"


class Role:
    ASSISTANT = "assistant"
    USER = "user"
    SYSTEM = "system"
    TOOL = "tool"


class RunStatus:
    Created = "created"
    InProgress = "in_progress"
    Completed = "completed"
    Canceled = "canceled"
    Failed = "failed"
    Rejected = "rejected"
    Unknown = "unknown"
    Queued = "queued"
    Incomplete = "incomplete"


class FunctionParameters(BaseModel):
    type: str
    properties: Dict[str, Any]
    required: Optional[List[str]]


class FunctionTool(BaseModel):
    name: str
    description: str
    parameters: Union[FunctionParameters, Dict[str, Any]]


class Tool(BaseModel):
    type: Optional[str] = "function"
    function: Optional[FunctionTool] = None


class FunctionCall(BaseModel):
    call_id: Optional[str] = None
    name: Optional[str] = None
    arguments: Optional[str] = None


class FunctionCallOutput(BaseModel):
    call_id: str
    name: Optional[str] = None
    output: str


class McpCall(BaseModel):
    call_id: Optional[str] = None
    arguments: Optional[str] = None
    name: Optional[str] = None
    server_label: Optional[str] = None


class McpCallOutput(BaseModel):
    call_id: str
    name: Optional[str] = None
    output: str


class McpListToolsTool(BaseModel):
    input_schema: object
    name: str
    annotations: Optional[object] = None
    description: Optional[str] = None


class McpListTools(BaseModel):
    id: str
    server_label: str
    tools: List[McpListToolsTool]
    error: Optional[str] = None


class McpApprovalRequest(BaseModel):
    id: str
    arguments: str
    name: str
    server_label: str


class McpApprovalResponse(BaseModel):
    approval_request_id: str
    approve: bool
    id: Optional[str] = None
    reason: Optional[str] = None


class Error(BaseModel):
    code: str
    message: str


class Event(BaseModel):
    sequence_number: Optional[int] = None
    object: str
    status: Optional[str] = None
    error: Optional[Error] = None

    def created(self) -> "Event":
        self.status = RunStatus.Created
        return self

    def in_progress(self) -> "Event":
        self.status = RunStatus.InProgress
        return self

    def completed(self) -> "Event":
        self.status = RunStatus.Completed
        return self

    def failed(self, error: Error) -> "Event":
        self.status = RunStatus.Failed
        self.error = error
        return self

    def rejected(self) -> "Event":
        self.status = RunStatus.Rejected
        return self

    def canceled(self) -> "Event":
        self.status = RunStatus.Canceled
        return self


class Content(Event):
    type: str
    object: str = "content"
    index: Optional[int] = None
    delta: Optional[bool] = False
    msg_id: Optional[str] = None

    @staticmethod
    def from_chat_completion_chunk(
        chunk: Any,
        index: Optional[int] = None,
    ) -> Optional[Union["TextContent", "DataContent", "ImageContent"]]:
        choices = getattr(chunk, "choices", None)
        if not choices:
            return None
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            return None
        if getattr(delta, "content", None) is not None:
            return TextContent(delta=True, text=delta.content, index=index)
        tool_calls = getattr(delta, "tool_calls", None)
        if tool_calls:
            tool_call = tool_calls[0]
            function = getattr(tool_call, "function", None)
            if function is not None:
                return DataContent(
                    delta=True,
                    data={
                        "call_id": getattr(tool_call, "id", None),
                        "name": getattr(function, "name", None),
                        "arguments": getattr(function, "arguments", None),
                    },
                    index=index,
                )
        return None


class ImageContent(Content):
    type: Literal[ContentType.IMAGE] = ContentType.IMAGE
    image_url: Optional[str] = None
    filename: Optional[str] = None
    file_name: Optional[str] = None
    file_source_url: Optional[str] = None
    stored_name: Optional[str] = None
    attachment_role: Optional[str] = None
    attachment_role_label: Optional[str] = None
    attachment_schema_version: Optional[int] = None
    attachment_digest: Optional[str] = None
    attachment_version: Optional[int] = None
    attachment_source_id: Optional[str] = None
    attachment_source_chat_id: Optional[str] = None
    attachment_source_command_id: Optional[str] = None
    attachment_grant_ref: Optional[str] = None
    attachment_grant_state: Optional[str] = None
    attachment_upload_receipt: Optional[str] = None
    media_type: Optional[str] = None
    file_size: Optional[int] = None


class TextContent(Content):
    type: Literal[ContentType.TEXT] = ContentType.TEXT
    text: Optional[str] = None


class DataContent(Content):
    type: Literal[ContentType.DATA] = ContentType.DATA
    data: Optional[Dict] = None


class AudioContent(Content):
    type: Literal[ContentType.AUDIO] = ContentType.AUDIO
    data: Optional[str] = None
    format: Optional[str] = None
    filename: Optional[str] = None
    file_name: Optional[str] = None
    file_source_url: Optional[str] = None
    stored_name: Optional[str] = None
    attachment_role: Optional[str] = None
    attachment_role_label: Optional[str] = None
    attachment_schema_version: Optional[int] = None
    attachment_digest: Optional[str] = None
    attachment_version: Optional[int] = None
    attachment_source_id: Optional[str] = None
    attachment_source_chat_id: Optional[str] = None
    attachment_source_command_id: Optional[str] = None
    attachment_grant_ref: Optional[str] = None
    attachment_grant_state: Optional[str] = None
    attachment_upload_receipt: Optional[str] = None
    media_type: Optional[str] = None
    file_size: Optional[int] = None


class VideoContent(Content):
    type: Literal[ContentType.VIDEO] = ContentType.VIDEO
    video_url: Optional[str] = None
    filename: Optional[str] = None
    file_name: Optional[str] = None
    file_source_url: Optional[str] = None
    stored_name: Optional[str] = None
    attachment_role: Optional[str] = None
    attachment_role_label: Optional[str] = None
    attachment_schema_version: Optional[int] = None
    attachment_digest: Optional[str] = None
    attachment_version: Optional[int] = None
    attachment_source_id: Optional[str] = None
    attachment_source_chat_id: Optional[str] = None
    attachment_source_command_id: Optional[str] = None
    attachment_grant_ref: Optional[str] = None
    attachment_grant_state: Optional[str] = None
    attachment_upload_receipt: Optional[str] = None
    media_type: Optional[str] = None
    file_size: Optional[int] = None


class FileContent(Content):
    type: Literal[ContentType.FILE] = ContentType.FILE
    file_url: Optional[str] = None
    file_id: Optional[str] = None
    filename: Optional[str] = None
    file_name: Optional[str] = None
    file_source_url: Optional[str] = None
    stored_name: Optional[str] = None
    attachment_role: Optional[str] = None
    attachment_role_label: Optional[str] = None
    attachment_schema_version: Optional[int] = None
    attachment_digest: Optional[str] = None
    attachment_version: Optional[int] = None
    attachment_source_id: Optional[str] = None
    attachment_source_chat_id: Optional[str] = None
    attachment_source_command_id: Optional[str] = None
    attachment_grant_ref: Optional[str] = None
    attachment_grant_state: Optional[str] = None
    attachment_upload_receipt: Optional[str] = None
    file_data: Optional[str] = None
    media_type: Optional[str] = None
    file_size: Optional[int] = None


class RefusalContent(Content):
    type: Literal[ContentType.REFUSAL] = ContentType.REFUSAL
    refusal: Optional[str] = None


class ToolCall(BaseModel):
    arguments: str
    call_id: str
    name: str


class ToolCallOutput(BaseModel):
    call_id: str
    output: str


AgentRole: TypeAlias = Literal[
    Role.ASSISTANT,
    Role.SYSTEM,
    Role.USER,
    Role.TOOL,
]

AgentContent = Annotated[
    Union[
        TextContent,
        ImageContent,
        DataContent,
        AudioContent,
        FileContent,
        RefusalContent,
        VideoContent,
    ],
    Field(discriminator="type"),
]


_CONTENT_FIELDS = (
    "type",
    "object",
    "sequence_number",
    "status",
    "error",
    "index",
    "delta",
    "msg_id",
    "text",
    "image_url",
    "data",
    "format",
    "video_url",
    "file_url",
    "file_id",
    "filename",
    "file_name",
    "file_source_url",
    "stored_name",
    "attachment_role",
    "attachment_role_label",
    "attachment_schema_version",
    "attachment_digest",
    "attachment_version",
    "attachment_source_id",
    "attachment_source_chat_id",
    "attachment_source_command_id",
    "attachment_grant_ref",
    "attachment_grant_state",
    "attachment_upload_receipt",
    "file_data",
    "media_type",
    "file_size",
    "refusal",
)


def _dump_legacy_model(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return value


def _normalize_content_payload(value: Any) -> Any:
    value = _dump_legacy_model(value)
    if isinstance(value, dict):
        return value
    payload = {
        key: getattr(value, key)
        for key in _CONTENT_FIELDS
        if hasattr(value, key)
    }
    return payload or value


def _normalize_message_payload(value: Any) -> Any:
    value = _dump_legacy_model(value)
    if isinstance(value, dict):
        return value
    return value


class Message(Event):
    id: str = Field(default_factory=lambda: "msg_" + str(uuid4()))
    object: str = "message"
    type: str = "message"
    status: str = RunStatus.Created
    created_at: str | int | float | None = None
    role: Optional[AgentRole] = None
    content: Optional[List[AgentContent]] = None
    code: Optional[str] = None
    message: Optional[str] = None
    usage: Optional[Dict] = None
    metadata: Optional[Dict] = None

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            return [{"type": ContentType.TEXT, "text": value}]
        return [_normalize_content_payload(item) for item in value]

    @staticmethod
    def from_openai_message(message: Union[BaseModel, dict]) -> "Message":
        if isinstance(message, Message):
            return message
        if isinstance(message, BaseModel):
            message = message.model_dump()
        if "type" in message and message["type"] in MessageType.all_values():
            return Message(**message)

        if message["role"] == Role.ASSISTANT and "tool_calls" in message:
            content = [
                DataContent(
                    data=FunctionCall(
                        call_id=tool_call["id"],
                        name=tool_call["function"]["name"],
                        arguments=tool_call["function"]["arguments"],
                    ).model_dump(),
                )
                for tool_call in message["tool_calls"]
            ]
            return Message(type=MessageType.FUNCTION_CALL, content=content)

        if message["role"] == Role.TOOL:
            return Message(
                type=MessageType.FUNCTION_CALL_OUTPUT,
                content=[
                    DataContent(
                        data=FunctionCallOutput(
                            call_id=message["tool_call_id"],
                            output=message["content"],
                        ).model_dump(),
                    ),
                ],
            )

        if isinstance(message["content"], str):
            return Message(
                type=MessageType.MESSAGE,
                role=message["role"],
                content=[TextContent(text=message["content"])],
            )

        content = []
        for item in message["content"]:
            if item["type"] == "image_url":
                content.append(ImageContent(image_url=item["image_url"]["url"]))
            elif item["type"] == "text":
                content.append(TextContent(text=item["text"]))
            else:
                content.append(DataContent(data=item.get("text")))
        return Message(
            type=MessageType.MESSAGE,
            role=message["role"],
            content=content,
        )

    def get_text_content(self) -> Optional[str]:
        if self.content is None:
            return None
        for item in self.content:
            if isinstance(item, TextContent):
                return item.text
        return None

    def get_image_content(self) -> List[str]:
        if self.content is None:
            return []
        return [
            item.image_url
            for item in self.content
            if isinstance(item, ImageContent) and item.image_url
        ]

    def get_audio_content(self) -> List[str]:
        if self.content is None:
            return []
        audios: list[str] = []
        for item in self.content:
            if hasattr(item, "type") and item.type == "input_audio":
                input_audio = getattr(item, "input_audio", None)
                if input_audio is None:
                    continue
                data = getattr(input_audio, "data", None)
                if data:
                    audios.append(data)
                    continue
                base64_data = getattr(input_audio, "base64_data", None)
                if base64_data:
                    format_type = getattr(input_audio, "format", "mp3")
                    audios.append(
                        f"data:{format_type};base64,{base64_data}",
                    )
        return audios

    def add_delta_content(
        self,
        new_content: Union[TextContent, ImageContent, DataContent],
    ):
        self.content = self.content or []
        if new_content.index is None:
            content_index = len(self.content)
            copied = deepcopy(new_content)
            copied.delta = None
            copied.index = content_index
            copied.msg_id = self.id
            self.content.append(copied)
            new_content.index = content_index
            new_content.msg_id = self.id
            new_content.in_progress()
            return new_content

        if new_content.delta is True:
            previous = self.content[new_content.index]
            if previous.type == ContentType.TEXT and isinstance(
                previous,
                TextContent,
            ):
                previous.text = (previous.text or "") + (new_content.text or "")
            if previous.type == ContentType.IMAGE and isinstance(
                previous,
                ImageContent,
            ):
                previous.image_url = (previous.image_url or "") + (
                    new_content.image_url or ""
                )
            if previous.type == ContentType.DATA and isinstance(
                previous,
                DataContent,
            ):
                for key, value in (new_content.data or {}).items():
                    old_value = (previous.data or {}).get(key)
                    if isinstance(old_value, list) and isinstance(value, list):
                        old_value.extend(value)
                    elif isinstance(old_value, str) and isinstance(value, str):
                        previous.data[key] = old_value + value
            new_content.msg_id = self.id
            new_content.in_progress()
            return new_content
        return None

    def content_completed(self, content_index: int):
        if self.content is None or content_index >= len(self.content):
            return None
        content = deepcopy(self.content[content_index])
        content.delta = False
        content.index = content_index
        content.msg_id = self.id
        content.completed()
        return content

    def add_content(
        self,
        new_content: Union[TextContent, ImageContent, DataContent],
    ):
        self.content = self.content or []
        if new_content.index is None:
            self.content.append(deepcopy(new_content))
            new_content.index = len(self.content) - 1
            new_content.msg_id = self.id
            new_content.completed()
            return new_content
        return None


class BaseRequest(BaseModel):
    input: List[Message]
    stream: bool = True
    id: Optional[str] = None

    @field_validator("input", mode="before")
    @classmethod
    def normalize_input(cls, value):
        if value is None:
            return value
        if isinstance(value, str):
            return [
                {
                    "role": Role.USER,
                    "content": [
                        {"type": ContentType.TEXT, "text": value},
                    ],
                },
            ]
        if isinstance(value, dict):
            return [_normalize_message_payload(value)]
        return [_normalize_message_payload(item) for item in value]


class AgentRequest(BaseRequest):
    model_config = ConfigDict(extra="allow")

    model: Optional[str] = None
    top_p: Optional[float] = None
    temperature: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[Union[Optional[str], List[str]]] = None
    n: Optional[int] = Field(default=1, ge=1, le=5)
    seed: Optional[int] = None
    tools: Optional[List[Union[Tool, Dict]]] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None


class BaseResponse(Event):
    id: Optional[str] = Field(default_factory=lambda: "response_" + str(uuid4()))
    object: str = "response"
    status: str = RunStatus.Created
    created_at: int = Field(default_factory=lambda: int(datetime.now().timestamp()))
    completed_at: Optional[int] = None
    output: Optional[List[Message]] = None
    usage: Optional[Dict] = None

    @field_validator("output", mode="before")
    @classmethod
    def normalize_output(cls, value):
        if value is None:
            return value
        return [_normalize_message_payload(item) for item in value]

    @field_validator("id", mode="before")
    @classmethod
    def validate_id(cls, value):
        if value is None:
            return "response_" + str(uuid4())
        return value

    def add_new_message(self, message: Message):
        self.output = self.output or []
        self.output.append(message)

    def completed(self) -> "BaseResponse":
        self.completed_at = int(datetime.now().timestamp())
        if self.output is None:
            self.output = []
        super().completed()
        return self


class AgentResponse(BaseResponse):
    session_id: Optional[str] = None
    metadata: Optional[Dict] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class SequenceNumberGenerator:
    def __init__(self, start: int = 0):
        self._current = start

    def next(self) -> int:
        current = self._current
        self._current += 1
        return current

    def yield_with_sequence(self, event: Event) -> Event:
        event.sequence_number = self.next()
        return event


def convert_to_openai_tool_call(function: Union[FunctionCall, Dict[str, Any]]):
    function = _dump_legacy_model(function)
    if not isinstance(function, dict):
        function = {
            key: getattr(function, key)
            for key in ("call_id", "name", "arguments")
            if hasattr(function, key)
        }
    return {
        "id": function.get("call_id", None),
        "type": "function",
        "function": {
            "name": function.get("name", None),
            "arguments": function.get("arguments", None),
        },
    }


def convert_to_openai_messages(messages: List[Message]) -> List[Dict]:
    converted = []
    for msg in messages:
        if MessageType.MESSAGE == msg.type:
            converted.append(
                {
                    "role": msg.role,
                    "content": [
                        item.model_dump()
                        for item in (msg.content or [])
                    ],
                },
            )

        if MessageType.FUNCTION_CALL == msg.type:
            converted.append(
                {
                    "role": Role.ASSISTANT,
                    "tool_calls": [
                        convert_to_openai_tool_call(item.data)
                        for item in (msg.content or [])
                    ],
                },
            )

        if MessageType.FUNCTION_CALL_OUTPUT == msg.type:
            for item in msg.content or []:
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": item.data.get("call_id"),
                        "content": item.data.get("output"),
                    },
                )
    return converted


def convert_to_openai_tools(tools: List[Union[Tool, Dict]]) -> Optional[list]:
    if not tools:
        return None
    return [
        tool.model_dump() if isinstance(tool, Tool) else tool
        for tool in tools
    ]
