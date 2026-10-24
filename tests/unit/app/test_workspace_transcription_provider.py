from lantai.app.routers import workspace as workspace_router
from lantai.config.config import AgentsConfig


REMOVED_CONFIG_FIELDS = {
    "audio_mode",
    "transcription_provider_type",
    "transcription_provider_id",
    "transcription_model",
}

REMOVED_WORKSPACE_PATHS = {
    "/workspace/audio-mode",
    "/workspace/transcription-provider-type",
    "/workspace/local-whisper-status",
    "/workspace/transcription-providers",
    "/workspace/transcription-provider",
    "/workspace/transcribe",
}


def test_transcription_configuration_is_removed():
    assert REMOVED_CONFIG_FIELDS.isdisjoint(AgentsConfig.model_fields)


def test_legacy_transcription_configuration_is_ignored_on_load():
    config = AgentsConfig.model_validate(
        {
            "audio_mode": "auto",
            "transcription_provider_type": "whisper_api",
            "transcription_provider_id": "openai",
            "transcription_model": "whisper-1",
        }
    )

    assert REMOVED_CONFIG_FIELDS.isdisjoint(config.model_dump())


def test_transcription_routes_are_removed():
    route_paths = {route.path for route in workspace_router.router.routes}

    assert REMOVED_WORKSPACE_PATHS.isdisjoint(route_paths)
