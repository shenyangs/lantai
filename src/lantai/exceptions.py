# -*- coding: utf-8 -*-
"""Lantai exception definitions and converters."""

from typing import Any, Dict, Optional


class AppBaseException(Exception):
    """Base exception carrying the API error contract."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        return f"[{self.status}] {self.code}: {self.message}"

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(status={self.status}, code="
            f"'{self.code}', message='{self.message}')"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class BadRequestException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(400, code, message, details)


class UnauthorizedException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(401, code, message, details)


class ForbiddenException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(403, code, message, details)


class NotFoundException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(404, code, message, details)


class MethodNotAllowedException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(405, code, message, details)


class ConflictException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(409, code, message, details)


class UnprocessableEntityException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(422, code, message, details)


class TooManyRequestsException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(429, code, message, details)


class InternalServerErrorException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(500, code, message, details)


class BadGatewayException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(502, code, message, details)


class ServiceUnavailableException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(503, code, message, details)


class GatewayTimeoutException(AppBaseException):
    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(504, code, message, details)


class AuthenticationException(UnauthorizedException):
    def __init__(
        self,
        message: str = "Authentication failed",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("AUTH_FAILED", message, details)


class TokenExpiredException(UnauthorizedException):
    def __init__(
        self,
        message: str = "Token has expired",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("TOKEN_EXPIRED", message, details)


class InvalidTokenException(UnauthorizedException):
    def __init__(
        self,
        message: str = "Invalid token",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("INVALID_TOKEN", message, details)


class PermissionDeniedException(ForbiddenException):
    def __init__(
        self,
        message: str = "Permission denied",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("PERMISSION_DENIED", message, details)


class AccessDeniedException(ForbiddenException):
    def __init__(
        self,
        message: str = "Access denied",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("ACCESS_DENIED", message, details)


class ResourceNotFoundException(NotFoundException):
    def __init__(
        self,
        resource_type: str,
        resource_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "RESOURCE_NOT_FOUND",
            f"{resource_type} not found: {resource_id}",
            details,
        )


class UserNotFoundException(NotFoundException):
    def __init__(
        self,
        user_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("USER_NOT_FOUND", f"User not found: {user_id}", details)


class TaskNotFoundException(NotFoundException):
    def __init__(
        self,
        task_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("TASK_NOT_FOUND", f"Task not found: {task_id}", details)


class InvalidParameterException(BadRequestException):
    def __init__(
        self,
        parameter: str,
        message: str | None = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "INVALID_PARAMETER",
            message or f"Invalid parameter: {parameter}",
            details,
        )


class MissingParameterException(BadRequestException):
    def __init__(
        self,
        parameter: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "MISSING_PARAMETER",
            f"Missing required parameter: {parameter}",
            details,
        )


class ParameterValidationException(BadRequestException):
    def __init__(
        self,
        parameter: str,
        validation_error: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "PARAMETER_VALIDATION",
            f"Parameter validation failed: {parameter} - {validation_error}",
            details,
        )


class RateLimitExceededException(TooManyRequestsException):
    def __init__(
        self,
        operation: str,
        retry_after: int = 60,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "RATE_LIMIT_EXCEEDED",
            (
                f"Operation {operation} is too frequent, please retry "
                f"after {retry_after} seconds"
            ),
            details,
        )


class BusinessLogicException(UnprocessableEntityException):
    """Base class for business-logic errors."""


class WorkflowException(BusinessLogicException):
    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("WORKFLOW_ERROR", message, details)


class AgentException(BusinessLogicException):
    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("AGENT_ERROR", message, details)


class ResponseException(BusinessLogicException):
    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("RESPONSE_ERROR", message, details)


class SystemException(InternalServerErrorException):
    """Base class for system errors."""


class DatabaseException(SystemException):
    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("DATABASE_ERROR", message, details)


class RedisException(SystemException):
    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("REDIS_ERROR", message, details)


class ExternalServiceException(SystemException):
    def __init__(
        self,
        service_name: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "EXTERNAL_SERVICE_ERROR",
            f"External service {service_name} error: {message}",
            details,
        )


class ConfigurationException(InternalServerErrorException):
    def __init__(
        self,
        config_key: str,
        message: str | None = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "CONFIGURATION_ERROR",
            message or f"Configuration error: {config_key}",
            details,
        )


class NetworkException(ServiceUnavailableException):
    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("NETWORK_ERROR", message, details)


class TimeoutException(GatewayTimeoutException):
    def __init__(
        self,
        operation: str,
        timeout: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "TIMEOUT",
            (
                f"Operation {operation} timed out, timeout limit:"
                f" {timeout} seconds"
            ),
            details,
        )


class AgentRuntimeErrorException(BusinessLogicException):
    """Base class for Lantai runtime errors."""


class ToolExecutionException(AgentRuntimeErrorException):
    def __init__(self, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            "TOOL_EXECUTION_FAILED",
            "Error occurred during tool execution",
            details,
        )


class ToolNotFoundException(AgentRuntimeErrorException):
    def __init__(
        self,
        tool_name: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("TOOL_NOT_FOUND", f"Tool not found: {tool_name}", details)


class MCPConnectionException(AgentRuntimeErrorException):
    def __init__(self, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            "MCP_CONNECTION_FAILED",
            "Failed to connect to MCP service",
            details,
        )


class MCPProtocolException(AgentRuntimeErrorException):
    def __init__(self, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            "MCP_PROTOCOL_ERROR",
            "Invalid MCP protocol message format",
            details,
        )


class ModelExecutionException(AgentRuntimeErrorException):
    def __init__(
        self,
        model_name: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "MODEL_EXECUTION_FAILED",
            f"Error occurred during execution of model: {model_name}",
            details,
        )


class ArtifactDeliveryException(AgentRuntimeErrorException):
    """A requested artifact exists or was attempted but no attachment arrived."""

    def __init__(self, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            "ARTIFACT_DELIVERY_INCOMPLETE",
            "文件产物未能作为附件交付，请重试；已生成文件会保留在当前工作区。",
            details,
        )


class ModelProtocolException(AgentRuntimeErrorException):
    """The provider produced a response that violated the tool protocol."""

    def __init__(self, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            "MODEL_PROTOCOL_ERROR",
            "模型没有按工具调用协议完成本轮，请重试或切换模型。",
            details,
        )


class ModelConfigurationIncompatibleException(AgentRuntimeErrorException):
    """The selected provider features cannot be used together."""

    def __init__(self, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            "MODEL_CONFIGURATION_INCOMPATIBLE",
            "当前模型配置与必需的工具调用不兼容，请关闭思考模式后重试或切换模型。",
            details,
        )


class ModelTimeoutException(AgentRuntimeErrorException):
    def __init__(
        self,
        model_name: str,
        timeout: int,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "MODEL_TIMEOUT",
            (
                f"Model inference timed out: {model_name}, timeout limit:"
                f" {timeout} seconds"
            ),
            details,
        )


class ModelNotFoundException(AgentRuntimeErrorException):
    def __init__(
        self,
        model_name: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "MODEL_NOT_FOUND",
            f"Model not found: {model_name}",
            details,
        )


class UnauthorizedModelAccessException(AgentRuntimeErrorException):
    def __init__(
        self,
        model_name: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "MODEL_UNAUTHORIZED_ACCESS",
            f"Unauthorized access to model: {model_name}",
            details,
        )


class UnknownAgentException(AgentRuntimeErrorException):
    def __init__(
        self,
        original_exception: Optional[Exception] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        message = "Unknown agent error"
        if original_exception is not None:
            message += (
                f": {type(original_exception).__name__}: "
                f"{str(original_exception)}"
            )
        super().__init__("AGENT_UNKNOWN_ERROR", message, details)


class ModelQuotaExceededException(AgentRuntimeErrorException):
    def __init__(
        self,
        model_name: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "MODEL_QUOTA_EXCEEDED",
            f"Model quota exceeded: {model_name}",
            details,
        )


class TokenDanceRecoveryException(AgentRuntimeErrorException):
    """Actionable recovery supplied by TokenDance's response contract."""

    _MESSAGES = {
        "top_up_balance": (
            "TOKENDANCE_TOP_UP_REQUIRED",
            "TokenDance balance is insufficient. Top up the balance, then retry.",
        ),
        "reauthorize_api_key": (
            "TOKENDANCE_REAUTHORIZE_REQUIRED",
            "The TokenDance API Key is no longer usable. Reauthorize "
            "TokenDance in Settings > Models.",
        ),
        "api_key_quota": (
            "TOKENDANCE_API_KEY_QUOTA",
            "The TokenDance API Key's periodic quota is exhausted. Wait for "
            "the quota to refresh or reauthorize with a new quota.",
        ),
    }

    def __init__(
        self,
        action: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        code, message = self._MESSAGES[action]
        super().__init__(code, message, details)


class VideoGenerationAttemptException(AgentRuntimeErrorException):
    """A video attempt whose task identity and failover policy are known."""

    def __init__(
        self,
        message: str,
        *,
        task_id: str = "",
        may_fail_over: bool = False,
        response: Any = None,
    ) -> None:
        super().__init__(
            "VIDEO_GENERATION_ATTEMPT_ERROR",
            message,
            {
                "task_id": task_id,
                "may_fail_over": may_fail_over,
            },
        )
        self.task_id = task_id
        self.may_fail_over = may_fail_over
        if response is not None:
            self.response = response


def get_tokendance_recovery_action(exc: Exception) -> str:
    """Return a validated TokenDance recovery action from an API error."""

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        value = headers.get("TokenDance-Recovery-Action", "")
    except (AttributeError, TypeError):
        return ""
    action = str(value or "").strip()
    return action if action in TokenDanceRecoveryException._MESSAGES else ""


class ModelContextLengthExceededException(AgentRuntimeErrorException):
    def __init__(
        self,
        model_name: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            "MODEL_CONTEXT_LENGTH_EXCEEDED",
            f"Model context length exceeded: {model_name}",
            details,
        )


# ==================== Lantai Business Exceptions ====================


class ProviderError(AgentRuntimeErrorException):
    """Exception raised when there's an error with a model provider."""

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("PROVIDER_ERROR", message, details)


class ModelFormatterError(AgentRuntimeErrorException):
    """Exception raised when there's an error with model message formatting."""

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("MODEL_FORMATTER_ERROR", message, details)


class SystemCommandException(AgentRuntimeErrorException):
    """Exception raised when there's an error with system command execution."""

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("SYSTEM_COMMAND_ERROR", message, details)


class ChannelError(ExternalServiceException):
    """Exception raised for channel communication errors."""

    def __init__(
        self,
        channel_name: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize channel error."""
        # Add channel_name to details for better debugging
        if details is None:
            details = {}
        details["channel"] = channel_name

        # Call parent with service_name set to channel_name
        super().__init__(
            service_name=channel_name,
            message=message,
            details=details,
        )


class AgentStateError(AgentRuntimeErrorException):
    """Exception raised for agent state and session errors."""

    def __init__(
        self,
        session_id: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if details is None:
            details = {}
        # Add session_id to details for better debugging
        details["session_id"] = session_id
        super().__init__("AGENT_STATE_ERROR", message, details)


class SkillsError(AgentRuntimeErrorException):
    """Exception raised for skills management errors."""

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__("SKILLS_ERROR", message, details)


# ==================== LLM API Exception Converter ====================


def _is_model_related_error(exc: Exception) -> bool:
    """Check if exception is likely related to LLM model execution.

    Args:
        exc: Exception to check

    Returns:
        True if likely a model-related error, False otherwise
    """
    # Check exception type name
    exc_type_name = type(exc).__name__.lower()

    # Common LLM provider exception names
    model_exception_types = [
        "api",
        "model",
        "openai",
        "anthropic",
        "completion",
        "chat",
        "generation",
        "inference",
        "llm",
    ]

    if any(keyword in exc_type_name for keyword in model_exception_types):
        return True

    # Check if has status_code attribute (typical for API errors)
    if hasattr(exc, "status_code"):
        return True

    # Check error message for model-related keywords
    error_msg = str(exc).lower()
    model_keywords = [
        "api",
        "model",
        "token",
        "completion",
        "chat",
        "openai",
        "anthropic",
        "rate limit",
        "quota",
        "context length",
        "authentication",
        "unauthorized",
        "forbidden",
        "timeout",
        "timed out",
    ]

    if any(keyword in error_msg for keyword in model_keywords):
        return True

    return False


def convert_model_exception(  # pylint: disable=too-many-return-statements
    exc: Exception,
    model_name: Optional[str] = None,
) -> AgentRuntimeErrorException:
    """Convert provider/runtime exceptions to Lantai runtime exceptions.

    Args:
        exc: Original exception
        model_name: Name of the model (optional, defaults to "unknown")

    Returns:
        AgentRuntimeErrorException with original details preserved
    """
    # Build details with original exception info
    details = {
        "original_error_type": type(exc).__name__,
        "original_error_message": str(exc),
    }

    original_type = type(exc).__name__
    original_message = str(exc)
    normalized_message = original_message.lower()
    if original_type == "_ModelProtocolError":
        if "requested file was not attached" in normalized_message:
            return ArtifactDeliveryException(details=details)
        return ModelProtocolException(details=details)
    if (
        "tool_choice" in normalized_message
        and "required" in normalized_message
        and "thinking" in normalized_message
        and ("incompatible" in normalized_message or "cannot" in normalized_message)
    ):
        return ModelConfigurationIncompatibleException(details=details)

    # Level 0: Check if this is a model-related error
    if not _is_model_related_error(exc):
        # Non-model error: wrap as UnknownAgentException
        return UnknownAgentException(
            original_exception=exc,
            details=details,
        )

    # Extract information for model errors
    status_code = getattr(exc, "status_code", None)
    error_message = str(exc).lower()
    model = model_name or "unknown"
    details["model_name"] = model

    if status_code is not None:
        details["status_code"] = status_code

    recovery_action = get_tokendance_recovery_action(exc)
    if recovery_action:
        details["recovery_action"] = recovery_action
        return TokenDanceRecoveryException(
            recovery_action,
            details=details,
        )

    # Level 1: Status code mapping (most reliable)
    if status_code in (401, 403):
        return UnauthorizedModelAccessException(model, details=details)

    if status_code == 429:
        return ModelQuotaExceededException(model, details=details)

    # Level 2: Keyword mapping
    if any(
        kw in error_message
        for kw in [
            "unauthorized",
            "authentication",
            "api key",
            "invalid key",
            "forbidden",
        ]
    ):
        return UnauthorizedModelAccessException(model, details=details)

    if any(
        kw in error_message
        for kw in [
            "rate limit",
            "quota",
            "too many requests",
        ]
    ):
        return ModelQuotaExceededException(model, details=details)

    if any(
        kw in error_message
        for kw in [
            "timeout",
            "timed out",
            "deadline exceeded",
        ]
    ):
        return ModelTimeoutException(model, timeout=60, details=details)

    if any(
        kw in error_message
        for kw in [
            "context",
            "maximum context",
            "context window",
            "too many tokens",
        ]
    ):
        return ModelContextLengthExceededException(model, details=details)

    # Level 3: Model-related default catch-all
    return ModelExecutionException(model, details=details)
