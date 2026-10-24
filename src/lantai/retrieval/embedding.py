# -*- coding: utf-8 -*-
"""Local-first embedding implementations used by retrieval projections."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
from functools import lru_cache
from pathlib import Path
from threading import Lock

import numpy as np


_DEFAULT_MODEL_SLUG = "bge-small-zh-v1.5"


def resolve_bundled_embedding_model_dir(model_slug: str = _DEFAULT_MODEL_SLUG) -> Path:
    """Resolve one bundled model in source and PyInstaller runtimes."""

    override = os.environ.get("LANTAI_EMBEDDING_MODEL_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "lantai" / "embedding_models" / model_slug
    return Path(__file__).resolve().parents[1] / "embedding_models" / model_slug


class LocalOnnxEmbeddingProvider:
    """Sentence embedding using a pinned local tokenizer and ONNX model."""

    def __init__(self, model_dir: str | Path, *, max_length: int = 512) -> None:
        self.model_dir = Path(model_dir)
        self.max_length = max(8, min(512, int(max_length)))
        self._manifest = self._load_and_verify_manifest()
        self.model_name = str(self._manifest["model_name"])
        self.dimensions = int(self._manifest["dimensions"])
        self.fingerprint = str(self._manifest["fingerprint"])
        self._tokenizer = None
        self._session = None
        self._load_lock = Lock()

    def _load_and_verify_manifest(self) -> dict:
        manifest_path = self.model_dir / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"embedding manifest is missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for relative, expected in dict(manifest.get("sha256") or {}).items():
            path = self.model_dir / relative
            if not path.is_file():
                raise FileNotFoundError(f"embedding model file is missing: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected:
                raise RuntimeError(f"embedding model checksum mismatch: {relative}")
        return manifest

    def _ensure_loaded(self) -> None:
        if self._session is not None and self._tokenizer is not None:
            return
        with self._load_lock:
            if self._session is not None and self._tokenizer is not None:
                return
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=self.max_length)
            tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
            options = ort.SessionOptions()
            options.intra_op_num_threads = min(4, max(1, os.cpu_count() or 1))
            options.inter_op_num_threads = 1
            session = ort.InferenceSession(
                str(self.model_dir / "onnx" / "model_quantized.onnx"),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._tokenizer = tokenizer
            self._session = session

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimensions), dtype=np.float32)
        from ..research.embedding_budget import embedding_dispatch
        with embedding_dispatch(self, 'local-onnx') as dispatch:
            return self._embed(texts, dispatch)

    def _embed(self, texts, dispatch):
        self._ensure_loaded()
        assert self._tokenizer is not None
        assert self._session is not None
        encodings = self._tokenizer.encode_batch([str(text) for text in texts])
        input_ids = np.asarray([item.ids for item in encodings], dtype=np.int64)
        attention_mask = np.asarray(
            [item.attention_mask for item in encodings],
            dtype=np.int64,
        )
        token_type_ids = np.asarray(
            [item.type_ids for item in encodings],
            dtype=np.int64,
        )
        available_inputs = {item.name for item in self._session.get_inputs()}
        feed = {"input_ids": input_ids, "attention_mask": attention_mask}
        if "token_type_ids" in available_inputs:
            feed["token_type_ids"] = token_type_ids
        output = np.asarray(self._session.run(None, feed)[0], dtype=np.float32)
        dispatch.usage = {'total_tokens': int(attention_mask.sum())}
        if output.ndim == 3:
            mask = attention_mask[:, :, None].astype(np.float32)
            output = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1e-9)
        if output.shape != (len(texts), self.dimensions):
            raise RuntimeError(
                f"unexpected local embedding shape {output.shape}; "
                f"expected {(len(texts), self.dimensions)}",
            )
        norms = np.linalg.norm(output, axis=1, keepdims=True)
        if np.any(norms <= 0) or not np.isfinite(output).all():
            raise RuntimeError("local embedding returned invalid vectors")
        return (output / norms).astype(np.float32, copy=False)


class OpenAICompatibleEmbeddingProvider:
    """Remote embedding Adapter for explicitly configured compatible endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        model_name: str,
        dimensions: int,
        api_key: str = "",
        timeout_seconds: float = 10.0,
        use_dimensions: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.dimensions = int(dimensions)
        self.api_key = api_key
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.use_dimensions = bool(use_dimensions)
        self.fingerprint = (
            f"openai-compatible:{self.base_url}:{self.model_name}:{self.dimensions}"
        )

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimensions), dtype=np.float32)
        from ..research.embedding_budget import embedding_dispatch
        with embedding_dispatch(self, 'openai-compatible-embedding') as dispatch:
            return self._embed(texts, dispatch)

    def _embed(self, texts, dispatch):
        endpoint = (
            self.base_url
            if self.base_url.endswith("/embeddings")
            else f"{self.base_url}/embeddings"
        )
        payload: dict[str, object] = {"model": self.model_name, "input": texts}
        if self.use_dimensions:
            payload["dimensions"] = self.dimensions
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=min(self.timeout_seconds, dispatch.allowance)) as response:
            body = json.loads(response.read().decode("utf-8"))
        dispatch.usage = body.get('usage')
        data = sorted(list(body.get("data") or []), key=lambda item: int(item["index"]))
        vectors = np.asarray([item["embedding"] for item in data], dtype=np.float32)
        expected = (len(texts), self.dimensions)
        if vectors.shape != expected or not np.isfinite(vectors).all():
            raise RuntimeError(
                f"remote embedding shape {vectors.shape} does not match {expected}",
            )
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms <= 0):
            raise RuntimeError("remote embedding returned zero-length vectors")
        return (vectors / norms).astype(np.float32, copy=False)


@lru_cache(maxsize=1)
def get_default_local_embedding_provider() -> LocalOnnxEmbeddingProvider:
    """Reuse one lazy ONNX session across Wiki and Memory runtimes."""

    return LocalOnnxEmbeddingProvider(resolve_bundled_embedding_model_dir())


def create_embedding_provider(config: dict) -> object | None:
    """Resolve effective config to a real local or remote implementation."""

    backend = str(config.get("backend") or "").strip().lower()
    model_name = str(config.get("model_name") or "").strip()
    if backend in {"local", "onnx"} or model_name.lower().startswith("local/"):
        return get_default_local_embedding_provider()
    base_url = str(config.get("base_url") or "").strip()
    if base_url and model_name:
        return OpenAICompatibleEmbeddingProvider(
            base_url=base_url,
            model_name=model_name,
            dimensions=int(config.get("dimensions") or 0),
            api_key=str(config.get("api_key") or ""),
            use_dimensions=bool(config.get("use_dimensions")),
        )
    return None
