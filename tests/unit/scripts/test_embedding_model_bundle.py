# -*- coding: utf-8 -*-
"""Release policy locks for the offline semantic retrieval runtime."""

from pathlib import Path

from lantai.retrieval import LocalOnnxEmbeddingProvider, resolve_bundled_embedding_model_dir


ROOT = Path(__file__).resolve().parents[3]


def test_pyinstaller_bundles_the_model_and_native_embedding_dependencies():
    spec = (ROOT / "lantai-server.spec").read_text(encoding="utf-8")

    assert "src/lantai/embedding_models" in spec
    assert "onnxruntime" in spec
    assert "tokenizers" in spec


def test_bundled_embedding_manifest_verifies_every_release_asset():
    provider = LocalOnnxEmbeddingProvider(resolve_bundled_embedding_model_dir())

    assert provider.model_name == "BAAI/bge-small-zh-v1.5"
    assert provider.dimensions == 512
