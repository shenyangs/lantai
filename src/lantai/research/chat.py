"""Research admission from trusted server chat metadata; no new agent loop."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from .inference import ResearchModelClient, reserve, finish
from .projects import ResearchService
from .context import accounted_dispatch


def bind_chat(chat, agent_home):
    metadata = getattr(chat, 'meta', None)
    project = metadata.get('research_project_id') if isinstance(metadata, dict) else None
    if project is None:
        return None
    if not isinstance(project, str) or not project.strip():
        raise ValueError('Invalid research project association')
    binding = ResearchChatBinding(ResearchService(agent_home), project)
    binding.current()
    return binding


class ResearchChatBinding:
    def __init__(self, service, project):
        self.service, self.project = service, project

    def current(self):
        # Read only project metadata here: admission must not extract source files.
        with self.service.store.transaction() as db:
            project = self.service.store.current(self.project, db)
            self.service.store.expect(self.project, project['revision'], db)
        # The normal chat includes auxiliary calls whose egress is not yet bound
        # to project policy. Fail before preflight instead of claiming offline.
        if project['policy'] == 'offline':
            raise ValueError('Offline research main chat is unavailable until all auxiliary inference is locally bound; use local research review.')
        return project

    def _options(self, provider_id, model):
        from ..providers import ProviderManager
        provider = ProviderManager.get_instance().get_provider(provider_id)
        if provider is None:
            return {}
        return {'endpoint': getattr(provider, 'base_url', '') or '',
                'generation': provider.get_effective_generate_kwargs(model)}

    def wrap_native(self, client, *, surface="main_chat"):
        return _CurrentProjectClient(client, self.service, self.project, 0,
            surface=surface, binding=self, **self._options(client.provider_id, client.model_name))

    def wrap_legacy(self, model, *, surface="main_chat", provider_id=None):
        if isinstance(model, _LegacyProjectModel) and model.binding is self:
            return model
        return _LegacyProjectModel(model, self, surface=surface, provider_id=provider_id)


class _CurrentProjectClient(ResearchModelClient):
    def __init__(self, *args, binding, **kwargs):
        super().__init__(*args, **kwargs)
        self.binding = binding

    def _reserve(self, request):
        current = self.binding.current()
        return reserve(self.service, self.project, current['revision'], self.configuration(request), self.surface)


class _LegacyProjectModel:
    def __init__(self, inner, binding, *, surface, provider_id=None):
        self._inner, self.binding = inner, binding
        identity = SimpleNamespace(provider_id=provider_id or getattr(inner, '_provider_id', 'unknown'),
                                   model_name=getattr(inner, 'model_name', 'unknown'))
        self._ledger = binding.wrap_native(identity, surface=surface)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def __call__(self, *args, **kwargs):
        from ..runtime.models import ModelRequest
        # Only generation parameters belong in the ledger; messages/tools never do.
        generation = {k: v for k, v in kwargs.items() if k not in {
            'messages', 'tools', 'tool_choice', 'structured_model'}}
        identifier, allowance = self._ledger._reserve(ModelRequest(generation=generation))
        started, transferred, usage = time.monotonic(), False, None
        status = 'interrupted'
        try:
            with accounted_dispatch():
                result = await asyncio.wait_for(self._inner(*args, **kwargs), timeout=allowance)
            if hasattr(result, '__aiter__'):
                transferred = True
                return self._stream(result, identifier, allowance, started)
            usage, status = getattr(result, 'usage', None), 'completed'
            return result
        finally:
            if not transferred:
                finish(self.binding.service, identifier, time.monotonic() - started, status, usage)

    async def _stream(self, stream, identifier, allowance, started):
        usage, status = None, 'interrupted'
        stream = stream.__aiter__()
        try:
            while True:
                remaining = allowance - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError('Research model time budget exhausted; progress is preserved')
                try:
                    with accounted_dispatch():
                        chunk = await asyncio.wait_for(anext(stream), timeout=remaining)
                except StopAsyncIteration:
                    status = 'completed'
                    break
                if getattr(chunk, 'usage', None) is not None:
                    usage = chunk.usage
                yield chunk
        finally:
            try:
                if hasattr(stream, 'aclose'):
                    with accounted_dispatch():
                        await stream.aclose()
            finally:
                finish(self.binding.service, identifier, time.monotonic() - started, status, usage)
