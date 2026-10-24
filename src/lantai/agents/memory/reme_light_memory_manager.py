# -*- coding: utf-8 -*-
"""ReMeLight-backed memory manager for agents."""
import asyncio
import hashlib
import json
import logging
import platform
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...runtime.converters import (
    legacy_message,
    legacy_message_from_dict,
    legacy_message_to_dict,
)
from ...runtime.tool_response import (
    TextBlock,
    ToolResponse,
    ToolResultBlock,
    ToolUseBlock,
)
from .base_memory_manager import BaseMemoryManager, memory_registry
from .canonical_ledger import CanonicalMemoryLedger, MemoryEvent
from .dreaming import MemoryDreamingService
from .migration import (
    migrate_memory_governance,
    migrate_memory_recall_safety,
    migrate_memory_source_grades,
)
from .layered_memory import (
    LayeredMemoryStore,
    extract_llm_atom_candidates,
    review_atom_candidates,
)
from .policy import MemoryPolicyResult
from .prompts import (
    MEMORY_GUIDANCE_ZH,
    MEMORY_GUIDANCE_EN,
)
from .structured_user_memory import (
    StructuredUserMemoryStore,
    build_memory_recall_plan,
    extract_structured_user_memory,
    infer_memory_type,
)
from ..memory_recall_policy import assess_memory_recall
from ..persona.user_understanding import (
    TurnUnderstandingProjection,
    UserUnderstandingTurnContext,
    UnderstandingAtom,
    UserUnderstandingStore,
    extract_user_understanding_candidates,
)
from ..persona.opportunity_queue import OpportunityStore
from ..persona.subject_continuity import (
    SubjectContinuityStore,
    SubjectTurnProjection,
)
from ..persona.turn_understanding import build_turn_understanding
from ..model_factory import create_model_and_formatter
from ..utils import get_token_counter
from ...config import load_config
from ...config.config import load_agent_config
from ...config.context import (
    set_current_workspace_dir,
    set_current_recent_max_bytes,
)
from ...constant import EnvVarLoader
from ...retrieval import (
    HybridRetrievalRuntime,
    IndexedDocument,
    RetrievalCandidate,
    create_embedding_provider,
)
from ...app.utility_model import run_memory_text_task

logger = logging.getLogger(__name__)

_REME_STORE_VERSION = "v1"
# Maximum number of tokens from query splitting
MAX_QUERY_TOKENS = 50
_LOCAL_MEMORY_FILE_LIMIT = 256
_LOCAL_MEMORY_FILE_MAX_BYTES = 512 * 1024
_MEMORY_RETRIEVAL_INDEX_FILE = "memory-retrieval.sqlite"
_LOW_INFORMATION_MEMORY_QUERIES = {
    "hi",
    "hello",
    "hey",
    "ok",
    "okay",
    "yes",
    "no",
    "你好",
    "您好",
    "嗨",
    "哈喽",
    "早",
    "早上好",
    "下午好",
    "晚上好",
    "嗯",
    "哦",
    "好",
    "好的",
    "行",
    "可以",
    "收到",
    "知道了",
    "不知道",
    "不清楚",
    "没事",
    "没关系",
    "随便",
    "随便聊聊",
    "聊聊",
    "闲聊",
    "都行",
    "谢谢",
    "多谢",
}


def _is_low_information_memory_query(query: str) -> bool:
    """Skip recall for turns that cannot benefit from historical context."""

    normalized = re.sub(r"\s+", "", (query or "").strip()).lower()
    if not normalized:
        return True
    if normalized in _LOW_INFORMATION_MEMORY_QUERIES:
        return True
    if not re.search(r"[\w\u3400-\u9fff]", normalized):
        return True
    return bool(
        re.fullmatch(
            r"(?:好|好的|行|可以|收到|知道了|没事|没关系|谢谢|多谢|嗯|哦)"
            r"[啊呀哟呢吧啦嘛呗~～!！。,.，]*",
            normalized,
        )
        or
        re.fullmatch(r"(?:哈|呵|嘿){2,}[啊呀哟~～!！。,.，]*", normalized)
        or re.fullmatch(r"(?:ha){2,}[!！。,.，]*", normalized)
        or re.fullmatch(r"lol+[!！。,.，]*", normalized)
    )


def _cfg_bool(value, default: bool) -> bool:
    """Return config bool while treating mocks/legacy missing attrs safely."""
    return value if isinstance(value, bool) else default


def _cfg_int(value, default: int) -> int:
    """Return config int while treating mocks/legacy missing attrs safely."""
    return value if isinstance(value, int) else default


def _cfg_float(value, default: float) -> float:
    """Return config float while treating mocks/legacy missing attrs safely."""
    return float(value) if isinstance(value, (int, float)) else default


def _detect_memory_manager_backend() -> str:
    """Detect the memory store backend from environment variables.

    Resolves ``MEMORY_STORE_BACKEND`` with the following priority:
    - ``local``: always used on Windows
    - ``chroma``: used when ``chromadb`` is importable (non-Windows)
    - falls back to ``local`` when ``chromadb`` is unavailable

    Returns:
        Backend name string: ``"local"``, ``"chroma"``, or any explicitly
        configured value.
    """
    backend_env = EnvVarLoader.get_str("MEMORY_STORE_BACKEND", "auto")
    if backend_env != "auto":
        return backend_env

    if platform.system() == "Windows":
        return "local"

    try:
        import chromadb  # noqa: F401 pylint: disable=unused-import

        return "chroma"
    except Exception as exc:  # optional dependency / SQLite compatibility
        logger.info(
            "chromadb is unavailable; using the local memory backend: %s",
            exc,
        )
        return "local"


def _create_reme_backend(**kwargs: Any) -> Any | None:
    """Keep the historical factory seam while using Lantai-native memory only.

    The argument remains accepted so older internal call sites and tests can
    migrate without loading any external agent runtime.
    """

    del kwargs
    return None

