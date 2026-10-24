# -*- coding: utf-8 -*-
import logging
import os
import time

from .utils.logging import setup_logger

# Fallback before we can safely read canonical constant definitions.
LOG_LEVEL_ENV = "LANTAI_LOG_LEVEL"

_bootstrap_err: Exception | None = None
if (
    os.environ.get("LANTAI_SKILL_SCRIPT_CHILD") != "1"
    and os.environ.get("LANTAI_ANALYSIS_WORKER_CHILD") != "1"
    and os.environ.get("LANTAI_METADATA_ONLY") != "1"
):
    try:
        # Load persisted env vars before importing modules that read env-backed
        # constants at import time (e.g., WORKING_DIR). Sandboxed Skill
        # children already receive their manifest-declared environment from
        # the parent and must not inspect unrelated host credentials.
        from .envs import load_envs_into_environ

        load_envs_into_environ()
    except Exception as exc:
        # Best effort: package import should not fail if env bootstrap fails.
        _bootstrap_err = exc

_t0 = time.perf_counter()
setup_logger(os.environ.get(LOG_LEVEL_ENV, "info"))
if _bootstrap_err is not None:
    logging.getLogger(__name__).warning(
        "lantai: failed to load persisted envs on init: %s",
        _bootstrap_err,
    )
logging.getLogger(__name__).debug(
    "%.3fs package init",
    time.perf_counter() - _t0,
)
