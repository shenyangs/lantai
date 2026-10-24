# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from lantai.presentation.runtime import PresentationJobRuntime

from . import test_whole_job_lifecycle as base


def test_closed_runtime_refuses_new_job_before_durable_creation(tmp_path):
    runtime = PresentationJobRuntime(tmp_path / "jobs")
    runtime.shutdown()
    before = runtime.service.store.list_records()
    try:
        with pytest.raises(RuntimeError):
            runtime.create_and_start(base.request())
        assert runtime.service.store.list_records() == before
        # White-box lock exercises the actual private boundary.
        # pylint: disable-next=protected-access
        assert not runtime._futures
    finally:
        runtime.shutdown()