@memory_registry.register("remelight")
class ReMeLightMemoryManager(BaseMemoryManager):
    """Memory manager backed by ReMeLight.

    Delegates lifecycle, search, and compaction to a ``ReMeLight`` instance
    (``self._reme``).
    """

    def __init__(self, working_dir: str, agent_id: str):
        super().__init__(working_dir=working_dir, agent_id=agent_id)
        self._reme = None
        self._native_started = False
        self.user_memory_store = StructuredUserMemoryStore(working_dir)
        self.user_understanding_store = UserUnderstandingStore(working_dir)
        self.opportunity_store = OpportunityStore(working_dir)
        self.subject_continuity_store = SubjectContinuityStore(
            working_dir,
            agent_id=agent_id,
        )
        # New writes use this append-only source of truth. Existing structured
        # memory stays readable during migration, but inferred facts no longer
        # become immediately injectable profile entries.
        self.memory_ledger = CanonicalMemoryLedger(working_dir)
        # Post-reply work must never build an unbounded maintenance backlog.
        # L0 evidence is still recorded while a costly extraction/consolidation
        # is in flight; a later turn can extract from its recent L0 window.
        self._atom_extraction_busy = False
        self._background_semantic_service = None
        self._system2_busy = False
        self._capture_turn_lock = asyncio.Lock()
        self._background_capture_tasks: set[asyncio.Task[Any]] = set()
        # Automatic recall runs on the foreground path. Rebuilding and syncing
        # the same SQLite FTS index before every model response makes ordinary
        # chat latency grow with memory size. Reuse it until one of its two
        # authoritative source files changes.
        self._memory_retrieval_index: HybridRetrievalRuntime | None = None
        self._memory_retrieval_source_signature: tuple[
            tuple[bool, int, int], ...
        ] | None = None
        self._retrieval_health_cache: dict[str, Any] | None = None
        self._retrieval_health_checked_at = 0.0
        # Lifecycle gate used by destructive maintenance.  Confirmation and
        # retrieval policy cannot protect a clear operation if a previously
        # accepted background turn writes its projections back mid-delete.
        self._accept_background_capture = True
        self.layered_memory = LayeredMemoryStore(
            working_dir=working_dir,
            agent_id=agent_id,
            user_memory_store=self.user_memory_store,
        )
        try:
            migrate_memory_governance(
                working_dir,
                apply=True,
                agent_id=agent_id,
            )
        except Exception:
            logger.exception("Memory governance migration failed; continuing with confirmed-only reads")
        try:
            migrate_memory_recall_safety(
                working_dir,
                apply=True,
                agent_id=agent_id,
            )
        except Exception:
            logger.exception(
                "Memory recall-safety migration failed; runtime recall gate remains active",
            )
        try:
            migrate_memory_source_grades(working_dir, apply=True)
        except Exception:
            logger.exception("Memory source-grade migration failed; original-source labels may be stale")

        logger.info(
            f"ReMeLightMemoryManager init: "
            f"agent_id={agent_id}, working_dir={working_dir}",
        )

        memory_manager_backend = _detect_memory_manager_backend()

        emb_config = self.get_embedding_config()
        vector_enabled = self._vector_index_enabled(emb_config)
        self._embedding_provider = None
        try:
            self._embedding_provider = create_embedding_provider(emb_config)
        except Exception:
            logger.warning(
                "Embedding model unavailable; Memory will use lexical recall",
                exc_info=True,
            )

        log_cfg = {
            **emb_config,
            "api_key": self._mask_key(emb_config["api_key"]),
        }
        logger.info(
            f"Embedding config: {log_cfg}, vector_enabled={vector_enabled}",
        )

        fts_enabled = EnvVarLoader.get_bool("FTS_ENABLED", True)

        agent_config = load_agent_config(self.agent_id)
        reme_cfg = agent_config.running.reme_light_memory_config
        rebuild_on_start = reme_cfg.rebuild_memory_index_on_start

        store_name = "memory"
        effective_rebuild = self._resolve_rebuild_on_start(
            working_dir=working_dir,
            store_version=_REME_STORE_VERSION,
            rebuild_on_start=rebuild_on_start,
        )

        recursive_file_watcher = reme_cfg.recursive_file_watcher

        self._reme = _create_reme_backend(
            working_dir=working_dir,
            default_embedding_model_config=emb_config,
            default_file_store_config={
                "backend": memory_manager_backend,
                "store_name": store_name,
                "vector_enabled": vector_enabled,
                "fts_enabled": fts_enabled,
            },
            default_file_watcher_config={
                "rebuild_index_on_start": effective_rebuild,
                "recursive": recursive_file_watcher,
            },
        )

    def _get_summary_model_and_formatter(self) -> tuple[Any, Any]:
        """Return the legacy model/formatter expected by ReMe summarization."""

        chat_model = getattr(self, "chat_model", None)
        formatter = getattr(self, "formatter", None)
        if chat_model is None or formatter is None:
            chat_model, formatter = create_model_and_formatter(self.agent_id)
            self.chat_model = chat_model
            self.formatter = formatter
        return chat_model, formatter

    @staticmethod
    def _mask_key(key: str) -> str:
        """Mask an API key, showing only the first 5 characters."""
        return key[:5] + "*" * (len(key) - 5) if len(key) > 5 else key

    def get_embedding_config(self) -> dict:
        """Return embedding config: config > env var > default."""
        cfg = load_agent_config(
            self.agent_id,
        ).running.reme_light_memory_config.embedding_model_config
        api_key = cfg.api_key or EnvVarLoader.get_str("EMBEDDING_API_KEY")
        base_url = cfg.base_url or EnvVarLoader.get_str("EMBEDDING_BASE_URL")
        model_name = cfg.model_name or EnvVarLoader.get_str("EMBEDDING_MODEL_NAME")
        backend = cfg.backend
        dimensions = cfg.dimensions
        # Profiles created before the bundled model existed persisted the old
        # blank OpenAI defaults. Treat that exact shape as an upgrade marker so
        # existing users gain local retrieval without editing agent.json.
        if (
            not base_url
            and not model_name
            and str(backend).lower() == "openai"
        ):
            backend = "local"
            model_name = "local/bge-small-zh-v1.5"
            dimensions = 512
        return {
            "backend": backend,
            "api_key": api_key,
            "base_url": base_url,
            "model_name": model_name,
            "dimensions": dimensions,
            "enable_cache": cfg.enable_cache,
            "use_dimensions": cfg.use_dimensions,
            "max_cache_size": cfg.max_cache_size,
            "max_input_length": cfg.max_input_length,
            "max_batch_size": cfg.max_batch_size,
        }

    @staticmethod
    def _local_worker_enabled(emb_config: dict) -> bool:
        backend = str(emb_config.get("backend") or "").lower()
        model_name = str(emb_config.get("model_name") or "").lower()
        return backend in {"local", "onnx"} or model_name.startswith("local/")

    @classmethod
    def _vector_index_enabled(cls, emb_config: dict) -> bool:
        """Return whether the configured embedding adapter can actually run.

        Remote adapters require an endpoint and a model.  Local/ONNX adapters
        deliberately have no endpoint, so treating ``base_url`` as mandatory
        silently disabled their vector index while the UI reported a worker.
        """

        model_name = bool(emb_config.get("model_name"))
        return model_name and (
            cls._local_worker_enabled(emb_config)
            or bool(emb_config.get("base_url"))
        )

    def _build_engine_status(self, snapshot: dict | None) -> dict:
        snapshot = snapshot or {}
        engine_status = dict(snapshot.get("engine_status") or {})
        structured_status = dict(engine_status.get("structured_memory") or {})
        layered_stats = dict(snapshot.get("stats") or {})
        emb_config = self.get_embedding_config()
        vector_enabled = self._vector_index_enabled(emb_config)
        try:
            health = self._memory_retrieval_health()
        except Exception as exc:
            logger.warning("Memory retrieval health probe failed", exc_info=True)
            health = {
                "overall": "degraded",
                "embedding": {"ready": False, "error": str(exc)},
                "vector": {
                    "ready": False, "records": 0, "write": False,
                    "query": False, "delete": False, "error": str(exc),
                },
            }
        embedding_health = dict(health.get("embedding") or {})
        vector_health = dict(health.get("vector") or {})
        vector_ready = bool(vector_health.get("ready"))
        structured_ready = (
            bool(structured_status) or self.user_memory_store is not None
        )
        overall = "ready" if structured_ready and vector_ready else "partial"

        engine_status["overall"] = overall
        engine_status["structured_memory"] = {
            "status": structured_status.get("status") or "已就绪",
            "visible_entries": int(structured_status.get("visible_entries") or 0),
            "pending_entries": int(structured_status.get("pending_entries") or 0),
            "total_entries": int(structured_status.get("total_entries") or 0),
        }
        engine_status["layered_memory"] = {
            "l0": int(layered_stats.get("l0_conversations") or 0),
            "l1": int(layered_stats.get("l1_atoms") or 0),
            "l2": int(layered_stats.get("l2_scenes") or 0),
            "system2": int(layered_stats.get("system2_models") or 0),
        }
        engine_status["vector_index"] = {
            "enabled": vector_ready,
            "configured": vector_enabled,
            "status": "已就绪" if vector_ready else ("不可用" if vector_enabled else "未配置"),
            "backend": "sqlite-exact-cosine",
            "records": int(vector_health.get("records") or 0),
            "sources": int(vector_health.get("sources") or 0),
            "stale_sources": int(vector_health.get("stale_sources") or 0),
            "records_source": "probe",
            "dimensions": embedding_health.get("dimensions") or emb_config.get("dimensions") or None,
            "probe": {
                "write": bool(vector_health.get("write")),
                "query": bool(vector_health.get("query")),
                "delete": bool(vector_health.get("delete")),
            },
            "error": vector_health.get("error"),
        }
        engine_status["embedding"] = {
            "enabled": bool(embedding_health.get("ready")),
            "configured": bool(emb_config.get("model_name")),
            "status": "已就绪" if embedding_health.get("ready") else ("不可用" if emb_config.get("model_name") else "未配置"),
            "provider": emb_config.get("backend") or None,
            "model": embedding_health.get("model") or emb_config.get("model_name") or None,
            "dimensions": embedding_health.get("dimensions") or emb_config.get("dimensions") or None,
            "fingerprint": embedding_health.get("fingerprint"),
            "error": embedding_health.get("error"),
        }

        if self._local_worker_enabled(emb_config):
            engine_status["local_worker"] = {
                "enabled": True,
                "status": "已就绪" if embedding_health.get("ready") else "不可用",
                "model": emb_config.get("model_name") or None,
            }

        return engine_status

    def _memory_retrieval_health(self) -> dict[str, Any]:
        now = time.monotonic()
        if (
            self._retrieval_health_cache is not None
            and now - self._retrieval_health_checked_at < 60.0
        ):
            return self._retrieval_health_cache
        runtime = self._build_memory_retrieval_index()
        health = runtime.health_probe()
        self._retrieval_health_cache = health
        self._retrieval_health_checked_at = now
        return health

    @staticmethod
    def _resolve_rebuild_on_start(
        working_dir: str,
        store_version: str,
        rebuild_on_start: bool,
    ) -> bool:
        """Return effective ``rebuild_index_on_start`` value.

        Uses a sentinel file ``.reme_store_{store_version}`` to detect whether
        the current store version has been initialized. Forces a one-time
        rebuild when the sentinel is absent. Bump *_REME_STORE_VERSION* to
        trigger another one-time rebuild on next start.
        """
        sentinel_name = f".reme_store_{store_version}"
        sentinel_path = Path(working_dir) / sentinel_name

        if sentinel_path.exists():
            return rebuild_on_start

        logger.info(
            f"Sentinel '{sentinel_name}' not found, forcing rebuild.",
        )

        try:
            for old in Path(working_dir).glob(".reme_store_*"):
                old.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"Failed to remove old sentinels: {e}")

        try:
            sentinel_path.touch()
        except Exception as e:
            logger.warning(f"Failed to create sentinel '{sentinel_name}': {e}")

        return True

    # ------------------------------------------------------------------
    # BaseMemoryManager interface
    # ------------------------------------------------------------------

    async def start(self):
        """Start the ReMeLight lifecycle."""
        self._native_started = True
        if self._reme is None:
            return None
        return await self._reme.start()

    async def close(self) -> bool:
        """Close ReMeLight and perform cleanup."""
        self._native_started = False
        self._accept_background_capture = False
        logger.info(
            f"ReMeLightMemoryManager closing: agent_id={self.agent_id}",
        )
        pending_captures = [
            task
            for task in self._background_capture_tasks
            if not task.done()
        ]
        if pending_captures:
            # Memory analysis is deliberately a background job.  A graceful
            # reload/shutdown must still let an already accepted turn finish
            # so the latest disclosure is not silently discarded.
            await asyncio.gather(*pending_captures, return_exceptions=True)
        self._background_capture_tasks.clear()
        if self._reme is None:
            return True
        result = await self._reme.close()
        logger.info(
            f"ReMeLightMemoryManager closed: agent_id={self.agent_id}, "
            f"result={result}",
        )
        return result

    async def capture_turn_serialized(self, **kwargs: Any) -> dict | None:
        """Capture one completed turn behind the lifecycle gate.

        Only the short, local evidence write is serialized inside
        :meth:`capture_turn`.  Semantic enrichment deliberately runs outside
        that lock: one slow utility-model request must never turn every later
        chat reply into a queued background model job.
        """

        if not self._accept_background_capture:
            return None
        return await self.capture_turn(**kwargs)

    async def quiesce_capture_for_clear(self) -> None:
        """Stop accepting captures and wait for every accepted write to settle."""

        self._accept_background_capture = False
        pending = [
            task
            for task in self._background_capture_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Also cover direct callers of ``capture_turn_serialized`` that are not
        # represented in the background-task registry.
        async with self._capture_turn_lock:
            pass

    def resume_capture_after_failed_clear(self) -> None:
        """Re-open the lifecycle gate when maintenance aborts before deletion."""

        self._accept_background_capture = True

    async def _run_background_capture(self, **kwargs: Any) -> None:
        try:
            result = await self.capture_turn_serialized(**kwargs)
            if isinstance(result, dict) and result.get("maintenance_required"):
                await self.run_system2_consolidation(
                    reason=str(kwargs.get("source") or "post_reply"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Background memory capture failed",
                exc_info=True,
            )

    def bind_background_semantic_service(self, service: Any) -> None:
        """Route accepted post-answer enrichment through the durable worker."""

        self._background_semantic_service = service

    async def handle_background_semantic_job(
        self,
        payload: dict[str, Any],
    ) -> None:
        """Rehydrate and execute one persisted memory enrichment job."""

        messages = [
            legacy_message_from_dict(item)
            for item in list(payload.get("messages") or [])
            if isinstance(item, dict)
        ]
        result = await self.capture_turn_serialized(
            messages=messages,
            session_id=str(payload.get("session_id") or ""),
            source=str(payload.get("source") or "post_reply"),
        )
        if isinstance(result, dict) and result.get("maintenance_required"):
            await self.run_system2_consolidation(
                reason=str(payload.get("source") or "post_reply"),
            )

    def schedule_capture_turn(self, **kwargs: Any) -> asyncio.Task[None]:
        """Start durable post-reply analysis without blocking the chat UI."""

        if not self._accept_background_capture:
            async def discarded_capture() -> None:
                return None

            return asyncio.create_task(
                discarded_capture(),
                name=f"memory-capture-discarded-{self.agent_id}",
            )
        service = self._background_semantic_service
        if service is not None:
            try:
                payload = {
                    "messages": [
                        legacy_message_to_dict(message)
                        for message in list(kwargs.get("messages") or [])
                    ],
                    "session_id": str(kwargs.get("session_id") or ""),
                    "source": str(kwargs.get("source") or "post_reply"),
                }
                fingerprint = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                key = (
                    "memory:"
                    + hashlib.sha256(
                    fingerprint.encode("utf-8"),
                    ).hexdigest()
                )
                job = service.submit(
                    kind="memory_capture",
                    payload=payload,
                    idempotency_key=key,
                )

                async def wait_for_durable_capture() -> None:
                    await service.wait_for_job(job.job_id)

                task = asyncio.create_task(
                    wait_for_durable_capture(),
                    name=f"memory-capture-durable-{self.agent_id}",
                )
                self._background_capture_tasks.add(task)
                task.add_done_callback(self._background_capture_tasks.discard)
                return task
            except Exception:
                logger.warning(
                    "Could not persist memory capture; using local fallback",
                    exc_info=True,
                )
        task = asyncio.create_task(
            self._run_background_capture(**kwargs),
            name=f"memory-capture-{self.agent_id}",
        )
        self._background_capture_tasks.add(task)
        task.add_done_callback(self._background_capture_tasks.discard)
        return task

    def get_memory_prompt(self, language: str = "zh") -> str:
        """Return the memory guidance prompt for the system prompt."""
        if not self.memory_ledger.get_preferences()["runtime_enabled"]:
            return ""
        prompts = {"zh": MEMORY_GUIDANCE_ZH, "en": MEMORY_GUIDANCE_EN}
        guidance = prompts.get(language, MEMORY_GUIDANCE_EN)
        structured_guidance = (
            "\n\n### 结构化用户记忆\n"
            "- 已确认只表示这条认识可信；只有当前问题相关且作用域匹配时才能注入。\n"
            "- 只有文本明确表达‘默认/每次/始终’的常驻规则才可以跨任务常驻生效。\n"
            "- 当用户纠正你、表达稳定偏好、给出长期规则或工作区约定时，"
            "优先调用 `memory_store` 写入结构化记忆。\n"
            "- 不要把临时任务进度、一次性 todo、密钥或系统提示写入记忆。"
            if language == "zh"
            else "\n\n### Structured User Memory\n"
            "- Confirmation means a memory is trustworthy, not applicable to every turn. "
            "Recall requires current relevance and a matching scope.\n"
            "- Only rules explicitly stated as defaults or always-on instructions may persist across tasks.\n"
            "- When the user corrects you or gives a durable preference, "
            "workflow convention, or standing rule, call `memory_store`.\n"
            "- Do not store temporary task progress, one-off todos, secrets, "
            "or system prompts."
        )
        # User-specific memory is deliberately absent from the static system
        # prompt.  Per-turn selection is the only runtime injection seam.
        return "\n\n".join([guidance, structured_guidance])

    def project_user_understanding_prompt(
        self,
        query: str,
        language: str = "zh",
    ) -> str:
        """Return the prompt view of the structured turn context."""

        return self.project_user_understanding_context(query, language).prompt

    def project_subject_continuity_context(
        self,
        query: str,
        language: str = "zh",
        local_time: str = "",
        session_id: str = "",
    ) -> SubjectTurnProjection:
        """Return the one durable subject projection shared by both runtimes."""

        del language
        return self.subject_continuity_store.project_for_turn(
            query,
            local_time=local_time,
            session_id=session_id,
        )

    def project_user_understanding_context(
        self,
        query: str,
        language: str = "zh",
    ) -> UserUnderstandingTurnContext:
        """Return one sparse context without throwing structured evidence away."""

        kind_by_memory_type = {
            "identity": "fact",
            "workflow": "fact",
            "style": "preference",
            "instruction": "relationship_protocol",
        }
        for entry in self.user_memory_store.load_entries():
            if entry.status != "active" or not entry.confirmed:
                continue
            kind = kind_by_memory_type.get(entry.type, "fact")
            evidence_ids = tuple(
                item
                for item in (
                    entry.canonical_fact_id,
                    entry.source_message_id,
                    entry.id,
                )
                if item
            )
            self.user_understanding_store.remember(
                UnderstandingAtom.create(
                    kind=kind,  # type: ignore[arg-type]
                    statement=entry.content,
                    status="confirmed",
                    confidence=max(0.95, entry.confidence),
                    evidence_ids=evidence_ids,
                    scope=getattr(entry, "scope", "global") or "global",
                    scope_id=getattr(entry, "scope_id", "") or "",
                    sensitivity="normal",
                ),
            )
        # Historical tasks, prospective intents and working hypotheses must
        # not silently turn an ordinary conversation into a continuation of
        # an older task. They remain inspectable through memory UI/tools;
        # hidden recall is limited to confirmed, currently relevant context.
        projection = self.user_understanding_store.project_for_turn(
            query,
            max_hypotheses=0,
            max_open_loops=0,
        )
        projection = TurnUnderstandingProjection(
            confirmed=projection.confirmed,
            hypotheses=projection.hypotheses,
            open_loops=projection.open_loops,
            association_candidates=self.user_understanding_store.association_palette(
                max_items=12,
            ),
        )
        understanding_prompt = projection.compile_prompt(language)
        if _is_low_information_memory_query(query):
            return UserUnderstandingTurnContext(
                prompt=understanding_prompt,
                projection=projection,
            )
        turn_prompt = build_turn_understanding(
            query,
            projection=projection,
        ).compile_prompt(language)
        return UserUnderstandingTurnContext(
            prompt="\n\n".join(
                part for part in (understanding_prompt, turn_prompt) if part
            ),
            projection=projection,
        )

    def list_memory_tools(self):
        """Return memory tool functions to register with the agent toolkit."""
        return [
            self.memory_search,
            self.layered_memory_search,
            self.conversation_search,
            self.memory_source_open,
            self.memory_store,
            self.memory_list,
            self.memory_forget,
        ]

    @staticmethod
    def _is_cjk(char: str) -> bool:
        """Check if a character is CJK (Chinese/Japanese/Korean)."""
        cp = ord(char)
        return (
            (0x4E00 <= cp <= 0x9FFF)
            or (0x3400 <= cp <= 0x4DBF)  # CJK Unified Ideographs
            or (  # CJK Extension A
                0xF900 <= cp <= 0xFAFF
            )  # CJK Compatibility Ideographs
        )

    def tokenize_query(
        self,
        query: str,
        max_tokens: int = MAX_QUERY_TOKENS,
    ) -> list[str]:
        """Tokenize query: CJK chars as 1-gram, non-CJK split by whitespace.

        Args:
            query: The search query string (non-empty)
            max_tokens: Maximum number of tokens to return

        Returns:
            List of tokens, limited to max_tokens
        """
        tokens = []

        for word in query.split():
            if not word:
                continue

            # Fast path: pure non-CJK word, add directly
            if not any(self._is_cjk(c) for c in word):
                tokens.append(word)
                if len(tokens) >= max_tokens:
                    break
                continue

            # Mixed CJK/non-CJK: iterate chars within the word
            non_cjk_buffer = []
            for char in word:
                if self._is_cjk(char):
                    if non_cjk_buffer:
                        tokens.append("".join(non_cjk_buffer))
                        non_cjk_buffer = []
                    tokens.append(char)
                else:
                    non_cjk_buffer.append(char)

                if len(tokens) >= max_tokens:
                    break

            if non_cjk_buffer and len(tokens) < max_tokens:
                tokens.append("".join(non_cjk_buffer))

            if len(tokens) >= max_tokens:
                break

        return tokens[:max_tokens]

    async def memory_search(
        self,
        query: str,
        max_results: int = 5,
        min_score: float = 0.1,
    ) -> ToolResponse:
        """
        Search MEMORY.md and memory/*.md files semantically.

        Use this tool before answering questions about prior work,
        decisions, dates, people, preferences, or todos. Returns top
        relevant snippets with file paths and line numbers.

        Args:
            query (`str`):
                The semantic search query to find relevant memory snippets.
            max_results (`int`, optional):
                Maximum number of search results to return. Defaults to 5.
            min_score (`float`, optional):
                Minimum similarity score for results. Defaults to 0.1.

        Returns:
            `ToolResponse`:
                Search results formatted with paths, line numbers, and
                content.
        """
        from ...customization.runtime_projection import (
            clamp_memory_recall_limit,
        )

        max_results = clamp_memory_recall_limit(max_results)
        if max_results <= 0:
            return ToolResponse(
                content=[TextBlock(type="text",
                        text="Memory recall is disabled by the active Lantai Profile.",
                    )
                ]
            )
        if self._reme is None:
            return self._fallback_memory_search(
                query=query,
                max_results=max_results,
                min_score=min_score,
                reason=(
                    "semantic_service_unavailable"
                    if self._native_started
                    else "memory_service_not_started"
                ),
            )

        if not getattr(self._reme, "_started", False):
            return self._fallback_memory_search(
                query=query,
                max_results=max_results,
                min_score=min_score,
                reason="semantic_service_not_started",
            )

        try:
            query_final = " ".join(self.tokenize_query(query))
            logger.info(f"Tokenized query: {query_final}")
        except Exception as e:
            logger.exception(f"Failed to tokenize query: {e} query={query}")
            query_final = query

        semantic_response = await self._reme.memory_search(
            query=query_final,
            max_results=max_results,
            min_score=min_score,
        )
        if self._memory_response_has_hits(semantic_response):
            return semantic_response
        return self._fallback_memory_search(
            query=query,
            max_results=max_results,
            min_score=min_score,
            reason="semantic_search_empty",
        )

    @staticmethod
    def _memory_response_has_hits(response: ToolResponse) -> bool:
        """Reject empty/placeholder semantic responses before surfacing them."""

        if not isinstance(response, ToolResponse):
            # Third-party backends may return a compatible response object.
            # Only rewrite a response whose empty content can be established.
            return True
        empty_markers = {
            "",
            "[]",
            "{}",
            "null",
            "none",
            "no results",
            "no memory found",
            "no related memory found",
        }
        for block in getattr(response, "content", ()) or ():
            text = (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", None)
            )
            normalized = str(text or "").strip().casefold().rstrip(".!\u3002")
            if normalized and normalized not in empty_markers:
                try:
                    decoded = json.loads(normalized)
                except (TypeError, ValueError):
                    return True
                if decoded not in (None, [], {}):
                    return True
        return False

    def _fallback_memory_search(
        self,
        *,
        query: str,
        max_results: int,
        min_score: float,
        reason: str,
    ) -> ToolResponse:
        """Search readable files, then layered atoms, with truthful provenance."""

        local = self._search_local_memory_files(
            query=query,
            max_results=max_results,
            min_score=min_score,
        )
        if self._memory_response_has_hits(local):
            local.metadata = {
                **(getattr(local, "metadata", None) or {}),
                "memory_backend": "local_files",
                "fallback_reason": reason,
            }
            return local

        layered = self.layered_memory.search_atoms(
            query=query,
            max_results=max_results,
        )
        if layered:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=json.dumps(layered, ensure_ascii=False, indent=2),
                    ),
                ],
                metadata={
                    "memory_backend": "layered_atoms",
                    "fallback_reason": reason,
                },
            )
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=(
                        "No matching memory was found across semantic, local-file, "
                        "or layered-memory sources."
                    ),
                ),
            ],
            metadata={
                "memory_backend": "none",
                "fallback_reason": reason,
            },
        )

    def _search_local_memory_files(
        self,
        *,
        query: str,
        max_results: int,
        min_score: float,
    ) -> ToolResponse:
        """Search human-readable memory files without the legacy runtime."""

        root = Path(self.working_dir)
        memory_dir = root / "memory"
        paths = [root / "MEMORY.md"]
        if memory_dir.is_dir():
            paths.extend(sorted(memory_dir.rglob("*.md")))

        tokens = [
            token.casefold()
            for token in self.tokenize_query(query)
            if token.strip()
        ]
        phrase = query.strip().casefold()
        if not tokens and not phrase:
            return ToolResponse(content=[TextBlock(type="text", text="")])

        limit = min(max(int(max_results), 1), 100)
        threshold = min(max(float(min_score), 0.0), 1.0)
        matches: list[tuple[float, str, int, str]] = []

        for path in paths[:_LOCAL_MEMORY_FILE_LIMIT]:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size > _LOCAL_MEMORY_FILE_MAX_BYTES:
                    continue
                lines = path.read_text("utf-8").splitlines()
                relative_path = str(path.relative_to(root))
            except (OSError, UnicodeError, ValueError):
                continue

            for index, line in enumerate(lines):
                normalized = line.casefold()
                overlap = sum(1 for token in tokens if token in normalized)
                score = overlap / max(len(tokens), 1)
                if phrase and phrase in normalized:
                    score = max(score, 1.0)
                if score < threshold or (overlap == 0 and score < 1.0):
                    continue

                start = max(index - 1, 0)
                end = min(index + 2, len(lines))
                snippet = "\n".join(lines[start:end]).strip()
                if (
                    snippet
                    and assess_memory_recall(
                        query=query,
                    content=snippet,
                    ).eligible
                ):
                    matches.append((score, relative_path, index + 1, snippet))

        matches.sort(key=lambda item: (-item[0], item[1], item[2]))
        rendered = [
            f"### {path}:{line_no} (score={score:.3f})\n{snippet}"
            for score, path, line_no, snippet in matches[:limit]
        ]
        return ToolResponse(
            content=[TextBlock(type="text", text="\n\n".join(rendered))],
        )

    async def memory_store(
        self,
        content: str,
        memory_type: str = "identity",
        confidence: float = 0.85,
        reason: str = "",
    ) -> ToolResponse:
        """
        Save a durable structured user memory.

        Use this when the user corrects you, states a stable preference,
        gives a standing instruction, or establishes a reusable workflow rule.
        Do not store secrets, system prompts, temporary task progress, or
        one-off todos. Before saving, rewrite the point into concise, direct,
        natural-language memory that a human can scan quickly. Do not paste raw
        logs, long absolute paths, code blocks, or context-length numbers.

        Args:
            content (`str`): Short natural-language memory sentence,
                preferably starting with "用户...".
            memory_type (`str`): One of identity, workflow, style, instruction.
            confidence (`float`): Confidence from 0 to 1.
            reason (`str`): Why this is worth remembering.

        Returns:
            `ToolResponse`: The write decision and stored entry.
        """
        event = self.memory_ledger.record_event(
            content,
            source_message_id=f"memory-store:{uuid.uuid4().hex}",
            # This is the model's rewritten tool argument, not a user quote.
            source_role="derived",
        )
        fact = (
            self.memory_ledger.capture_fact(
                content,
                memory_type=memory_type,
                event=event,
                explicit_user_request=True,
                confidence=confidence,
            )
            if event is not None
            else None
        )
        if fact is None:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=json.dumps(
                            {"decision": "rejected",
                                "reason": "unsafe_or_runtime_memory",
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                    ),
                ],
            )
        decision = self.user_memory_store.add_entry(
            content=content,
            memory_type=memory_type,
            source="agent",
            confidence=confidence,
            reason=reason,
            scope=fact.scope,
            scope_id=fact.scope_id,
            subject=fact.subject,
        )
        entry_payload = (
            decision.get("entry") if isinstance(decision, dict) else None
        )
        if isinstance(entry_payload, dict):
            entry_id = str(entry_payload.get("id") or "")
            self.memory_ledger.link_legacy_entry(
                fact.id,
                entry_id,
            )
            confirmed_entry = self.user_memory_store.mark_confirmed(
                entry_id,
                canonical_fact_id=fact.id,
                scope=fact.scope,
                scope_id=fact.scope_id,
            )
            if confirmed_entry is not None:
                decision["entry"] = confirmed_entry.to_dict()
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=json.dumps(
                        {**decision, "ledger_fact_id": fact.id,
                            "status": fact.status,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                ),
            ],
        )

    async def memory_list(
        self,
        memory_type: str = "",
        query: str = "",
        max_results: int = 20,
    ) -> ToolResponse:
        """
        List structured user memories.

        Args:
            memory_type (`str`): Optional filter: identity, workflow, style,
                instruction.
            query (`str`): Optional relevance query.
            max_results (`int`): Maximum memories to return.

        Returns:
            `ToolResponse`: Structured memory entries.
        """
        if query.strip():
            entries = self.user_memory_store.search_relevant(
                query,
                max_results=max_results,
            )
        else:
            entries = [
                item
                for item in self.user_memory_store.load_entries()
                if item.status == "active"
            ][:max_results]

        if memory_type in {"identity", "workflow", "style", "instruction"}:
            entries = [item for item in entries if item.type == memory_type]

        text = json.dumps(
            [entry.to_dict() for entry in entries[:max_results]],
            ensure_ascii=False,
            indent=2,
        )
        return ToolResponse(content=[TextBlock(type="text", text=text)])

    async def memory_forget(self, entry_id: str) -> ToolResponse:
        """
        Archive a structured user memory by id.

        Use this only when the user says a memory is wrong, obsolete, or should
        be forgotten.

        Args:
            entry_id (`str`): The structured memory id.

        Returns:
            `ToolResponse`: Archive result.
        """
        existing = next(
            (item for item in self.user_memory_store.load_entries() if item.id == entry_id),
            None,
        )
        linked_fact_ids = [
            fact.id for fact in self.memory_ledger.list_facts()
            if entry_id in fact.legacy_entry_ids
        ]
        entry = self.user_memory_store.archive_entry(entry_id)
        forgotten_facts = (
            self.memory_ledger.forget_matching_content(
                entry.content,
                legacy_entry_id=entry.id,
            )
            if entry is not None
            else 0
        )
        forgotten_projections = (
            self.layered_memory.forget_projection_content(entry.content)
            if entry is not None
            else {"atoms": 0, "tombstones": 0}
        )
        if existing is not None:
            understanding_store = getattr(self, "user_understanding_store", None)
            if understanding_store is not None:
                understanding_store.invalidate_source(entry_id)
                if existing.canonical_fact_id:
                    understanding_store.invalidate_source(
                        existing.canonical_fact_id,
                    )
                for fact_id in linked_fact_ids:
                    understanding_store.invalidate_source(fact_id)
        payload = (
            {
                "archived": True,
                "entry": entry.to_dict(),
                "ledger_facts_forgotten": forgotten_facts,
                "layered_projections_forgotten": forgotten_projections,
            }
            if entry is not None
            else {"archived": False, "reason": "not_found"}
        )
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                ),
            ],
        )

    async def summarize(self, messages: list[Any], **_kwargs) -> str:
        """Generate a summary of the given messages and persist to memory."""
        try:
            if self._reme is None:
                return ""

            agent_config = load_agent_config(self.agent_id)
            light_ctx = agent_config.running.light_context_config
            cc = light_ctx.context_compact_config
            set_current_workspace_dir(Path(self.working_dir))
            pruning_cfg = light_ctx.tool_result_pruning_config
            recent_max_bytes = pruning_cfg.pruning_recent_msg_max_bytes
            set_current_recent_max_bytes(recent_max_bytes)
            chat_model, formatter = self._get_summary_model_and_formatter()

            return await self._reme.summary_memory(
                messages=messages,
                as_llm=chat_model,
                as_llm_formatter=formatter,
                as_token_counter=get_token_counter(agent_config),
                toolkit=None,
                language=agent_config.language,
                max_input_length=agent_config.running.max_input_length,
                compact_ratio=cc.compact_threshold_ratio,
                timezone=load_config().user_timezone or None,
                add_thinking_block=cc.compact_with_thinking_block,
            )
        finally:
            try:
                await self.extract_structured_user_memory(messages)
            except Exception:
                logger.warning(
                    "structured user memory extraction failed",
                    exc_info=True,
                )
            try:
                await self.capture_turn(messages, source="summarize")
            except Exception:
                logger.warning("layered memory capture failed", exc_info=True)

    async def extract_structured_user_memory(
        self,
        messages: list[Any],
    ) -> list[dict]:
        """Extract and persist structured user memories from messages."""
        candidates = await extract_structured_user_memory(
            agent_id=self.agent_id,
            messages=messages,
            store=self.user_memory_store,
        )
        # Inferred candidates are recorded as pending by ``capture_turn`` once
        # their L0 evidence ids exist. They must not enter the stable profile
        # until an explicit user request confirms them.
        return [
            {
                "decision": "pending",
                "content": candidate.content,
                "type": candidate.type,
            }
            for candidate in candidates
        ]

    async def retrieve(
        self,
        messages: list[Any] | Any,
        agent_name: str = "",
        **_kwargs,
    ) -> dict | None:
        """Run optional automatic recall away from the foreground loop.

        The retrieval implementation is deliberately local, but it performs
        synchronous JSON/SQLite work.  Keeping that work inside an ``async``
        coroutine used to block the event loop, which made an otherwise
        ordinary chat appear frozen.  A worker thread also makes the caller's
        fail-open timeout effective.
        """

        return await asyncio.to_thread(
            self._retrieve_sync,
            messages,
            agent_name,
        )

    def _retrieve_sync(
        self,
        messages: list[Any] | Any,
        agent_name: str = "",
    ) -> dict | None:
        """Retrieve relevant memory and return updated kwargs dict.

        Args:
            messages: One or more conversation messages used as the query.
            agent_name: Agent name for constructing compatibility messages.

        Returns:
            None: No relevant memory found, caller should not update kwargs.
            dict: {"msg": msgs + [assistant_msg, tool_result_msg]} to merge
                with kwargs via {**kwargs, **result}.
        """
        if not self.memory_ledger.get_preferences()["runtime_enabled"]:
            return None
        msgs: list[Any] = (
            list(messages)
            if isinstance(messages, (list, tuple))
            else [messages]
        )

        # Build query from the newest messages, preserving tail.
        query_parts: list[str] = []
        total = 0
        for msg in reversed(msgs):
            remaining = 100 - total
            if remaining <= 0:
                break

            text = (msg.get_text_content() or "").strip()
            if not text:
                continue

            chunk = text[:remaining]
            query_parts.insert(0, chunk)
            total += len(chunk)

        query = " ".join(query_parts).strip()
        if _is_low_information_memory_query(query):
            return None

        agent_config = load_agent_config(self.agent_id)
        reme_cfg = agent_config.running.reme_light_memory_config
        ms = reme_cfg.auto_memory_search_config
        from ...customization.runtime_projection import (
            clamp_memory_recall_limit,
        )

        max_results = clamp_memory_recall_limit(ms.max_results)
        if max_results <= 0:
            return None
        min_score = ms.min_score

        try:
            layered_cfg = reme_cfg.layered_memory_config
            recall_plan = build_memory_recall_plan(query, max_results=max_results)
            # Automatic recall is part of the model's hidden input, so its
            # authority must be stricter than an explicit ``memory_search``
            # tool call.  Raw L0 conversations, assistant replies and derived
            # System2 "open intentions" are useful evidence when the model
            # deliberately searches history, but silently injecting them on
            # every turn can resurrect an obsolete task or an assistant's own
            # promise as if it were a current user instruction.
            recall_plan.include_l0 = False
            recall_plan.include_system2 = False
            structured_context = ""
            layered_context = ""
            if _cfg_bool(getattr(layered_cfg, "enabled", True), True):
                layered_max_results = _cfg_int(
                    getattr(layered_cfg, "max_recall_results", max_results),
                    max_results,
                )
                layered_max_chars = _cfg_int(
                    getattr(layered_cfg, "max_context_chars", 5200),
                    5200,
                )
                if _cfg_bool(
                    getattr(layered_cfg, "hybrid_recall_enabled", True),
                    True,
                ):
                    layered_context = (
                        self._format_unified_memory_recall_context(
                            query=query,
                        max_results=layered_max_results,
                            max_chars=layered_max_chars,
                        )
                    )
                    layered_context = self._truncate_to_token_budget(
                        layered_context,
                        getattr(layered_cfg, "max_context_tokens", None),
                    )
                else:
                    structured_context = (
                        self.user_memory_store.format_retrieval_context(
                        query=query,
                        max_results=max_results,
                            recall_plan=recall_plan,
                        )
                    )
                    layered_context = (
                        self.layered_memory.format_recall_context(
                        query=query,
                        max_results=layered_max_results,
                            max_chars=layered_max_chars,
                        )
                    )
            else:
                structured_context = (
                    self.user_memory_store.format_retrieval_context(
                    query=query,
                    max_results=max_results,
                        recall_plan=recall_plan,
                    )
                )

            text_parts = [
                part
                for part in [structured_context, layered_context]
                if part and part.strip()
            ]
            text_content = "\n\n".join(text_parts)

            if not text_content:
                return None

            # Construct compatibility assistant/tool-result messages.
            _id = uuid.uuid4().hex
            tool_use_input = {
                "query": query,
                "max_results": max_results,
                "min_score": min_score,
            }

            assistant_msg = legacy_message(
                name=agent_name,
                role="assistant",
                content=[
                    ToolUseBlock(
                        type="tool_use",
                        id=_id,
                        name="memory_search",
                        input=tool_use_input,
                        raw_input=json.dumps(
                            tool_use_input,
                            ensure_ascii=False,
                        ),
                    ),
                ],
            )

            tool_result_msg = legacy_message(
                name=agent_name,
                role="system",
                content=[
                    ToolResultBlock(
                        type="tool_result",
                        id=_id,
                        name="memory_search",
                        output=[TextBlock(type="text", text=text_content)],
                    ),
                ],
            )

            return {"_memory_hints": [assistant_msg, tool_result_msg]}

        except Exception as e:
            logger.exception(f"memory_search failed: {e}")
            return None

    def _current_memory_retrieval_source_signature(
        self,
    ) -> tuple[tuple[bool, int, int], ...]:
        """Return a cheap revision key for authoritative recall sources."""

        signature: list[tuple[bool, int, int]] = []
        for path in (
            self.memory_ledger.facts_path,
            self.user_memory_store.entries_path,
        ):
            try:
                stat = path.stat()
            except FileNotFoundError:
                signature.append((False, 0, 0))
            except OSError:
                # Fail open to a rebuild when source metadata is unreadable.
                signature.append((True, -1, -1))
            else:
                signature.append((True, stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def _build_memory_retrieval_index(self) -> HybridRetrievalRuntime:
        """Incrementally refresh Memory lexical and semantic projections."""

        signature = self._current_memory_retrieval_source_signature()
        cached_index = getattr(self, "_memory_retrieval_index", None)
        cached_signature = getattr(
            self,
            "_memory_retrieval_source_signature",
            None,
        )
        if cached_index is not None and cached_signature == signature:
            return cached_index

        documents: list[IndexedDocument] = []
        for fact in self.memory_ledger.confirmed_facts():
            if fact.scope == "thread":
                continue
            documents.append(
                IndexedDocument(
                    source_id=f"memory:{fact.id}", source_kind="memory",
                    title=fact.memory_type, content=fact.content,
                    citation=f"memory-ledger/facts.json#{fact.id}",
                    revision=fact.updated_at, trust=0.95,
                ),
            )
        for entry in self.user_memory_store.load_entries():
            if entry.status != "active" or not entry.confirmed:
                continue
            if getattr(entry, "scope", "global") == "thread":
                continue
            documents.append(
                IndexedDocument(
                    source_id=f"structured:{entry.id}", source_kind="memory",
                    title=entry.type, content=entry.content,
                    citation=f"structured-user-memory/entries.json#{entry.id}",
                    revision=entry.updated_at, trust=0.70,
                ),
            )
        index = cached_index or HybridRetrievalRuntime(
            Path(self.working_dir),
            embedding=getattr(self, "_embedding_provider", None),
            lexical_filename=_MEMORY_RETRIEVAL_INDEX_FILE,
            vector_filename="memory-retrieval-vectors.sqlite",
        )
        changes = index.sync(
            documents,
            source_prefixes=("memory:", "structured:",
                "atom:",
                "system2:",
                "l0:",
            ),
        )
        if any(int(value) for value in changes.values()):
            self._retrieval_health_cache = None
        self._memory_retrieval_index = index
        self._memory_retrieval_source_signature = signature
        return index

    @staticmethod
    def _truncate_to_token_budget(text: str, token_budget: Any) -> str:
        """Apply a conservative token cap while retaining legacy char caps."""

        try:
            budget = int(token_budget)
        except (TypeError, ValueError):
            return text
        if budget <= 0 or not text:
            return text
        from ..context.context_budget import ModelAwareTokenCounter

        counter = ModelAwareTokenCounter()
        if counter.count(text).upper_bound <= budget:
            return text
        suffix = "\n[truncated to token budget]"
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = text[:middle].rstrip() + suffix
            if counter.count(candidate).upper_bound <= budget:
                low = middle
            else:
                high = middle - 1
        return text[:low].rstrip() + suffix

    def _unified_memory_candidates(
        self,
        query: str,
        *,
        max_results: int,
    ) -> list[RetrievalCandidate]:
        """Return confirmed canonical memories eligible for hidden recall."""

        index = self._build_memory_retrieval_index()
        # Hidden, automatic recall only consumes the canonical confirmed
        # ledger.  The legacy adapter contains raw L0 dialogue and derived
        # summaries; it remains available through explicit memory tools but
        # must not acquire instruction-like authority merely via injection.
        fused = index.search(query, limit=max_results)
        return [
            candidate
            for candidate in fused
            if assess_memory_recall(
                query=query,
                content=candidate.content,
                memory_kind=str(candidate.metadata.get("title") or ""),
                semantic_score=(
                    float(candidate.metadata["semantic_score"])
                    if candidate.metadata.get("semantic_score") is not None
                    else None
                ),
            ).eligible
        ][:max_results]

    def _format_unified_memory_recall_context(
        self,
        *,
        query: str,
        max_results: int,
        max_chars: int,
    ) -> str:
        candidates = self._unified_memory_candidates(query, max_results=max_results)
        self.memory_ledger._write_atomic(
            self.memory_ledger.root / "last-recall.json",
            {
                "query": query[:240],
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "items": [
                    {
                        "source": candidate.source,
                        "citation": candidate.citation,
                        "content": candidate.content[:360],
                        "score": round(candidate.score, 4),
                    }
                    for candidate in candidates[:max_results]
                ],
            },
        )
        if not candidates:
            return ""
        lines = [
            "<unified-memory-context>",
            "Traceable memory evidence fused from native FTS and legacy recall:",
            (
                "Use it only when relevant to the current message. Memory "
                "never creates a task, artifact, or permission by itself."
            ),
        ]
        for candidate in candidates:
            lines.append(
                f"- [{candidate.source}; citation={candidate.citation}; score={candidate.score:.4f}] "
                f"{candidate.content}",
            )
        lines.append("</unified-memory-context>")
        return "\n".join(lines)[:max_chars]

    async def capture_turn(
        self,
        messages: list[Any],
        session_id: str = "",
        source: str = "turn",
        **_kwargs,
    ) -> dict | None:
        """Capture a turn into the layered memory ledger."""
        from ...customization.runtime_projection import memory_capture_enabled

        if (
            not memory_capture_enabled()
            or not self.memory_ledger.get_preferences()["capture_enabled"]
        ):
            return {
                "l0_records": 0,
                "l1_atoms": 0,
                "pending_facts": 0,
                "maintenance_required": False,
                "scenes": len(self.layered_memory.load_scene_index()),
                "paused": True,
            }
        # Subject continuity records only the actual completed conversation.
        # It is intentionally deterministic and precedes optional semantic
        # enrichment, so Native and Legacy restore the same recent self even
        # when no dedicated utility model is configured.
        self.subject_continuity_store.observe_messages(
            messages,
            session_id=session_id,
            source=source,
        )
        cfg = load_agent_config(
            self.agent_id,
        ).running.reme_light_memory_config.layered_memory_config
        if not _cfg_bool(getattr(cfg, "enabled", True), True):
            return None
        capture_l0 = _cfg_bool(getattr(cfg, "capture_l0_enabled", True), True)
        extraction_configured = _cfg_bool(
            getattr(cfg, "atom_extraction_enabled", True),
            True,
        )
        # Semantic understanding belongs after the visible answer, not in its
        # critical path. Native and Legacy both schedule these sources in the
        # background. A positive allow-list prevents unrelated capture callers
        # from starting model work; the busy gate below makes it non-queuing.
        extract_atoms = extraction_configured and source in {
            "manual_reindex",
            "native_post_reply",
            "post_reply",
        }
        if not capture_l0 and not extract_atoms:
            return None

        # Keep the critical section local and deterministic.  Previously the
        # outer capture lock also covered up to 120 seconds of utility-model
        # work, so every later turn accumulated behind it.  Raw evidence must
        # be durable promptly, while semantic enrichment is best-effort and
        # protected separately by ``_atom_extraction_busy`` below.
        async with self._capture_turn_lock:
            if not self._accept_background_capture:
                return None
            records = (
                self.layered_memory.record_messages(
                    messages=messages,
                    session_id=session_id,
                    source=source,
                )
                if capture_l0
                else []
            )
            ledger_events = self._record_ledger_events(records)
        atoms = []
        understanding_count = 0
        if extract_atoms:
            if self._atom_extraction_busy:
                logger.debug("layered L1 extraction queued behind active work")
                while self._atom_extraction_busy:
                    await asyncio.sleep(0.01)
            self._atom_extraction_busy = True
        if extract_atoms:
            try:
                # Long-term memory may be semantically enriched by the
                # dedicated utility model. The foreground chat model is never
                # borrowed for automatic post-reply analysis.
                llm_enabled = True
                max_atoms = getattr(cfg, "max_atoms_per_turn", 10)
                max_atoms = max_atoms if isinstance(max_atoms, int) else 10
                timeout_seconds = getattr(cfg, "extraction_timeout_seconds", 20.0)
                timeout_seconds = (
                    timeout_seconds
                    if isinstance(timeout_seconds, (int, float))
                    else 20.0
                )
                # Semantic memory runs after the visible reply, so it can use
                # a deliberately generous budget.  Keep the configured value
                # as a per-attempt floor while reserving enough time for the
                # primary fallback to finish even after a slow utility model.
                semantic_budget_seconds = max(
                    float(timeout_seconds) * 2.0,
                    120.0,
                )
                user_records = [
                    record
                    for record in records
                    if str(getattr(record, "role", "") or "") == "user"
                ]
                user_texts = [
                    str(getattr(record, "content", "") or "")
                    for record in user_records
                    if str(getattr(record, "content", "") or "").strip()
                ]
                source_ids = [
                    (
                        f"{session_id}:{getattr(record, 'id', '')}"
                        if session_id
                        else str(getattr(record, "id", "") or "")
                    )
                    for record in user_records
                    if str(getattr(record, "id", "") or "")
                ]

                async def extract_durable_atoms() -> list[Any]:
                    try:
                        return await asyncio.wait_for(
                            extract_llm_atom_candidates(
                                agent_id=self.agent_id,
                                messages=messages,
                                existing_atoms=self.layered_memory.load_atoms(),
                                max_atoms=max_atoms,
                                timeout_seconds=semantic_budget_seconds,
                                records=records,
                            ),
                            timeout=semantic_budget_seconds + 1.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("layered L1 extraction timed out")
                    except Exception:
                        logger.warning("layered L1 extraction failed", exc_info=True)
                    return []

                async def extract_current_understanding() -> list[Any]:
                    if not user_texts or not source_ids:
                        return []
                    try:
                        return await asyncio.wait_for(
                            extract_user_understanding_candidates(
                                agent_id=self.agent_id,
                                user_texts=user_texts,
                                source_ids=source_ids,
                                timeout_seconds=semantic_budget_seconds,
                            ),
                            timeout=semantic_budget_seconds + 1.0,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("user understanding extraction timed out")
                    except Exception:
                        logger.warning(
                            "user understanding extraction failed",
                            exc_info=True,
                        )
                    return []

                llm_candidates: list[Any] = []
                understanding_candidates: list[Any] = []
                if llm_enabled:
                    # Durable facts and time-bounded context share the same
                    # completed turn but have independent schemas.  Run them
                    # together so both receive a real utility→primary fallback
                    # budget without doubling post-reply latency.
                    (
                        llm_candidates,
                        understanding_candidates,
                    ) = await asyncio.gather(
                        extract_durable_atoms(),
                        extract_current_understanding(),
                    )
                if llm_candidates:
                    corrections = [
                        {
                            "old_content": fact.content,
                            "state": fact.status,
                            "reason": fact.rejection_reason,
                            "replacement": (
                                self.memory_ledger.get_fact(fact.superseded_by_id).content
                                if fact.superseded_by_id and self.memory_ledger.get_fact(fact.superseded_by_id)
                                else ""
                            ),
                        }
                        for fact in self.memory_ledger.list_facts(
                            statuses={"forgotten", "rejected", "superseded"},
                            limit=30,
                        )
                    ]
                    correction_version = self.memory_ledger.correction_fingerprint()
                    llm_candidates = await review_atom_candidates(
                        agent_id=self.agent_id,
                        candidates=llm_candidates,
                        records=records,
                        corrections=corrections,
                        timeout_seconds=semantic_budget_seconds,
                    )
                    if self.memory_ledger.correction_fingerprint() != correction_version:
                        # A user decision landed while model review ran.
                        # Reprocess L0 later instead of committing stale atoms.
                        llm_candidates = []
                if llm_candidates:
                    atoms = self.layered_memory.ingest_atom_candidates(
                        llm_candidates,
                        records=records,
                        detect_conflicts=_cfg_bool(
                            getattr(cfg, "conflict_detection_enabled", True),
                            True,
                        ),
                    )
                else:
                    # Raw L0 evidence is durable and can be reprocessed later.
                    # Never promote deterministic sentence splitting into
                    # user memory when no model produced a semantic result.
                    atoms = []
                for candidate in understanding_candidates:
                    self.user_understanding_store.ingest_candidate(candidate)
                understanding_count = len(understanding_candidates)
            finally:
                self._atom_extraction_busy = False

        refresh_scenes = _cfg_bool(getattr(cfg, "scene_enabled", True), True)
        refresh_persona = _cfg_bool(getattr(cfg, "persona_enabled", True), True)
        if atoms and (refresh_scenes or refresh_persona):
            self.layered_memory.refresh_scenes_and_persona(
                refresh_scenes=refresh_scenes,
                refresh_persona=refresh_persona,
            )
        pending_facts = self._record_pending_ledger_facts(atoms, ledger_events)
        result = {
            "l0_records": len(records),
            "l1_atoms": len(atoms),
            "pending_facts": pending_facts,
            "understanding_atoms": understanding_count,
            "maintenance_required": bool(atoms),
            "scenes": len(self.layered_memory.load_scene_index()),
        }
        logger.debug("layered memory capture result: %s", result)
        return result

    def _record_ledger_events(
        self,
        records: list[Any],
    ) -> dict[str, MemoryEvent]:
        """Mirror eligible L0 records into the canonical evidence log once."""

        events: dict[str, MemoryEvent] = {}
        for record in records:
            role = str(getattr(record, "role", "") or "")
            metadata = getattr(record, "metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            is_tool_result = metadata.get("memory_source") == "tool_result"
            if role != "user" and not is_tool_result:
                continue
            event = self.memory_ledger.record_event(
                record.content,
                source_message_id=record.source_message_id or record.id,
                session_id=str(getattr(record, "session_id", "") or ""),
                source_role="tool_result" if is_tool_result else "user",
                source_created_at=str(getattr(record, "source_created_at", "") or ""),
                event_id=f"event_{record.id}",
            )
            if event is not None:
                events[record.id] = event
        return events

    def _record_pending_ledger_facts(
        self,
        atoms: list[Any],
        events: dict[str, MemoryEvent],
    ) -> int:
        """Persist inferred durable atoms as non-injectable pending facts."""

        if not events:
            return 0
        count = 0
        for atom in atoms:
            support_status = str(
                (getattr(atom, "metadata", {}) or {}).get("support_status")
                or "unreviewed"
            )
            if support_status == "unsupported":
                continue
            atom_type = str(getattr(atom, "type", ""))
            if atom_type not in {"identity", "workflow", "style", "instruction"}:
                continue
            metadata = getattr(atom, "metadata", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            source_kind = str(metadata.get("source_kind") or "")
            preferred_role = (
                "tool_result"
                if source_kind == "user_provided_material"
                else "user"
            )
            eligible_events = {
                record_id: candidate
                for record_id, candidate in events.items()
                if candidate.source_role == preferred_role or (
                    preferred_role == "user" and candidate.source_role == "unknown"
                )
            }
            source_ids = list(dict.fromkeys(
                getattr(atom, "source_record_ids", []) or []
            ))
            if source_ids:
                # An upserted atom may cite earlier turns. A review spanning
                # those turns must bind every member of that review group.
                previous_ids = {
                    f"event_{record_id}" for record_id in source_ids
                    if record_id not in events
                }
                previous_events = {
                    event.id: event for event in self.memory_ledger.list_events(
                        event_ids=previous_ids,
                    )
                } if previous_ids else {}
                if any(
                    record_id not in eligible_events and not (
                        f"event_{record_id}" in previous_events and
                        previous_events[f"event_{record_id}"].source_role == preferred_role
                    )
                    for record_id in source_ids
                ):
                    continue
                available_events = {
                    **{event_id.removeprefix("event_"): event
                       for event_id, event in previous_events.items()},
                    **eligible_events,
                }
                current_ids = [record_id for record_id in source_ids if record_id in eligible_events]
            else:
                # Older callers do not carry a source ID. One eligible source
                # is unambiguous; several are not, so never guess the last.
                current_ids = [""] if len(eligible_events) == 1 else []
                available_events = {"": next(iter(eligible_events.values()))} if current_ids else {}
            if not current_ids:
                continue
            evidence_quote = str(metadata.get("evidence_quote") or "")
            evidence_quotes = metadata.get("evidence_quotes")
            evidence_quotes = evidence_quotes if isinstance(evidence_quotes, dict) else {}
            reviewed_ids = metadata.get("support_source_record_ids")
            reviewed_ids = set(reviewed_ids) if isinstance(reviewed_ids, list) else set()
            raw_groups = metadata.get("support_review_groups")
            review_groups = [
                list(dict.fromkeys(group)) for group in raw_groups
                if isinstance(group, list) and group
                and all(isinstance(record_id, str) and (
                    record_id in available_events
                ) for record_id in group)
                and any(record_id in current_ids for record_id in group)
            ] if isinstance(raw_groups, list) else []
            if not review_groups and support_status == "supported":
                # Older stored atoms have reviewed IDs but no group metadata.
                fallback = [record_id for record_id in current_ids if record_id in reviewed_ids]
                if fallback:
                    review_groups = [fallback]
            covered = {record_id for group in review_groups for record_id in group}
            source_groups = [
                (group, True) for group in review_groups
            ] + [([record_id], False) for record_id in current_ids if record_id not in covered]
            policy_result = None
            admission_decision = str(
                metadata.get("admission_decision") or "",
            )
            if admission_decision in {"suggest", "confirm"}:
                policy_result = MemoryPolicyResult(
                    decision=admission_decision,
                    scope=str(metadata.get("scope") or "global"),
                    subject=str(metadata.get("subject") or "user"),
                    utility_score=float(metadata.get("utility_score") or 0.88),
                    reason=str(
                        metadata.get("decision_reason")
                        or "用户直接陈述的高置信核心事实"
                    ),
                    sensitivity=str(metadata.get("sensitivity") or "normal"),
                )
            fact = None
            for group, reviewed in source_groups:
                source_bindings = [
                    (record_id, available_events[record_id])
                    for record_id in group
                ]
                group_supported = reviewed and support_status == "supported" and all(
                    record_id in reviewed_ids for record_id in group
                )
                support_group = ""
                if len(group) > 1:
                    # Independent reviews have separate groups. A joint
                    # review is invalidated when any member is removed.
                    source_set = sorted(event.id for _, event in source_bindings)
                    support_group = "joint:" + hashlib.sha256(
                        json.dumps(source_set, ensure_ascii=False).encode("utf-8")
                    ).hexdigest()[:20]
                capture_policy = policy_result
                should_confirm_joint = False
                if support_group:
                    base_policy = policy_result or self.memory_ledger.policy.evaluate(
                        str(getattr(atom, "content", "")), memory_type=atom_type,
                        explicit_user_request=False,
                    )
                    should_confirm_joint = base_policy.decision == "confirm"
                    if should_confirm_joint:
                        # A joint claim remains pending until all originals
                        # and reviewed excerpts reach the ledger.
                        capture_policy = MemoryPolicyResult(
                            decision="suggest", scope=base_policy.scope,
                            subject=base_policy.subject,
                            utility_score=base_policy.utility_score,
                            reason=base_policy.reason,
                            sensitivity=base_policy.sensitivity,
                        )
                all_sources_captured = True
                for record_id, event in source_bindings:
                    quote = evidence_quotes.get(record_id, evidence_quote)
                    quote = quote if isinstance(quote, str) and quote in event.content else ""
                    captured = self.memory_ledger.capture_fact(
                        str(getattr(atom, "content", "")),
                        memory_type=atom_type,
                        event=event,
                        explicit_user_request=False,
                        confidence=float(getattr(atom, "confidence", 0.6)),
                        policy_result=capture_policy,
                        atom_id=str(getattr(atom, "id", "") or ""),
                        evidence_quote=quote,
                        support_status="supported" if group_supported else "unreviewed",
                        support_reason=str(metadata.get("support_reason") or ""),
                        support_model=str(metadata.get("support_model") or ""),
                        support_reviewed_at=str(metadata.get("support_reviewed_at") or ""),
                        support_group=support_group,
                        scope_id=str(metadata.get("scope_id") or (
                            getattr(atom, "scene_name", "") if metadata.get("scope") == "project"
                            else event.session_id if metadata.get("scope") == "thread" and metadata.get("temporal_scope") in {"current", "episodic"} else ""
                        )),
                    )
                    all_sources_captured = all_sources_captured and (
                        captured is not None and event.id in captured.event_ids
                    )
                    if captured is not None:
                        fact = captured
                if (
                    fact is not None and should_confirm_joint
                    and all_sources_captured and group_supported
                ):
                    confirmed = self.memory_ledger.confirm(
                        fact.id,
                        required_event_ids={event.id for _, event in source_bindings},
                    )
                    if confirmed is not None:
                        fact = confirmed
            if fact is not None:
                if fact.status == "confirmed" and not fact.legacy_entry_ids:
                    projection = self.user_memory_store.add_entry(
                        content=fact.content,
                        memory_type=fact.memory_type,
                        source="agent",
                        confidence=max(fact.confidence, 0.95),
                        reason=fact.decision_reason or "用户明确要求记住",
                        evidence=f"canonical:{fact.id}",
                        scope=fact.scope,
                        scope_id=fact.scope_id,
                        subject=fact.subject,
                    )
                    entry_payload = (
                        projection.get("entry")
                        if isinstance(projection, dict)
                        else None
                    )
                    if isinstance(entry_payload, dict):
                        entry_id = str(entry_payload.get("id") or "")
                        self.memory_ledger.link_legacy_entry(fact.id, entry_id)
                        self.user_memory_store.mark_confirmed(
                            entry_id,
                            canonical_fact_id=fact.id,
                            scope=fact.scope,
                            scope_id=fact.scope_id,
                        )
                atom_status = {
                    "observed": "observed",
                    "pending": "pending",
                    "confirmed": "active",
                    "conflict": "conflict",
                }.get(fact.status)
                if atom_status and hasattr(self, "layered_memory"):
                    self.layered_memory.set_atom_status(
                        [str(getattr(atom, "id", "") or "")],
                        atom_status,
                    )
            count += int(fact is not None and fact.status in {"pending", "conflict"})
        return count

    async def run_system2_consolidation(
        self,
        reason: str = "post_reply",
    ) -> dict | None:
        """Run the non-blocking System2 memory consolidation pass."""

        if self._system2_busy:
            return {"status": "deferred_backpressure"}
        cfg = load_agent_config(
            self.agent_id,
        ).running.reme_light_memory_config.layered_memory_config
        if not _cfg_bool(getattr(cfg, "enabled", True), True):
            return None
        if not _cfg_bool(getattr(cfg, "system2_enabled", True), True):
            return None
        max_atoms = _cfg_int(getattr(cfg, "system2_max_atoms", 80), 80)
        self._system2_busy = True
        try:
            self.memory_ledger.expire_stale(days=30)
            result = await asyncio.to_thread(
                self.layered_memory.system2_consolidate,
                max_atoms=max_atoms,
                reason=reason,
            )
            logger.debug("layered System2 consolidation result: %s", result)
            return result
        finally:
            self._system2_busy = False

    async def offload_tool_results(
        self,
        messages: list[Any],
        **_kwargs,
    ) -> dict | None:
        """Persist large tool results and replace them with symbolic summaries."""
        cfg = load_agent_config(
            self.agent_id,
        ).running.reme_light_memory_config.offload_config
        if not cfg.enabled:
            return None
        result = self.layered_memory.offload_tool_results(
            messages=messages,
            min_bytes=cfg.min_bytes,
            max_summary_chars=cfg.max_summary_chars,
        )
        if result.get("created") or result.get("replaced"):
            logger.info("layered offload result: %s", result)
        return result

    def layered_snapshot(self) -> dict | None:
        """Return L0/L1/L2/L3 and offload diagnostics."""
        snapshot = self.layered_memory.snapshot()
        snapshot["engine_status"] = self._build_engine_status(snapshot)
        return snapshot

    async def layered_memory_search(
        self,
        query: str,
        max_results: int = 5,
    ) -> ToolResponse:
        """
        Search layered L1 atom memories.

        Use this for prior durable facts, workflow conventions, scene memory,
        and recently extracted task progress. Results include source ids that
        can be opened with `memory_source_open`.
        """
        results = self.layered_memory.search_atoms(
            query=query,
            max_results=max_results,
        )
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=json.dumps(results, ensure_ascii=False, indent=2),
                ),
            ],
        )

    async def conversation_search(
        self,
        query: str,
        max_results: int = 5,
    ) -> ToolResponse:
        """
        Search raw L0 conversation snippets by keyword overlap.

        Use this when the exact source conversation matters or when L1 memory
        is too compressed. Results contain source ids for `memory_source_open`.
        """
        results = self.layered_memory.search_conversations(
            query=query,
            max_results=max_results,
        )
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=json.dumps(results, ensure_ascii=False, indent=2),
                ),
            ],
        )

    async def memory_source_open(self, source_id: str) -> ToolResponse:
        """
        Open the original evidence for a fact, a layered memory source, or an
        offloaded tool-result node.

        Args:
            source_id (`str`): Canonical fact id, L0 record id, source message
                id, or offload node id returned by memory search tools.
        """
        result = (
            self.memory_ledger.open_fact_sources(source_id)
            if source_id.startswith("fact_")
            else self.layered_memory.open_source(source_id)
        )
        payload = result or {"found": False, "source_id": source_id}
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                ),
            ],
        )

    async def dream(
        self,
        *,
        trigger: str = "scheduled",
        shadow: bool = False,
    ) -> dict[str, Any]:
        """Run governed Light/REM/Deep consolidation over canonical evidence."""

        preferences = self.memory_ledger.get_preferences()
        if not (
            preferences.get("runtime_enabled", True)
            and preferences.get("capture_enabled", True)
        ):
            return {
                "status": "skipped",
                "reason": "memory_disabled",
                "trigger": trigger,
                "shadow": bool(shadow),
            }
        agent_config = load_agent_config(self.agent_id)
        dream_cfg = agent_config.running.reme_light_memory_config
        min_score = _cfg_float(
            getattr(dream_cfg, "dreaming_min_score", 0.68),
            0.68,
        )
        rem_enabled = _cfg_bool(
            getattr(dream_cfg, "dreaming_rem_enabled", True),
            True,
        )
        service = MemoryDreamingService(
            self.working_dir,
            agent_id=self.agent_id,
            ledger=self.memory_ledger,
        )
        result = await asyncio.to_thread(
            service.run,
            trigger=trigger,
            shadow=shadow,
            min_score=min_score,
            rem_enabled=rem_enabled,
        )
        if rem_enabled and result.get("candidates"):
            try:
                candidate_lines = [
                    (
                        f"- score={item.get('score')} eligible={item.get('eligible')} "
                        f"content={item.get('content')} reason={item.get('reason')}"
                    )
                    for item in result["candidates"][:12]
                    if isinstance(item, dict)
                ]
                reflection = await run_memory_text_task(
                    agent_id=self.agent_id,
                    system_prompt=(
                        "你是澜台的 REM 反思器。只能概括给定候选中反复出现的"
                        "主题、矛盾与可能过期之处；不得补充新事实、不得给出记忆"
                        "晋升决定。输出 120 到 400 字的中文短报告。该报告只供"
                        "用户浏览，永远不会成为记忆证据。"
                    ),
                    user_prompt="\n".join(candidate_lines),
                    purpose="memory_dreaming_rem_reflection",
                )
                if reflection is not None and reflection.text.strip():
                    attached = await asyncio.to_thread(
                        service.attach_reflection,
                        result["run_id"],
                        text=reflection.text,
                        model_label=reflection.model_label,
                    )
                    if attached is not None:
                        result = attached
            except Exception:
                logger.warning(
                    "Dreaming REM reflection failed; deterministic diary retained",
                    exc_info=True,
                )
        logger.info(
            "governed memory Dreaming completed: run_id=%s eligible=%s staged=%s",
            result.get("run_id"),
            result.get("counts", {}).get("eligible"),
            result.get("counts", {}).get("staged"),
        )
        return result
