# -*- coding: utf-8 -*-

from lantai.config.config import EmbeddingModelConfig


def test_embedding_defaults_are_offline_and_match_the_bundled_model():
    config = EmbeddingModelConfig()

    assert config.backend == "local"
    assert config.model_name == "local/bge-small-zh-v1.5"
    assert config.dimensions == 512
    assert config.base_url == ""
    assert config.api_key == ""


def test_legacy_blank_remote_profile_is_migrated_when_loaded():
    config = EmbeddingModelConfig(
        backend="openai",
        base_url="",
        model_name="",
        dimensions=1024,
    )

    assert config.backend == "local"
    assert config.model_name == "local/bge-small-zh-v1.5"
    assert config.dimensions == 512
