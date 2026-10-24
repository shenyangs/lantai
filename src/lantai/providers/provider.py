# -*- coding: utf-8 -*-
"""Definition of Provider."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List, Type

from pydantic import BaseModel, ConfigDict, Field

from lantai.providers.legacy_chat_models import resolve_legacy_chat_model_cls
from lantai.providers.model_endpoint import ProviderModelEndpoint

if TYPE_CHECKING:
    from .multimodal_prober import ProbeResult


class ModelInfo(BaseModel):
    id: str = Field(..., description="Model identifier used in API calls")
    name: str = Field(..., description="Human-readable model name")
    supports_multimodal: bool | None = Field(
        default=None,
        description="Whether this model supports multimodal input "
        "(image/audio/video). None means not yet probed.",
    )
    supports_image: bool | None = Field(
        default=None,
        description="Whether this model supports image input. "
        "None means not yet probed.",
    )
    supports_video: bool | None = Field(
        default=None,
        description="Whether this model supports video input. "
        "None means not yet probed.",
    )
    supports_tool_use: bool | None = Field(
        default=None,
        description=(
            "Whether the serving runtime reports native tool/function "
            "calling support. None means unknown."
        ),
    )
    probe_source: str | None = Field(
        default=None,
        description=(
            "Probe result source: 'documentation' (from docs)"
            " or 'probed' (actual probe)"
        ),
    )
    is_free: bool = Field(
        default=False,
        description="Whether this model is free to use (e.g., no API cost)",
    )
    generate_kwargs: Dict[str, Any] = Field(
        default_factory=dict,
        description="Per-model generation parameters that override "
        "provider-level generate_kwargs.",
    )
    context_window: int | None = Field(
        default=None,
        description=(
            "Currently usable combined input/output context tokens. For "
            "local runtimes this prefers the loaded instance configuration."
        ),
    )
    max_context_window: int | None = Field(
        default=None,
        description=(
            "Maximum context window reported by the model artifact, which "
            "may be larger than the currently loaded runtime window."
        ),
    )
    user_context_window: int | None = Field(
        default=None,
        description=(
            "Optional user ceiling for this exact provider/model. "
            "It does not replace the reported model capability."
        ),
    )
    max_output_tokens: int | None = Field(
        default=None,
        description="Reported maximum completion tokens for this model.",
    )
    tokenizer: str | None = Field(
        default=None,
        description="Optional tokenizer identifier exposed by the provider.",
    )
    recommended_output_reserve: int | None = Field(
        default=None,
        description=(
            "Recommended output/reasoning reserve used by the product policy."
        ),
    )
    capability_source: str | None = Field(
        default=None,
        description="Where the context capability metadata came from.",
    )
    capability_verified_at: str | None = Field(
        default=None,
        description="Optional ISO date for capability freshness diagnostics.",
    )
    runtime_state: str | None = Field(
        default=None,
        description=(
            "Runtime availability state such as loaded, available, or "
            "not-loaded."
        ),
    )
    parameter_size: str | None = Field(
        default=None,
        description="Human-readable model parameter count reported locally.",
    )
    quantization: str | None = Field(
        default=None,
        description="Local model quantization label, when reported.",
    )
    size_bytes: int | None = Field(
        default=None,
        description="Local model artifact size in bytes, when reported.",
    )
    supported_protocols: List[str] = Field(
        default_factory=list,
        description=(
            "Wire protocols explicitly advertised by the provider catalog. "
            "An empty list means the provider did not publish protocol data."
        ),
    )
    show_in_chat: bool = Field(
        default=True,
        description=(
            "Whether this chat-capable model appears in user-facing chat "
            "model selectors. Media capability routing is independent."
        ),
    )
    use_for_video: bool = Field(
        default=False,
        description=(
            "Whether this video-generation model is enabled for the "
            "conversation-triggered video tool."
        ),
    )

    @property
    def supports_chat(self) -> bool:
        """Whether catalog metadata exposes at least one chat protocol."""

        if not self.supported_protocols:
            return True
        return bool(
            {
                "openai:chat-completions",
                "openai:responses",
                "anthropic:messages",
                "gemini:generate-content",
            }.intersection(self.supported_protocols),
        )

    @property
    def supports_image_generation(self) -> bool:
        """Whether catalog metadata exposes an image-generation protocol."""

        return bool(
            {
                "openai:image-generations",
                "ark:image-generations",
            }.intersection(self.supported_protocols),
        )

    @property
    def supports_video_generation(self) -> bool:
        """Whether catalog metadata exposes a video-generation protocol."""

        return bool(
            {
                "seedance:generations",
                "vidu:text2video",
                "vidu:img2video",
                "vidu:reference2video",
                "vidu:start-end2video",
                "kling:text2video",
                "kling:image2video",
                "kling:omni-video",
                "wan3:video-synthesis",
                "happyhorse:video-synthesis",
                "minimax:video_generation_v2",
            }.intersection(self.supported_protocols),
        )

    @property
    def supports_text_to_video(self) -> bool:
        """Whether this exact catalog row can accept a text-only video task."""

        direct_protocols = {
            "seedance:generations",
            "kling:text2video",
            "kling:omni-video",
            "wan3:video-synthesis",
            "minimax:video_generation_v2",
        }
        if direct_protocols.intersection(self.supported_protocols):
            return True
        return (
            "happyhorse:video-synthesis" in self.supported_protocols
            and "t2v" in self.id.lower()
        )


class ExtendedModelInfo(ModelInfo):
    """Extended model info with additional metadata for providers."""

    provider: str = Field(
        default="",
        description="Provider/series (e.g., 'openai', 'google')",
    )
    input_modalities: List[str] = Field(
        default_factory=list,
        description="Supported input modalities",
    )
    output_modalities: List[str] = Field(
        default_factory=list,
        description="Supported output modalities",
    )
    pricing: Dict[str, str] = Field(
        default_factory=dict,
        description="Pricing info (prompt/completion)",
    )


class ProviderInfo(BaseModel):
    """Provider configuration and metadata."""

    # Allow flexible typing for test environments where ModelInfo
    # may be reloaded (different object identity)
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_default=False,
    )

    id: str = Field(..., description="Provider identifier")
    name: str = Field(..., description="Human-readable provider name")
    base_url: str = Field(default="", description="API base URL")
    api_key: str = Field(default="", description="API key for authentication")
    chat_model: str = Field(
        default="OpenAIChatModel",
        description="Legacy chat model name (e.g., 'OpenAIChatModel')",
    )
    models: List[ModelInfo] = Field(
        default_factory=list,
        description="List of pre-defined models",
    )
    extra_models: List[ModelInfo] = Field(
        default_factory=list,
        description="List of user-added models (not fetched from provider)",
    )

    api_key_prefix: str = Field(
        default="",
        description="Expected prefix for the API key (e.g., 'sk-')",
    )
    is_local: bool = Field(
        default=False,
        description="Whether this provider is for a local hosting platform",
    )
    freeze_url: bool = Field(
        default=False,
        description="Whether the base_url should be frozen (not editable)",
    )
    require_api_key: bool = Field(
        default=True,
        description="Whether this provider requires an API key",
    )
    is_custom: bool = Field(
        default=False,
        description=("Whether this provider is user-created (not built-in)."),
    )
    support_model_discovery: bool = Field(
        default=False,
        description=(
            "Whether this provider supports fetching available models"
            " from the provider's API"
        ),
    )
    support_connection_check: bool = Field(
        default=True,
        description=(
            "Whether this provider supports checking connection to the API "
            "without model configuration"
        ),
    )
    supports_video_generation: bool = Field(
        default=False,
        description=(
            "Whether the provider implements the text-to-video runtime contract."
        ),
    )
    generate_kwargs: Dict[str, Any] = Field(
        default_factory=dict,
        description="Generation parameters for legacy chat models.",
    )
    meta: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata for the provider "
        "(e.g., api_key_url, api_key_hint).",
    )
    configuration_revision: int = Field(
        default=1,
        ge=1,
        description="Monotonic version of connection-affecting config",
    )
    connection_verified_revision: int | None = Field(
        default=None,
        description="Config revision used by the last connection probe",
    )
    connection_verified_at: str | None = Field(
        default=None,
        description="UTC timestamp of the last successful connection probe",
    )
    model_verified_revision: int | None = Field(
        default=None,
        description="Config revision used by the last model probe",
    )
    model_verified_id: str | None = Field(
        default=None,
        description="Exact model ID used by the last successful model probe",
    )
    model_verified_at: str | None = Field(
        default=None,
        description="UTC timestamp of the last successful model probe",
    )


class Provider(ProviderInfo, ABC):
    """Represents a provider instance with its configuration."""

    @abstractmethod
    async def check_connection(self, timeout: float = 5) -> tuple[bool, str]:
        """Check if the provider is reachable with the current config."""

    @abstractmethod
    async def fetch_models(self, timeout: float = 5) -> List[ModelInfo]:
        """Fetch the list of available models from the provider."""

    @abstractmethod
    async def check_model_connection(
        self,
        model_id: str,
        timeout: float = 5,  # pylint: disable=unused-argument
    ) -> tuple[bool, str]:
        """Check if a specific model is reachable/usable."""

    async def add_model(
        self,
        model_info: ModelInfo,
        target: str = "extra_models",
        timeout: float = 10,  # pylint: disable=unused-argument
    ) -> tuple[bool, str]:
        """Add a model to the provider's model list."""
        model_info.id = model_info.id.strip()
        if any(
            model.id.strip() == model_info.id
            for model in self.models + self.extra_models
        ):
            return False, f"Model '{model_info.id}' already exists"
        if target == "extra_models":
            self.extra_models.append(model_info)
        elif target == "models":
            self.models.append(model_info)
        else:
            return False, f"Invalid target '{target}' for adding model"
        return True, ""

    async def delete_model(
        self,
        model_id: str,
        timeout: float = 10,  # pylint: disable=unused-argument
    ) -> tuple[bool, str]:
        """Delete a model from the provider's model list."""
        model_id = model_id.strip()
        self.extra_models = [
            model
            for model in self.extra_models
            if model.id.strip() != model_id
        ]
        return True, ""

    def update_config(self, config: Dict) -> None:
        """Update provider configuration with the given dictionary."""
        before = self._verification_config_snapshot()
        if "name" in config and config["name"] is not None:
            self.name = str(config["name"]).strip()
        if (
            not self.freeze_url
            and "base_url" in config
            and config["base_url"] is not None
        ):
            self.base_url = str(config["base_url"]).strip()
        if "api_key" in config and config["api_key"] is not None:
            self.api_key = str(config["api_key"]).strip()
        if (
            self.is_custom
            and "chat_model" in config
            and config["chat_model"] is not None
        ):
            self.chat_model = str(config["chat_model"])
        if "api_key_prefix" in config and config["api_key_prefix"] is not None:
            self.api_key_prefix = str(config["api_key_prefix"])
        if (
            "generate_kwargs" in config
            and config["generate_kwargs"] is not None
            and isinstance(config["generate_kwargs"], dict)
        ):
            self.generate_kwargs = config["generate_kwargs"]
        if "extra_models" in config and config["extra_models"] is not None:
            # Always go through model_validate with dict data to
            # avoid class-identity issues from dual module loading.
            self.extra_models = [
                ModelInfo.model_validate(
                    model.model_dump()
                    if isinstance(model, BaseModel)
                    else model,
                )
                for model in config["extra_models"]
            ]
        if self._verification_config_snapshot() != before:
            self.configuration_revision += 1

    def _verification_config_snapshot(self) -> tuple[Any, ...]:
        """Return fields that can change the result of a real model call."""
        return (
            self.base_url,
            self.api_key,
            self.chat_model,
            self.generate_kwargs,
            [model.model_dump() for model in self.extra_models],
        )

    def get_chat_model_cls(self) -> Type[Any]:
        """Return the chat model class associated with this provider."""
        return resolve_legacy_chat_model_cls(
            self.chat_model,
            provider_name=self.name,
        )

    @staticmethod
    def _deep_merge(
        base: Dict[str, Any],
        override: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Recursively merge *override* into *base* (returns a new dict)."""
        result = dict(base)
        for key, val in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(val, dict)
            ):
                result[key] = Provider._deep_merge(result[key], val)
            else:
                result[key] = val
        return result

    def get_effective_generate_kwargs(self, model_id: str) -> Dict[str, Any]:
        """Return merged generate_kwargs: provider-level as base, model-level
        overrides on top (deep merge for nested dicts).

        Always returns a new dict so callers never mutate provider state.
        """
        for model in self.models + self.extra_models:
            if model.id == model_id:
                if model.generate_kwargs:
                    return self._deep_merge(
                        self.generate_kwargs,
                        model.generate_kwargs,
                    )
                break
        return dict(self.generate_kwargs)

    def update_model_config(
        self,
        model_id: str,
        config: Dict,
    ) -> bool:
        """Update per-model configuration (e.g. generate_kwargs)."""
        for model in self.models + self.extra_models:
            if model.id == model_id:
                before = model.model_dump()
                if (
                    "generate_kwargs" in config
                    and config["generate_kwargs"] is not None
                    and isinstance(config["generate_kwargs"], dict)
                ):
                    model.generate_kwargs = config["generate_kwargs"]
                if "user_context_window" in config:
                    raw_user_window = config["user_context_window"]
                    model.user_context_window = (
                        None
                        if raw_user_window in (None, "")
                        else max(1000, int(raw_user_window))
                    )
                if "show_in_chat" in config:
                    model.show_in_chat = bool(config["show_in_chat"])
                if "use_for_video" in config:
                    model.use_for_video = bool(config["use_for_video"])
                capability_changed = False
                for field_name in (
                    "context_window",
                    "max_output_tokens",
                    "recommended_output_reserve",
                ):
                    if field_name not in config:
                        continue
                    raw_value = config[field_name]
                    value = (
                        None
                        if raw_value in (None, "")
                        else max(1, int(raw_value))
                    )
                    setattr(model, field_name, value)
                    capability_changed = True
                if "tokenizer" in config:
                    model.tokenizer = (
                        str(config["tokenizer"]).strip()
                        if config["tokenizer"]
                        else None
                    )
                    capability_changed = True
                if capability_changed:
                    model.capability_source = str(
                        config.get("capability_source")
                        or "user-configured"
                    )
                if model.model_dump() != before:
                    self.configuration_revision += 1
                return True
        return False

    def has_model(self, model_id: str) -> bool:
        """Check if the provider has a model with the given ID."""
        return any(
            model.id == model_id for model in self.models + self.extra_models
        )

    def get_model_info(self, model_id: str) -> ModelInfo | None:
        """Return catalog/config metadata for one exact model ID."""

        return next(
            (
                model
                for model in self.models + self.extra_models
                if model.id == model_id
            ),
            None,
        )

    def supports_chat_model(self, model_id: str) -> bool:
        """Return whether a model is eligible for chat selection."""

        model = self.get_model_info(model_id)
        return bool(model and model.supports_chat)

    def supports_image_generation_model(self, model_id: str) -> bool:
        """Return whether a model is eligible for image generation.

        Providers without protocol metadata retain the historical permissive
        behavior. Protocol-aware catalogs can narrow the selection safely.
        """

        model = self.get_model_info(model_id)
        if model is None:
            return False
        if not model.supported_protocols:
            return callable(getattr(self, "generate_image", None))
        return model.supports_image_generation

    def _model_supports_multimodal(self, model_id: str) -> bool | None:
        """Return stored multimodal support for a model, if known."""

        for model in self.models + self.extra_models:
            if model.id == model_id:
                return model.supports_multimodal
        return None

    def resolve_model_endpoint(self, model_id: str) -> ProviderModelEndpoint:
        """Resolve this provider/model into a runtime model endpoint."""

        family = "openai"
        native_kind = "openai_compatible"
        if self.chat_model == "AnthropicChatModel":
            family = "anthropic"
            native_kind = "legacy"
        elif self.chat_model == "GeminiChatModel":
            family = "gemini"
            native_kind = "legacy"

        return ProviderModelEndpoint(
            provider_id=self.id,
            model_id=model_id,
            family=family,
            base_url=self.base_url,
            api_key=self.api_key,
            is_local=self.is_local,
            chat_model=self.chat_model,
            generate_kwargs=self.get_effective_generate_kwargs(model_id),
            supports_multimodal=self._model_supports_multimodal(model_id),
            native_kind=native_kind,
            normalize_content_to_text=bool(self.is_local),
        )

    @abstractmethod
    def get_chat_model_instance(self, model_id: str) -> Any:
        """Return an instance of the chat model associated with this
        provider and model_id."""

    async def probe_model_multimodal(
        self,
        model_id: str,  # pylint: disable=unused-argument
        timeout: float = 10,  # pylint: disable=unused-argument
        image_only: bool = False,  # pylint: disable=unused-argument
    ) -> ProbeResult:
        """Probe if a model supports multimodal input.

        Args:
            model_id: Model identifier.
            timeout: Per-probe timeout in seconds.
            image_only: When True, skip the video probe and return after
                the image probe only.  Use this for fast checks (e.g.
                from ``view_image``) to avoid blocking on the slower
                video probe.

        Default implementation returns ProbeResult() (all False).
        Subclasses with API access should override.
        """
        from .multimodal_prober import ProbeResult

        return ProbeResult()

    async def get_info(self, mock_secret: bool = True) -> ProviderInfo:
        """Return a ProviderInfo instance with the provider's details."""
        api_key = (
            self.api_key_prefix + "*" * 6
            if mock_secret and self.api_key
            else self.api_key
        )
        # Serialize models/extra_models to plain dicts so that
        # ProviderInfo constructs fresh ModelInfo instances using
        # the class in its own module scope.  This avoids pydantic
        # class-identity mismatches when the same module is loaded
        # via two different import paths (e.g. PYTHONPATH + pip install).
        public_meta = dict(self.meta)
        if self.id == "tokendance":
            # These records authorize use of the encrypted credential.  They
            # are backend persistence, not provider presentation metadata.
            public_meta.pop("tokendance_credential_binding", None)
            public_meta.pop("tokendance_payment_sessions", None)

        return ProviderInfo(
            id=self.id,
            name=self.name,
            base_url=self.base_url,
            api_key=api_key,
            chat_model=self.chat_model,
            models=[m.model_dump() for m in self.models],
            extra_models=[m.model_dump() for m in self.extra_models],
            api_key_prefix=self.api_key_prefix,
            is_local=self.is_local,
            is_custom=self.is_custom,
            support_model_discovery=self.support_model_discovery,
            # custom providers are assumed to not support connection check
            support_connection_check=self.support_connection_check
            and not self.is_custom,
            supports_video_generation=self.supports_video_generation,
            freeze_url=self.freeze_url,
            require_api_key=self.require_api_key,
            generate_kwargs=self.generate_kwargs,
            meta=public_meta,
            configuration_revision=self.configuration_revision,
            connection_verified_revision=self.connection_verified_revision,
            connection_verified_at=self.connection_verified_at,
            model_verified_revision=self.model_verified_revision,
            model_verified_id=self.model_verified_id,
            model_verified_at=self.model_verified_at,
        )
