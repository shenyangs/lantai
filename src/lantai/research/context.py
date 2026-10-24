"""Task-local research accounting, resolved when cached models dispatch."""
from contextlib import contextmanager
from contextvars import ContextVar

_binding = ContextVar('research_turn_binding', default=None)
_accounted = ContextVar('research_accounted_dispatch', default=False)


def current_binding():
    return _binding.get()


def set_binding(binding):
    return _binding.set(binding)


def reset_binding(token):
    _binding.reset(token)


@contextmanager
def research_turn(binding):
    token = set_binding(binding)
    try:
        yield
    finally:
        reset_binding(token)


@contextmanager
def accounted_dispatch():
    """Suppress nested bookkeeping only while executing an explicit dispatch."""
    token = _accounted.set(True)
    try:
        yield
    finally:
        _accounted.reset(token)


def native_dispatch(client):
    binding = current_binding()
    if binding is None or _accounted.get():
        return client
    return binding.wrap_native(client, surface='auxiliary_text')


def legacy_dispatch(model, provider_id):
    binding = current_binding()
    if binding is None or _accounted.get():
        return model
    return binding.wrap_legacy(model, surface='auxiliary_text', provider_id=provider_id)
