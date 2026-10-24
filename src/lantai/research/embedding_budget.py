"""Account synchronous embedding batches without retaining inputs or credentials."""
from contextlib import contextmanager
from dataclasses import dataclass
import time

from .context import current_binding
from .evidence import _digest
from .inference import reserve, finish


@dataclass
class EmbeddingDispatch:
    allowance: float = float('inf')
    usage: dict | None = None


@contextmanager
def embedding_dispatch(provider, backend):
    # Providers may be cached globally. Ownership must be resolved per call.
    binding = current_binding()
    dispatch = EmbeddingDispatch()
    if binding is None:
        yield dispatch
        return
    current = binding.current()
    configuration = {
        'provider': backend, 'model': provider.model_name,
        'dimensions': provider.dimensions,
        'fingerprint_hash': _digest(provider.fingerprint.encode()),
    }
    if backend == 'local-onnx':
        configuration['max_length'] = provider.max_length
    else:
        configuration['use_dimensions'] = provider.use_dimensions
    identifier, dispatch.allowance = reserve(binding.service, binding.project,
        current['revision'], configuration, 'embedding')
    started, status = time.monotonic(), 'interrupted'
    try:
        yield dispatch
        status = 'completed'
    finally:
        # Synchronous ONNX work/socket reads cannot promise a hard wall-clock cap.
        # Record the full observed time, including overruns, to gate the next call.
        finish(binding.service, identifier, time.monotonic() - started, status, dispatch.usage)
