# -*- coding: utf-8 -*-
"""Bounded, ordered page authoring inside one existing writer operation."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from dataclasses import replace
from threading import Event, Lock
from typing import Callable, Sequence

from ..utils.operation_control import bind_operation, current_operation
from .jobs import PresentationJobInterruptedError
from .models import DeckPlanSlide, DeckSlide


def author_pages_ordered(
    pages: Sequence[tuple[DeckPlanSlide, DeckSlide]],
    author: Callable[[DeckPlanSlide, DeckSlide], DeckSlide],
    *,
    max_workers: int = 1,
) -> list[DeckSlide]:
    """Keep publication identity and join every sibling before retiring it.

    Only explicitly concurrency-safe authors opt in.  No worker allocates a
    new job, deadline, retry allowance or budget; each inherits the caller's
    context and shares its resource settlement tracker.
    """
    if max_workers <= 1 or len(pages) <= 1:
        return [author(planned, scaffold) for planned, scaffold in pages]

    parent = current_operation()
    stopped = Event()
    failure_lock = Lock()
    failures: list[BaseException] = []

    def check():
        if parent is not None:
            parent.ensure_active()
        if stopped.is_set():
            raise PresentationJobInterruptedError("page author batch was retired")

    def controlled_author(pair):
        try:
            if parent is None:
                check()
                return author(*pair)
            with bind_operation(replace(parent, ensure_active=check)):
                check()
                return author(*pair)
        except BaseException as exc:
            # Preserve the original failure, not a sibling's cancellation.
            with failure_lock:
                if not failures:
                    failures.append(exc)
                stopped.set()
            raise

    workers = min(3, max_workers, len(pages))
    executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="lantai-presentation-author",
    )
    active = {}
    ordered = {}
    next_index = 0
    try:
        while next_index < len(pages) or active:
            while next_index < len(pages) and len(active) < workers:
                check()
                future = executor.submit(copy_context().run, controlled_author, pages[next_index])
                active[future] = next_index
                next_index += 1
            done, _pending = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                ordered[active.pop(future)] = future.result()
        return [ordered[index] for index in range(len(pages))]
    except BaseException:
        stopped.set()
        for future in active:
            future.cancel()
        with failure_lock:
            failure = failures[0] if failures else None
        if failure is not None:
            raise failure
        raise
    finally:
        # A failed author or repeated Stop must not release the writer while
        # an admitted utility call is still in its real cleanup block.
        executor.shutdown(wait=True, cancel_futures=True)
