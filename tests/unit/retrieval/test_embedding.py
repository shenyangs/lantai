# -*- coding: utf-8 -*-
"""Public behaviour locks for the bundled local embedding runtime."""

import numpy as np
import json

from lantai.retrieval import (
    LocalOnnxEmbeddingProvider,
    resolve_bundled_embedding_model_dir,
    OpenAICompatibleEmbeddingProvider,
)


def test_bundled_onnx_embedding_is_normalized_and_semantically_useful():
    provider = LocalOnnxEmbeddingProvider(resolve_bundled_embedding_model_dir())

    vectors = provider.embed(
        [
            "不同媒体渠道分别花了多少钱？",
            "媒体专项预算与各家报价明细",
            "给歌曲写一段更有记忆点的副歌旋律",
        ],
    )

    assert vectors.shape == (3, 512)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    assert float(np.dot(vectors[0], vectors[1])) > float(np.dot(vectors[0], vectors[2]))


def test_openai_compatible_embedding_adapter_uses_the_configured_endpoint(monkeypatch):
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {"data": [{"index": 0, "embedding": [3.0, 4.0]}]},
            ).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("lantai.retrieval.embedding.urllib.request.urlopen", fake_urlopen)
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:9999/v1",
        model_name="embedding-test",
        dimensions=2,
        api_key="secret",
    )

    vectors = provider.embed(["预算"])

    assert captured["url"] == "http://127.0.0.1:9999/v1/embeddings"
    assert captured["authorization"] == "Bearer secret"
    assert captured["payload"] == {"model": "embedding-test", "input": ["预算"]}
    assert np.allclose(vectors, [[0.6, 0.8]])
