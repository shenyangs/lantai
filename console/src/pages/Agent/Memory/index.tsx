import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import { useSearchParams } from "react-router-dom";
import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Skeleton,
  Switch,
  Tooltip,
} from "antd";
import {
  Archive,
  ArrowLeft,
  ArrowRight,
  Brain,
  Check,
  Clock3,
  Copy,
  Download,
  FileInput,
  History,
  Link2,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  Sparkles,
  Trash2,
  Upload,
} from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { workspaceApi } from "../../../api/modules/workspace";
import type {
  LayeredMemorySnapshot,
  MemoryClearPreview,
  MemoryDreamingRun,
  MemoryDreamingStatus,
  MemoryFact,
  MemoryImportCommitResult,
  MemoryImportPreview,
  MemoryImportPrompt,
  MemoryPackReport,
  MemoryRecallTrace,
  ProactiveOpportunity,
  RecentMemoryImport,
  UserMemoryEntry,
  UserMemorySnapshot,
  UserUnderstandingSnapshot,
} from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  confirmDiscardChanges,
  useBeforeUnload,
} from "../../../hooks/useUnsavedChanges";
import { useAgentStore } from "../../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";
import {
  buildMemoryEngineStatusView,
  toDisplayEvidence,
  toDisplaySentence,
  toUserFacingSourceTitle,
} from "./presentation";
import { formatMemoryTime as formatTime } from "./formatTime";
import {
  clearMemoryEditDraft,
  readMemoryEditDraft,
  writeMemoryEditDraft,
  type MemoryEditDraftOwner,
} from "./editDraft";
import {
  buildMemoryTopics,
  buildMemoryChanges,
  filterMemoryTopics,
  memoryTopicCorrectionReason,
  memoryTopicKindLabel,
  type MemoryTopic,
  type MemoryTopicKind,
} from "./model";
import { UnderstandingProjectionPanel } from "./UnderstandingProjectionPanel";
import { FactSources } from "./FactSources";
import styles from "./index.module.less";

const AUTO_REFRESH_INTERVAL_MS = 10_000;
const MEMORY_CLEAR_CONFIRMATION = "删除全部记忆";
const MEMORY_PACK_ADOPT_CONFIRMATION = "采用记忆包";

type MemoryView =
  | "home"
  | "library"
  | "topic"
  | "review"
  | "settings"
  | "organize";

type OptionalMemoryPanel =
  | "suggestions"
  | "preferences"
  | "recall"
  | "recentImport"
  | "dreaming"
  | "opportunities";

const OPTIONAL_MEMORY_PANEL_LABELS: Record<OptionalMemoryPanel, string> = {
  suggestions: "待核对记忆",
  preferences: "记忆开关",
  recall: "最近检索记录",
  recentImport: "最近导入记录",
  dreaming: "自动整理",
  opportunities: "主动关注",
};

const EMPTY_SNAPSHOT: UserMemorySnapshot = {
  entries: [],
  stats: {
    total: 0,
    auto: 0,
    manual: 0,
    conflict: 0,
    by_type: { identity: 0, workflow: 0, style: 0, instruction: 0 },
  },
  profile_summary: {
    summary: "",
    by_type: { identity: [], workflow: [], style: [], instruction: [] },
  },
};

const EMPTY_LAYERED: LayeredMemorySnapshot = {
  stats: {
    l0_conversations: 0,
    l1_atoms: 0,
    l2_scenes: 0,
    offload_entries: 0,
    system2_models: 0,
    open_intentions: 0,
  },
  atoms: [],
  scenes: [],
  persona: "",
  system2: { models: [], open_intentions: [], evolution_chains: [] },
  offload: [],
  storage_path: "",
  updated_at: "",
};

const EMPTY_UNDERSTANDING: UserUnderstandingSnapshot = {
  confirmed: [],
  hypotheses: [],
  open_loops: [],
  stats: { confirmed: 0, hypotheses: 0, open_loops: 0, retired: 0 },
};

const EMPTY_DREAMING: MemoryDreamingStatus = {
  enabled: false,
  schedule: "0 3 * * *",
  rem_enabled: true,
  min_score: 0.68,
  scheduled: false,
  latest_run: null,
  runs: [],
  next_run_at: "",
  diary_path: "",
  diary_excerpt: "",
  safety: {
    confirmed_only_injection: true,
    assistant_and_tool_evidence_blocked: true,
    diary_is_promotion_source: false,
  },
};

const DREAM_SCHEDULES = [
  { value: "0 23 * * *", label: "每天晚上 11:00" },
  { value: "0 1 * * *", label: "每天凌晨 1:00" },
  { value: "0 2 * * *", label: "每天凌晨 2:00" },
  { value: "0 3 * * *", label: "每天凌晨 3:00" },
  { value: "0 4 * * *", label: "每天凌晨 4:00" },
];

const IMPORT_PROVIDERS = [
  "ChatGPT",
  "Claude",
  "Gemini",
  "豆包",
  "DeepSeek",
  "Kimi",
  "其他 AI",
].map((value) => ({ value, label: value }));

const PACK_CATEGORY_LABELS: Record<string, string> = {
  root_memory: "基础记忆",
  daily_memory: "近期记录",
  user_memory: "关于你的资料和长期记忆",
  memory_ledger: "已确认和待确认记忆",
  layered_memory: "从对话中整理的信息",
  dreaming: "自动整理记录",
  user_understanding: "已确认的理解与修正",
  metadata: "其他记忆数据",
};

function formatSchedule(value: string) {
  return (
    DREAM_SCHEDULES.find((item) => item.value === value)?.label || "自定义时间"
  );
}

function lastRecallState(trace: MemoryRecallTrace) {
  const explicit = (trace as MemoryRecallTrace & { status?: string }).status;
  if (explicit) return explicit;
  if (!trace.query && !trace.updated_at) return "unavailable";
  return trace.items.length ? "ready" : "empty";
}

function dreamResultTitle(run: MemoryDreamingRun | null) {
  if (!run) return "还没有整理记录";
  if (run.status === "error") return "这次整理没有完成";
  if (run.status === "skipped") return "暂时没有新的内容需要整理";
  const staged = run.counts?.staged || 0;
  return staged
    ? `发现 ${staged} 条需要核对的理解`
    : "最近内容已检查，没有需要你处理的变化";
}

function topicKindIcon(kind: MemoryTopicKind) {
  if (kind === "project") return <History size={16} />;
  if (kind === "experience") return <Clock3 size={16} />;
  if (kind === "theme") return <Sparkles size={16} />;
  return <Brain size={16} />;
}

export default function MemoryPage() {
  const { message, modal } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const [searchParams] = useSearchParams();
  const loadGate = useRef(createLatestRequestGate());
  const optionalLoadGate = useRef(createLatestRequestGate());
  const mutationScope = useRef(createAsyncStateScope());
  const packInputRef = useRef<HTMLInputElement | null>(null);

  const [view, setView] = useState<MemoryView>("home");
  const [snapshot, setSnapshot] = useState(EMPTY_SNAPSHOT);
  const [layered, setLayered] = useState(EMPTY_LAYERED);
  const [understanding, setUnderstanding] = useState(EMPTY_UNDERSTANDING);
  const [suggestions, setSuggestions] = useState<MemoryFact[]>([]);
  const [opportunities, setOpportunities] = useState<ProactiveOpportunity[]>(
    [],
  );
  const [dreaming, setDreaming] = useState(EMPTY_DREAMING);
  const [recall, setRecall] = useState<MemoryRecallTrace>({
    query: "",
    updated_at: "",
    items: [],
  });
  const [recentImport, setRecentImport] =
    useState<RecentMemoryImport["batch"]>(null);
  const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [optionalLoadErrors, setOptionalLoadErrors] = useState<
    Partial<Record<OptionalMemoryPanel, string>>
  >({});
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null);

  const [query, setQuery] = useState("");
  const [topicKind, setTopicKind] = useState<MemoryTopicKind | "all">("all");
  const [selectedTopicId, setSelectedTopicId] = useState("");
  const [recallExpanded, setRecallExpanded] = useState(false);
  const [deferredReviewIds, setDeferredReviewIds] = useState<Set<string>>(
    () => new Set(),
  );

  useEffect(() => {
    const requestedQuery = searchParams.get("query")?.trim();
    if (!requestedQuery) return;
    setQuery(requestedQuery);
    setView("library");
  }, [searchParams]);

  const [addOpen, setAddOpen] = useState(false);
  const [addText, setAddText] = useState("");
  const [addSaving, setAddSaving] = useState(false);
  const [editingEntry, setEditingEntry] = useState<UserMemoryEntry | null>(
    null,
  );
  const [editText, setEditText] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [editDraftOwner, setEditDraftOwner] =
    useState<MemoryEditDraftOwner | null>(null);
  const [editDraftRestored, setEditDraftRestored] = useState(false);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [correctionText, setCorrectionText] = useState("");
  const [correctionSaving, setCorrectionSaving] = useState(false);

  const [importOpen, setImportOpen] = useState(false);
  const [importStage, setImportStage] = useState<"paste" | "review" | "done">(
    "paste",
  );
  const [importProvider, setImportProvider] = useState("ChatGPT");
  const [importPrompt, setImportPrompt] = useState<MemoryImportPrompt | null>(
    null,
  );
  const [importText, setImportText] = useState("");
  const [importPreview, setImportPreview] =
    useState<MemoryImportPreview | null>(null);
  const [importSelected, setImportSelected] = useState<string[]>([]);
  const [importResult, setImportResult] =
    useState<MemoryImportCommitResult | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importUndoing, setImportUndoing] = useState(false);

  const [packFile, setPackFile] = useState<File | null>(null);
  const [packReport, setPackReport] = useState<MemoryPackReport | null>(null);
  const [packConfirmation, setPackConfirmation] = useState("");
  const [packBusy, setPackBusy] = useState<
    "export" | "inspect" | "adopt" | null
  >(null);

  const [clearOpen, setClearOpen] = useState(false);
  const [clearPreview, setClearPreview] = useState<MemoryClearPreview | null>(
    null,
  );
  const [clearConfirmation, setClearConfirmation] = useState("");
  const [clearBackup, setClearBackup] = useState(true);
  const [clearBusy, setClearBusy] = useState(false);
  const [dreamBusy, setDreamBusy] = useState(false);

  const importDirty =
    importOpen && importStage !== "done" && Boolean(importText.trim());
  const editDirty = Boolean(editingEntry && editText !== editingEntry.content);
  useBeforeUnload(importDirty || editDirty);

  const loadOptionalMemories = useCallback(async () => {
    const requestAgent = selectedAgent;
    const ownsState = optionalLoadGate.current.begin();
    const results = await Promise.allSettled([
      workspaceApi.listMemorySuggestions(),
      workspaceApi.getMemoryPreferences(),
      workspaceApi.getLastMemoryRecall(),
      workspaceApi.getRecentMemoryImport(),
      workspaceApi.getMemoryDreaming(),
      workspaceApi.listProactiveOpportunities(),
    ]);
    if (
      !ownsState() ||
      useAgentStore.getState().selectedAgent !== requestAgent
    ) {
      return;
    }

    const nextErrors: Partial<Record<OptionalMemoryPanel, string>> = {};
    const [
      nextSuggestions,
      preferences,
      nextRecall,
      nextImport,
      nextDreaming,
      nextOpportunities,
    ] = results;

    if (nextSuggestions.status === "fulfilled") {
      setSuggestions(nextSuggestions.value.facts || []);
    } else {
      nextErrors.suggestions = "failed";
    }
    if (preferences.status === "fulfilled") {
      setMemoryEnabled(
        preferences.value.runtime_enabled && preferences.value.capture_enabled,
      );
    } else {
      nextErrors.preferences = "failed";
    }
    if (nextRecall.status === "fulfilled") {
      setRecall(nextRecall.value);
    } else {
      nextErrors.recall = "failed";
    }
    if (nextImport.status === "fulfilled") {
      setRecentImport(nextImport.value.batch);
    } else {
      nextErrors.recentImport = "failed";
    }
    if (nextDreaming.status === "fulfilled") {
      setDreaming(nextDreaming.value);
    } else {
      nextErrors.dreaming = "failed";
    }
    if (nextOpportunities.status === "fulfilled") {
      setOpportunities(nextOpportunities.value.opportunities || []);
    } else {
      nextErrors.opportunities = "failed";
    }
    setOptionalLoadErrors(nextErrors);

    const failedPanels = Object.keys(nextErrors);
    if (failedPanels.length) {
      console.warn(
        "Some optional memory panels failed to load:",
        failedPanels,
      );
    }
  }, [selectedAgent]);

  const loadMemories = useCallback(
    async (silent = false) => {
      const requestAgent = selectedAgent;
      const ownsState = loadGate.current.begin();
      if (!silent) setLoading(true);
      const optionalLoad = loadOptionalMemories();
      try {
        const [nextSnapshot, nextLayered, nextUnderstanding] =
          await Promise.all([
            workspaceApi.listUserMemory(true),
            workspaceApi.getLayeredMemory(),
            workspaceApi.getUserUnderstanding(),
          ]);
        if (
          !ownsState() ||
          useAgentStore.getState().selectedAgent !== requestAgent
        ) {
          return;
        }
        setSnapshot(nextSnapshot);
        setLayered(nextLayered);
        setUnderstanding(nextUnderstanding);
        setLoadError("");
        setLastSyncedAt(Date.now());
      } catch (error) {
        if (
          !ownsState() ||
          useAgentStore.getState().selectedAgent !== requestAgent
        ) {
          return;
        }
        console.error("Failed to load memory workspace:", error);
        setLoadError("暂时无法读取记忆。现有内容没有被修改，你可以重新加载。 ");
      } finally {
        if (
          ownsState() &&
          useAgentStore.getState().selectedAgent === requestAgent &&
          !silent
        ) {
          setLoading(false);
        }
      }
      await optionalLoad;
    },
    [loadOptionalMemories, selectedAgent],
  );

  useEffect(() => {
    loadGate.current.invalidate();
    optionalLoadGate.current.invalidate();
    mutationScope.current.invalidate();
    setSuggestions([]);
    setOpportunities([]);
    setDreaming(EMPTY_DREAMING);
    setRecall({ query: "", updated_at: "", items: [] });
    setRecentImport(null);
    setOptionalLoadErrors({});
    setEditingEntry(null);
    setEditText("");
    setEditDraftOwner(null);
    setEditDraftRestored(false);
  }, [selectedAgent]);

  useEffect(() => {
    void loadMemories();
    const timer = window.setInterval(
      () => void loadMemories(true),
      AUTO_REFRESH_INTERVAL_MS,
    );
    return () => window.clearInterval(timer);
  }, [loadMemories, selectedAgent]);

  useEffect(() => {
    const key = `lantai-memory-review-deferred:${selectedAgent || "default"}`;
    try {
      const stored = JSON.parse(sessionStorage.getItem(key) || "[]");
      setDeferredReviewIds(
        new Set(Array.isArray(stored) ? stored.map(String) : []),
      );
    } catch {
      setDeferredReviewIds(new Set());
    }
  }, [selectedAgent]);

  const topics = useMemo(
    () => buildMemoryTopics(snapshot, layered, understanding),
    [snapshot, layered, understanding],
  );
  const changes = useMemo(
    () => buildMemoryChanges(snapshot, dreaming, topics),
    [snapshot, dreaming, topics],
  );
  const filteredTopics = useMemo(
    () => filterMemoryTopics(topics, query, topicKind),
    [topics, query, topicKind],
  );
  const selectedTopic =
    topics.find((topic) => topic.id === selectedTopicId) || null;
  const profileTopics = topics.filter((topic) => topic.kind === "profile");
  const activeOpportunities = opportunities.filter(
    (item) => item.status === "queued",
  );
  const archivedEntries = snapshot.entries.filter(
    (entry) => entry.status === "archived",
  );
  const reviewIds = [
    ...suggestions.map((item) => `fact:${item.id}`),
    ...understanding.hypotheses.map((item) => `hypothesis:${item.id}`),
  ];
  const reviewCount = reviewIds.filter(
    (id) => !deferredReviewIds.has(id),
  ).length;
  const engineStatus = buildMemoryEngineStatusView(snapshot, layered);
  const recallStatus = lastRecallState(recall);

  const navigate = (next: MemoryView) => {
    setView(next);
    if (next !== "topic") setSelectedTopicId("");
  };

  const openTopic = (topic: MemoryTopic) => {
    setSelectedTopicId(topic.id);
    setView("topic");
  };

  const persistDeferredReviews = (next: Set<string>) => {
    setDeferredReviewIds(next);
    const key = `lantai-memory-review-deferred:${selectedAgent || "default"}`;
    try {
      sessionStorage.setItem(key, JSON.stringify([...next]));
    } catch {
      // Session persistence is a convenience; review choices are never inferred.
    }
  };

  const refreshAfter = async (notice: string) => {
    message.success(notice);
    await loadMemories(true);
  };

  const handleToggleMemory = async (enabled: boolean) => {
    const ownsState = mutationScope.current.capture();
    setMemoryEnabled(enabled);
    try {
      await workspaceApi.updateMemoryPreferences({
        runtime_enabled: enabled,
        capture_enabled: enabled,
      });
      if (!ownsState()) return;
      message.success(
        enabled ? "长期记忆已恢复" : "已暂停读取和新增长期记忆；已有内容仍保留",
      );
    } catch (error) {
      if (!ownsState()) return;
      setMemoryEnabled(!enabled);
      console.error("Failed to update memory preferences:", error);
      message.error("没有保存成功，请重试");
    }
  };

  const handleAdd = async () => {
    const content = addText.trim();
    if (!content) return;
    setAddSaving(true);
    try {
      const result = await workspaceApi.addUserMemory(content);
      if (result.decision === "skip") {
        message.info("这项内容已经存在，不需要重复保存");
      } else if (result.decision === "conflict") {
        message.warning("发现不同说法，已放到需要核对");
      } else {
        message.success("澜台会在相关对话中记得这件事");
      }
      setAddOpen(false);
      setAddText("");
      await loadMemories(true);
    } catch (error) {
      console.error("Failed to add memory:", error);
      message.error("没有保存成功，输入内容仍保留");
    } finally {
      setAddSaving(false);
    }
  };

  const openEdit = (entry: UserMemoryEntry) => {
    const owner = {
      agentId: selectedAgent || "default",
      entryId: entry.id,
      revision: entry.updated_at || entry.created_at || "",
    };
    const draft = readMemoryEditDraft(owner);
    setEditingEntry(entry);
    setEditDraftOwner(owner);
    setEditText(draft === undefined ? entry.content : draft);
    setEditDraftRestored(draft !== undefined);
  };

  const discardEdit = () => {
    if (editDraftOwner) clearMemoryEditDraft(editDraftOwner);
    setEditingEntry(null);
    setEditText("");
    setEditDraftOwner(null);
    setEditDraftRestored(false);
  };

  const closeEdit = () =>
    confirmDiscardChanges(modal, editDirty, discardEdit, {
      title: "放弃这次修改？",
      content: "尚未保存的修改会丢失，原记忆不会改变。",
    });

  const handleEditTextChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    const nextText = event.target.value;
    setEditText(nextText);
    if (!editingEntry || !editDraftOwner) return;
    if (nextText === editingEntry.content) {
      clearMemoryEditDraft(editDraftOwner);
      setEditDraftRestored(false);
      return;
    }
    writeMemoryEditDraft(editDraftOwner, nextText);
  };

  const handleEdit = async () => {
    if (!editingEntry || !editDraftOwner || !editText.trim()) return;
    if (editDraftOwner.agentId !== (selectedAgent || "default")) {
      message.error("已切换 Agent，未保存的修改仍保留在原 Agent。");
      setEditingEntry(null);
      setEditText("");
      setEditDraftOwner(null);
      setEditDraftRestored(false);
      return;
    }
    setEditSaving(true);
    try {
      await workspaceApi.updateUserMemory(editingEntry.id, {
        content: editText.trim(),
      });
      clearMemoryEditDraft(editDraftOwner);
      setEditingEntry(null);
      setEditText("");
      setEditDraftOwner(null);
      setEditDraftRestored(false);
      await refreshAfter("已修改，之后相关对话会使用新的说法");
    } catch (error) {
      console.error("Failed to edit memory:", error);
      message.error("没有保存成功，修改内容仍保留");
    } finally {
      setEditSaving(false);
    }
  };

  const handleCorrection = async () => {
    if (!selectedTopic || !correctionText.trim()) return;
    setCorrectionSaving(true);
    try {
      await workspaceApi.addUserMemory(
        correctionText.trim(),
        selectedTopic.correctionType,
        memoryTopicCorrectionReason(selectedTopic),
      );
      setCorrectionOpen(false);
      setCorrectionText("");
      await refreshAfter("纠正已经记录，并会优先显示在这个主题中");
    } catch (error) {
      console.error("Failed to correct memory topic:", error);
      message.error("没有保存成功，纠正内容仍保留");
    } finally {
      setCorrectionSaving(false);
    }
  };

  const handleArchive = async (entry: UserMemoryEntry) => {
    try {
      await workspaceApi.archiveUserMemory(entry.id);
      await refreshAfter("已停止使用这条记忆；聊天记录没有删除");
    } catch (error) {
      console.error("Failed to archive memory:", error);
      message.error("操作失败，原记忆没有改变");
    }
  };

  const handleRestore = async (entry: UserMemoryEntry) => {
    try {
      await workspaceApi.updateUserMemory(entry.id, { status: "active" });
      await refreshAfter("这条记忆已恢复使用");
    } catch (error) {
      console.error("Failed to restore memory:", error);
      message.error("恢复失败，原记忆仍保持停止使用");
    }
  };

  const handleDelete = async (entry: UserMemoryEntry) => {
    try {
      await workspaceApi.deleteUserMemory(entry.id);
      await refreshAfter("这条记忆已删除；原聊天和文件仍保留");
    } catch (error) {
      console.error("Failed to delete memory:", error);
      message.error("删除失败，原记忆仍然保留");
    }
  };

  const handleFactConfirm = async (
    fact: MemoryFact,
    scope: "global" | "project",
  ) => {
    try {
      await workspaceApi.confirmMemoryFact(fact.id, scope);
      await refreshAfter(
        scope === "project" ? "已限制为只在当前项目使用" : "已确认以后可以使用",
      );
    } catch (error) {
      console.error("Failed to confirm memory suggestion:", error);
      message.error("没有保存成功，请重试");
    }
  };

  const handleFactReject = async (fact: MemoryFact) => {
    try {
      await workspaceApi.rejectMemoryFact(fact.id, "用户明确选择不要记住");
      await refreshAfter("已拒绝，这条建议不会成为长期记忆");
    } catch (error) {
      console.error("Failed to reject memory suggestion:", error);
      message.error("操作失败，请重试");
    }
  };

  const handleHypothesis = async (
    atomId: string,
    verdict: "confirmed" | "corrected",
  ) => {
    try {
      await workspaceApi.feedbackUserUnderstanding(atomId, verdict);
      await refreshAfter(
        verdict === "confirmed" ? "已确认这项理解" : "已停止沿用这项理解",
      );
    } catch (error) {
      console.error("Failed to update understanding:", error);
      message.error("操作失败，请重试");
    }
  };

  const handleOpportunity = async (
    item: ProactiveOpportunity,
    verdict: "completed" | "dismissed",
  ) => {
    try {
      await workspaceApi.feedbackProactiveOpportunity(item.id, verdict);
      await refreshAfter(
        verdict === "completed"
          ? "已标记为处理完成"
          : "澜台不会再主动跟进这项建议",
      );
    } catch (error) {
      console.error("Failed to update proactive opportunity:", error);
      message.error("操作失败，请重试");
    }
  };

  const handleCompleteOpenLoop = async (atomId: string) => {
    try {
      await workspaceApi.feedbackUserUnderstanding(atomId, "corrected");
      await refreshAfter("已标记为结束；其他项目背景和历史记忆仍然保留");
    } catch (error) {
      console.error("Failed to complete open loop:", error);
      message.error("没有保存成功，这件事仍会显示为正在关注");
    }
  };

  const updateDreaming = async (
    patch: Partial<Pick<MemoryDreamingStatus, "enabled" | "schedule">>,
  ) => {
    const next = { ...dreaming, ...patch };
    setDreaming(next);
    try {
      await workspaceApi.updateMemoryDreaming({
        enabled: next.enabled,
        schedule: next.schedule,
        rem_enabled: next.rem_enabled,
        min_score: next.min_score,
      });
      message.success(next.enabled ? "自动整理设置已保存" : "自动整理已暂停");
    } catch (error) {
      console.error("Failed to update memory organization:", error);
      setDreaming(dreaming);
      message.error("没有保存成功，请重试");
    }
  };

  const runDreaming = async () => {
    setDreamBusy(true);
    try {
      const run = await workspaceApi.runMemoryDreaming({ shadow: false });
      await loadMemories(true);
      message.success(dreamResultTitle(run));
    } catch (error) {
      console.error("Failed to organize memory:", error);
      message.error("这次整理没有完成，原有记忆没有改变");
    } finally {
      setDreamBusy(false);
    }
  };

  const openImport = async () => {
    setImportOpen(true);
    setImportStage("paste");
    setImportText("");
    setImportPreview(null);
    setImportSelected([]);
    setImportResult(null);
    if (!importPrompt) {
      try {
        setImportPrompt(await workspaceApi.getMemoryImportPrompt());
      } catch (error) {
        console.error("Failed to load import prompt:", error);
        message.error("暂时无法准备迁移说明");
      }
    }
  };

  const closeImport = () =>
    confirmDiscardChanges(modal, importDirty, () => setImportOpen(false), {
      title: "退出并放弃这次导入？",
      content: "尚未导入的粘贴内容会丢失，现有记忆不会改变。",
    });

  const copyImportPrompt = async () => {
    if (!importPrompt?.prompt) return;
    try {
      await navigator.clipboard.writeText(importPrompt.prompt);
      message.success("提示词已复制");
    } catch (error) {
      console.error("Failed to copy import prompt:", error);
      message.error("复制失败，请手动选择文字");
    }
  };

  const previewImport = async () => {
    if (!importText.trim()) return;
    setImportBusy(true);
    try {
      const preview = await workspaceApi.previewMemoryImport({
        text: importText.trim(),
        provider: importProvider,
      });
      setImportPreview(preview);
      setImportSelected(
        preview.candidates
          .filter((candidate) => candidate.selected_by_default)
          .map((candidate) => candidate.id),
      );
      setImportText("");
      setImportStage("review");
    } catch (error) {
      console.error("Failed to preview import:", error);
      message.error("没有识别到可导入的内容，请检查后重试");
    } finally {
      setImportBusy(false);
    }
  };

  const commitImport = async () => {
    if (!importPreview || !importSelected.length) return;
    setImportBusy(true);
    try {
      const result = await workspaceApi.commitMemoryImport(
        importPreview.batch_id,
        importSelected,
      );
      setImportResult(result);
      setImportStage("done");
      await loadMemories(true);
    } catch (error) {
      console.error("Failed to commit import:", error);
      message.error("导入没有完成，现有记忆未被替换");
    } finally {
      setImportBusy(false);
    }
  };

  const undoImport = async (batchId: string) => {
    setImportUndoing(true);
    try {
      const result = await workspaceApi.undoMemoryImport(batchId);
      setRecentImport((current) =>
        current?.batch_id === batchId
          ? {
              ...current,
              status: "undone",
              can_undo: false,
              undone_at: result.undone_at,
            }
          : current,
      );
      await refreshAfter(
        `已撤销最近导入，移除 ${result.removed_count} 条并恢复 ${result.restored_count} 条`,
      );
    } catch (error) {
      console.error("Failed to undo memory import:", error);
      message.error("撤销没有完成，现有记忆没有被整批改变");
    } finally {
      setImportUndoing(false);
    }
  };

  const exportPack = async () => {
    setPackBusy("export");
    try {
      const result = await workspaceApi.exportMemoryPack();
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      message.success("记忆包已导出；含记忆来源原文，不含完整聊天记录或账号密钥");
    } catch (error) {
      console.error("Failed to export memory pack:", error);
      message.error("导出失败，请重试");
    } finally {
      setPackBusy(null);
    }
  };

  const selectPack = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null;
    event.target.value = "";
    if (!file) return;
    setPackFile(file);
    setPackReport(null);
    setPackConfirmation("");
    setPackBusy("inspect");
    try {
      setPackReport(await workspaceApi.adoptMemoryPack(file, { dryRun: true }));
    } catch (error) {
      console.error("Failed to inspect memory pack:", error);
      setPackFile(null);
      message.error("这个记忆包无法读取");
    } finally {
      setPackBusy(null);
    }
  };

  const adoptPack = async () => {
    if (!packFile || packConfirmation !== MEMORY_PACK_ADOPT_CONFIRMATION)
      return;
    setPackBusy("adopt");
    try {
      const report = await workspaceApi.adoptMemoryPack(packFile, {
        dryRun: false,
        confirmation: packConfirmation,
      });
      if (!report.adopted) throw new Error("Memory pack was not adopted");
      setPackFile(null);
      setPackReport(null);
      setPackConfirmation("");
      await refreshAfter("记忆包已采用，原内容已生成本地恢复包");
    } catch (error) {
      console.error("Failed to adopt memory pack:", error);
      message.error("采用失败，原记忆没有被替换");
    } finally {
      setPackBusy(null);
    }
  };

  const openClear = async () => {
    setClearOpen(true);
    setClearPreview(null);
    setClearConfirmation("");
    setClearBackup(true);
    try {
      setClearPreview(await workspaceApi.getMemoryClearPreview());
    } catch (error) {
      console.error("Failed to preview clear memory:", error);
      message.error("暂时无法核对删除范围");
    }
  };

  const clearAll = async () => {
    if (
      clearConfirmation !==
      (clearPreview?.confirmation_text || MEMORY_CLEAR_CONFIRMATION)
    )
      return;
    setClearBusy(true);
    try {
      const result = await workspaceApi.clearAllMemory({
        confirmation: clearConfirmation,
        create_backup: clearBackup,
      });
      setClearOpen(false);
      await refreshAfter(
        result.backup_created
          ? "长期记忆已清空，并创建了本地恢复备份"
          : "长期记忆已清空；聊天记录没有删除",
      );
    } catch (error) {
      console.error("Failed to clear memory:", error);
      message.error("删除没有完成，原有内容可能仍保留");
    } finally {
      setClearBusy(false);
    }
  };

  const renderTopicRow = (topic: MemoryTopic) => (
    <button
      key={topic.id}
      type="button"
      className={styles.topicRow}
      onClick={() => openTopic(topic)}
    >
      <span className={styles.topicIcon}>{topicKindIcon(topic.kind)}</span>
      <span className={styles.topicMain}>
        <span className={styles.topicTitleLine}>
          <strong>{topic.title}</strong>
          <span>{memoryTopicKindLabel(topic.kind)}</span>
        </span>
        <span className={styles.topicSummary}>{topic.summary}</span>
      </span>
      <span className={styles.topicMeta}>
        {formatTime(topic.updatedAt) ||
          (topic.entries.length
            ? `${topic.entries.length} 条可查看记忆`
            : "已形成主题")}
      </span>
      <ArrowRight size={16} className={styles.topicArrow} />
    </button>
  );

  const renderHome = () => (
    <div className={styles.homeView}>
      <section className={styles.understandingSection}>
        <div className={styles.sectionTitle}>
          <div>
            <h2>澜台现在怎样理解你</h2>
            <p>只显示会影响以后协作的内容，哪里不对可以直接修改。</p>
          </div>
          <Button type="text" onClick={() => navigate("library")}>
            查看全部
          </Button>
        </div>
        {profileTopics.length ? (
          <div className={styles.understandingGrid}>
            {profileTopics.map((topic) => (
              <button
                type="button"
                key={topic.id}
                className={styles.understandingItem}
                onClick={() => openTopic(topic)}
              >
                <span>{topic.title}</span>
                <strong>{topic.summary}</strong>
                <small>{topic.evidenceCount} 条相关记忆</small>
              </button>
            ))}
          </div>
        ) : (
          <div className={styles.firstMemory}>
            <div>
              <h3>还在慢慢了解你</h3>
              <p>正常聊天就可以。重要且适合长期使用的信息会在这里逐渐形成。</p>
            </div>
            <Button icon={<Plus size={15} />} onClick={() => setAddOpen(true)}>
              主动告诉澜台一件事
            </Button>
          </div>
        )}
      </section>

      <section className={styles.changesSection}>
        <div className={styles.sectionTitle}>
          <div>
            <h2>最近变化</h2>
            <p>这里说明具体发生了什么，不用通过运行次数判断整理有没有价值。</p>
          </div>
          <Button type="text" onClick={() => navigate("organize")}>
            自动整理
          </Button>
        </div>
        {changes.length ? (
          <div className={styles.changeList}>
            {changes.slice(0, 5).map((change) => {
              const topic = change.topicId
                ? topics.find((item) => item.id === change.topicId)
                : null;
              return (
                <article key={change.id} className={styles.changeRow}>
                  <span>{formatTime(change.occurredAt) || "近期"}</span>
                  <strong>
                    {change.verb} · {change.title}
                  </strong>
                  <div className={styles.changeCopy}>
                    <p>{change.detail}</p>
                    <small>
                      {change.reason} · {change.impact}
                    </small>
                  </div>
                  {topic ? (
                    <Button
                      type="text"
                      aria-label={`查看${change.title}`}
                      icon={<ArrowRight size={15} />}
                      onClick={() => openTopic(topic)}
                    />
                  ) : (
                    <span />
                  )}
                </article>
              );
            })}
          </div>
        ) : (
          <p className={styles.quietEmpty}>
            有新的可用内容后，澜台会把变化整理在这里。
          </p>
        )}
      </section>

      {topics.filter((topic) => topic.kind !== "profile").length ? (
        <section className={styles.topicSection}>
          <div className={styles.sectionTitle}>
            <div>
              <h2>主题与项目</h2>
              <p>标题会根据真实内容形成，不需要你先设计分类。</p>
            </div>
          </div>
          <div className={styles.topicList}>
            {topics
              .filter((topic) => topic.kind !== "profile")
              .slice(0, 5)
              .map(renderTopicRow)}
          </div>
        </section>
      ) : null}

      {understanding.open_loops.length || activeOpportunities.length ? (
        <section className={styles.focusSection}>
          <div className={styles.sectionTitle}>
            <div>
              <h2>正在关注</h2>
              <p>
                这些内容不会阻止你使用其他记忆，也不代表澜台已经获得行动授权。
              </p>
            </div>
          </div>
          {[...understanding.open_loops].map((item) => (
            <div key={item.id} className={styles.focusRow}>
              <div className={styles.focusCopy}>
                <p>{item.statement}</p>
                <span>来自你近期提到的未完事项</span>
              </div>
              <Button
                size="small"
                onClick={() => void handleCompleteOpenLoop(item.id)}
              >
                这件事结束了
              </Button>
            </div>
          ))}
          {activeOpportunities.map((item) => (
            <div key={item.id} className={styles.focusRow}>
              <p>{item.content}</p>
              <div className={styles.focusActions}>
                <Button
                  size="small"
                  onClick={() => void handleOpportunity(item, "completed")}
                >
                  已经处理
                </Button>
                <Button
                  size="small"
                  type="text"
                  onClick={() => void handleOpportunity(item, "dismissed")}
                >
                  不用跟进
                </Button>
              </div>
            </div>
          ))}
        </section>
      ) : null}

      <section className={styles.recallSection}>
        <button
          type="button"
          className={styles.recallSummary}
          aria-expanded={recallExpanded}
          onClick={() => setRecallExpanded((current) => !current)}
        >
          <Link2 size={16} />
          <span>
            <strong>最近一次记忆检索记录</strong>
            <small>
              {recallStatus === "ready"
                ? `检索到 ${recall.items.length} 条相关记忆 · ${formatTime(recall.updated_at)}`
                : recallStatus === "empty"
                  ? `已完成检索，没有找到适合提供的长期记忆 · ${formatTime(recall.updated_at)}`
                  : recallStatus === "error"
                    ? "记录读取失败，不能据此判断是否检索过记忆"
                    : "暂无可追溯的检索记录，不把未知显示成 0"}
            </small>
          </span>
          <ArrowRight size={15} data-open={recallExpanded} />
        </button>
        {recallExpanded ? (
          <div className={styles.recallDetails}>
            {recall.query ? <p>对应问题：{recall.query}</p> : null}
            {recall.items.length ? (
              <ul>
                {recall.items.map((item) => (
                  <li key={`${item.citation}-${item.content}`}>
                    <strong>{item.content}</strong>
                    <span>{item.citation}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p>这里记录的是最近一次检索，不代表模型内部全部因果。</p>
            )}
          </div>
        ) : null}
      </section>
    </div>
  );

  const renderLibrary = () => {
    const allKinds: Array<{ key: MemoryTopicKind | "all"; label: string }> = [
      { key: "all", label: "全部" },
      { key: "profile", label: "关于你" },
      { key: "project", label: "项目" },
      { key: "theme", label: "理解主题" },
      { key: "experience", label: "事项" },
    ];
    const kinds = allKinds.filter(
      ({ key }) => key === "all" || topics.some((topic) => topic.kind === key),
    );
    return (
      <section className={styles.libraryView}>
        <div className={styles.libraryHeader}>
          <div>
            <h2>全部记忆</h2>
            <p>按主题查找。事实只有一份，分类只是帮助你找到它。</p>
          </div>
          <Button
            type="primary"
            icon={<Plus size={15} />}
            onClick={() => setAddOpen(true)}
          >
            告诉澜台一件事
          </Button>
        </div>
        <div className={styles.libraryTools}>
          <Input
            prefix={<Search size={15} />}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            allowClear
            placeholder="搜索主题和内容"
            aria-label="搜索记忆"
          />
          <nav aria-label="记忆分类">
            {kinds.map((item) => (
              <button
                type="button"
                key={item.key}
                data-active={topicKind === item.key}
                onClick={() => setTopicKind(item.key)}
              >
                {item.label}
              </button>
            ))}
          </nav>
        </div>
        {filteredTopics.length ? (
          <div className={styles.topicList}>
            {filteredTopics.map(renderTopicRow)}
          </div>
        ) : (
          <div className={styles.libraryEmpty}>
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                query ? "没有找到匹配的记忆" : "还没有形成可查看的主题"
              }
            />
          </div>
        )}
      </section>
    );
  };

  const renderTopic = () => {
    if (!selectedTopic) return renderLibrary();
    return (
      <article className={styles.topicDetail}>
        <Button
          type="text"
          icon={<ArrowLeft size={16} />}
          onClick={() => navigate("library")}
        >
          返回全部记忆
        </Button>
        <header>
          <h2>{selectedTopic.title}</h2>
          <p>{selectedTopic.summary}</p>
          <small>
            {memoryTopicKindLabel(selectedTopic.kind)} ·{" "}
            {selectedTopic.entries.length
              ? `${selectedTopic.entries.length} 条可查看记忆`
              : "由相关记忆整理"}
            {selectedTopic.updatedAt
              ? ` · 更新于 ${formatTime(selectedTopic.updatedAt)}`
              : ""}
          </small>
          <Button
            icon={<Pencil size={15} />}
            onClick={() => {
              setCorrectionText("");
              setCorrectionOpen(true);
            }}
          >
            纠正这项理解
          </Button>
        </header>
        {selectedTopic.details.some(
          (detail) => detail !== selectedTopic.summary,
        ) && (
          <section className={styles.topicBody}>
            <h3>主题中的信息</h3>
            <div className={styles.topicParagraphs}>
              {selectedTopic.details
                .filter((detail) => detail !== selectedTopic.summary)
                .map((detail, index) => (
                  <p key={`${index}-${detail}`}>{detail}</p>
                ))}
            </div>
          </section>
        )}
        {selectedTopic.entries.length ? (
          <section className={styles.entrySection}>
            <h3>可以直接修改的记忆</h3>
            {selectedTopic.entries.map((entry) => (
              <div key={entry.id} className={styles.entryRow}>
                <div>
                  <p>
                    {toDisplaySentence(entry.content, entry.type) ||
                      entry.content}
                  </p>
                  <span>
                    {toUserFacingSourceTitle(entry)}
                    {entry.updated_at
                      ? ` · ${formatTime(entry.updated_at)}`
                      : ""}
                  </span>
                </div>
                <div>
                  <Tooltip title="修改">
                    <Button
                      type="text"
                      aria-label="修改记忆"
                      icon={<Pencil size={15} />}
                      onClick={() => openEdit(entry)}
                    />
                  </Tooltip>
                  <Popconfirm
                    title="停止使用这条记忆？"
                    description="原聊天和文件不会删除。"
                    okText="停止使用"
                    cancelText="取消"
                    onConfirm={() => void handleArchive(entry)}
                  >
                    <Tooltip title="停止使用">
                      <Button
                        type="text"
                        aria-label="停止使用记忆"
                        icon={<Archive size={15} />}
                      />
                    </Tooltip>
                  </Popconfirm>
                  <Popconfirm
                    title="彻底删除这条记忆？"
                    description="原聊天和文件不会随之删除。"
                    okText="删除"
                    cancelText="取消"
                    onConfirm={() => void handleDelete(entry)}
                  >
                    <Tooltip title="删除">
                      <Button
                        danger
                        type="text"
                        aria-label="删除记忆"
                        icon={<Trash2 size={15} />}
                      />
                    </Tooltip>
                  </Popconfirm>
                </div>
                {entry.canonical_fact_id ? (
                  <FactSources
                    factId={entry.canonical_fact_id}
                    agentId={selectedAgent}
                    onChanged={() => void loadMemories(true)}
                  />
                ) : entry.evidence ? (
                  <details>
                    <summary>查看历史摘要</summary>
                    <p>{toDisplayEvidence(entry.evidence)}</p>
                    <p>这条历史记忆尚未关联可核验的原文。</p>
                  </details>
                ) : null}
              </div>
            ))}
          </section>
        ) : (
          <p className={styles.topicFootnote}>
            这个主题由多条记忆整理形成。你可以直接提交正确说法，原始资料不会被删除。
          </p>
        )}
      </article>
    );
  };

  const renderOrganize = () => {
    const run = dreaming.latest_run;
    const candidates = (run?.candidates || []).filter((item) => item.eligible);
    const mutationContents = (run?.mutations || [])
      .map((mutation) => {
        const after = mutation.after;
        if (!after || typeof after !== "object") return "";
        return String((after as Record<string, unknown>).content || "").trim();
      })
      .filter(Boolean);
    return (
      <section className={styles.organizeView}>
        <Button
          type="text"
          icon={<ArrowLeft size={16} />}
          onClick={() => navigate("home")}
        >
          返回记忆
        </Button>
        <header className={styles.readingHeader}>
          <h2>自动整理</h2>
          <p>
            澜台定期复核近期的记忆候选，把证据达到条件、仍需要你判断的内容放到核对区。
          </p>
        </header>
        <div className={styles.organizeControl}>
          <div>
            <strong>
              {dreaming.enabled ? "自动整理已开启" : "自动整理已暂停"}
            </strong>
            <span>
              {dreaming.enabled
                ? `${formatSchedule(dreaming.schedule)} 运行，由本机发起整理`
                : "仍然可以随时手动整理"}
            </span>
          </div>
          <Switch
            checked={dreaming.enabled}
            aria-label="启用自动整理"
            onChange={(enabled) => void updateDreaming({ enabled })}
          />
        </div>
        <div className={styles.organizeActions}>
          <label>
            <span>整理时间</span>
            <Select
              aria-label="整理时间"
              value={dreaming.schedule}
              options={DREAM_SCHEDULES}
              onChange={(schedule) => void updateDreaming({ schedule })}
            />
          </label>
          <Button
            type="primary"
            loading={dreamBusy}
            onClick={() => void runDreaming()}
          >
            现在整理
          </Button>
        </div>
        <section className={styles.organizeResult}>
          <div className={styles.sectionTitle}>
            <div>
              <h2>最近一次整理</h2>
              <p>
                {run?.completed_at ? formatTime(run.completed_at) : "尚未运行"}
              </p>
            </div>
          </div>
          <h3>{dreamResultTitle(run)}</h3>
          {mutationContents.length ? (
            <div className={styles.candidateList}>
              {mutationContents.map((content, index) => (
                <div key={`${run?.run_id || "run"}-${index}-${content}`}>
                  <strong>送入核对区：{content}</strong>
                  <span>这项变化尚未成为已确认的长期记忆。</span>
                </div>
              ))}
              {(run?.counts?.excluded || 0) > 0 ? (
                <div>
                  <strong>
                    保留原状：{run?.counts?.excluded} 条候选没有改变记忆
                  </strong>
                  <span>它们没有同时满足来源、重复出现和长期复用条件。</span>
                </div>
              ) : null}
            </div>
          ) : run?.status === "completed" ? (
            <div className={styles.candidateList}>
              {candidates.map((candidate) => (
                <div key={candidate.fact_id}>
                  <strong>{candidate.content}</strong>
                  <span>
                    达到筛选条件 · 在 {candidate.session_count} 次对话中出现 ·{" "}
                    {candidate.reason}
                  </span>
                </div>
              ))}
              <div>
                {(run.counts?.staged || 0) > 0 ? (
                  <>
                    <strong>
                      已有 {run.counts?.staged || 0} 条变化送入核对区
                    </strong>
                    <span>
                      这份记录没有包含可展示的变化详情，可以到“需要核对”查看。
                    </span>
                  </>
                ) : (
                  <>
                    <strong>
                      检查了 {run.counts?.reviewed || 0}{" "}
                      条候选，没有改变长期记忆
                    </strong>
                    <span>
                      {candidates.length
                        ? `${candidates.length} 条达到筛选条件，但这次没有产生可提交的变化。`
                        : "没有候选同时满足来源、重复出现和长期复用条件。"}
                    </span>
                  </>
                )}
              </div>
            </div>
          ) : (
            <p className={styles.quietEmpty}>
              {run
                ? "没有新增可展示的主题变化；原有记忆仍可正常使用。"
                : "有新的可用内容后，澜台会在这里说明整理结果。"}
            </p>
          )}
          {run?.can_rollback ? (
            <Popconfirm
              title="撤回这次整理产生的待核对变化？"
              description="用户之后手动处理过的内容不会被覆盖。"
              okText="撤回"
              cancelText="取消"
              onConfirm={async () => {
                try {
                  await workspaceApi.rollbackMemoryDreaming(run.run_id);
                  await refreshAfter("已安全撤回这次整理产生的待核对变化");
                } catch (error) {
                  console.error(
                    "Failed to roll back memory organization:",
                    error,
                  );
                  message.error("无法安全撤回，可能已有内容被修改");
                }
              }}
            >
              <Button type="text" icon={<RotateCcw size={15} />}>
                撤回这次整理
              </Button>
            </Popconfirm>
          ) : null}
        </section>
      </section>
    );
  };

  const renderSettings = () => (
    <section className={styles.settingsView}>
      <Button
        type="text"
        icon={<ArrowLeft size={16} />}
        onClick={() => navigate("home")}
      >
        返回记忆
      </Button>
      <header className={styles.readingHeader}>
        <h2>记忆设置</h2>
        <p>日常不需要管理。这里用于暂停、迁移或彻底删除记忆。</p>
      </header>
      <div className={styles.settingsGroup}>
        <div className={styles.settingRow}>
          <div>
            <strong>让澜台跨对话理解你</strong>
            <span>
              {memoryEnabled
                ? "相关背景和偏好会在适合的对话中使用。"
                : "暂停后不会读取或新增，已有内容仍保留。"}
            </span>
          </div>
          <Switch
            checked={memoryEnabled}
            aria-label="跨对话记忆"
            onChange={(enabled) => void handleToggleMemory(enabled)}
          />
        </div>
        <button
          type="button"
          className={styles.settingLink}
          onClick={() => navigate("organize")}
        >
          <span>
            <strong>自动整理</strong>
            <small>
              {dreaming.enabled
                ? `${formatSchedule(dreaming.schedule)} 运行`
                : "当前已暂停"}
            </small>
          </span>
          <ArrowRight size={16} />
        </button>
      </div>

      <section className={styles.settingsSection}>
        <h3>带走或带入记忆</h3>
        <div className={styles.transferRows}>
          <div>
            <div>
              <strong>澜台记忆包</strong>
              <span>用于迁移到另一台设备，可能包含记忆对应的原话片段；不含完整聊天记录和账号密钥。</span>
            </div>
            <div>
              <Button
                icon={<Download size={15} />}
                loading={packBusy === "export"}
                onClick={() => void exportPack()}
              >
                导出
              </Button>
              <Button
                icon={<Upload size={15} />}
                loading={packBusy === "inspect"}
                onClick={() => packInputRef.current?.click()}
              >
                从记忆包恢复
              </Button>
              <input
                hidden
                ref={packInputRef}
                type="file"
                accept=".zip,.lantai-memory.zip,application/zip"
                aria-label="选择澜台记忆包"
                onChange={(event) => void selectPack(event)}
              />
            </div>
          </div>
          <div>
            <div>
              <strong>其他 AI 的记忆</strong>
              <span>先在本机预览，再选择真正需要带入的内容。</span>
              {recentImport ? (
                <small>
                  最近从 {recentImport.provider} 导入{" "}
                  {recentImport.imported_count} 条
                </small>
              ) : null}
            </div>
            <div>
              {recentImport?.can_undo ? (
                <Popconfirm
                  title="撤销最近一次导入？"
                  description="只撤销这一批且之后没有被修改的记忆。"
                  okText="撤销导入"
                  cancelText="取消"
                  onConfirm={() => void undoImport(recentImport.batch_id)}
                >
                  <Button
                    icon={<RotateCcw size={15} />}
                    loading={importUndoing}
                  >
                    撤销最近导入
                  </Button>
                </Popconfirm>
              ) : null}
              <Button
                icon={<FileInput size={15} />}
                onClick={() => void openImport()}
              >
                从其他 AI 导入
              </Button>
            </div>
          </div>
        </div>
      </section>

      {archivedEntries.length ? (
        <section className={styles.settingsSection}>
          <Collapse
            ghost
            items={[
              {
                key: "archived",
                label: `已停止使用的记忆（${archivedEntries.length}）`,
                children: (
                  <div className={styles.archivedList}>
                    {archivedEntries.map((entry) => (
                      <div key={entry.id}>
                        <span>
                          <strong>
                            {toDisplaySentence(entry.content, entry.type) ||
                              entry.content}
                          </strong>
                          <small>
                            停止使用于{" "}
                            {formatTime(entry.archived_at || entry.updated_at)}
                          </small>
                        </span>
                        <div>
                          <Button
                            size="small"
                            onClick={() => void handleRestore(entry)}
                          >
                            恢复使用
                          </Button>
                          <Popconfirm
                            title="彻底删除这条记忆？"
                            description="原聊天和文件不会随之删除。"
                            okText="删除"
                            cancelText="取消"
                            onConfirm={() => void handleDelete(entry)}
                          >
                            <Button size="small" type="text" danger>
                              删除
                            </Button>
                          </Popconfirm>
                        </div>
                      </div>
                    ))}
                  </div>
                ),
              },
            ]}
          />
        </section>
      ) : null}

      <section className={styles.settingsSection}>
        <Collapse
          ghost
          items={[
            {
              key: "diagnostics",
              label: "帮助排查问题",
              children: (
                <div className={styles.diagnostics}>
                  <p>{engineStatus.summary}</p>
                  {engineStatus.rows.map((row) => (
                    <div key={row.key}>
                      <strong>{row.label}</strong>
                      <span>
                        {row.status} · {row.detail}
                      </span>
                    </div>
                  ))}
                </div>
              ),
            },
          ]}
        />
      </section>

      <section className={styles.dangerSection}>
        <div>
          <h3>删除所有长期记忆</h3>
          <p>会清除记忆和整理记录，聊天、文件、知识库和技能不会删除。</p>
        </div>
        <Button
          danger
          icon={<Trash2 size={15} />}
          onClick={() => void openClear()}
        >
          删除所有记忆
        </Button>
      </section>
    </section>
  );

  const renderContent = () => {
    if (view === "library") return renderLibrary();
    if (view === "topic") return renderTopic();
    if (view === "review") {
      return (
        <UnderstandingProjectionPanel
          agentId={selectedAgent}
          facts={suggestions}
          hypotheses={understanding.hypotheses}
          deferredIds={deferredReviewIds}
          onBack={() => navigate("home")}
          onDefer={(id) =>
            persistDeferredReviews(new Set([...deferredReviewIds, id]))
          }
          onResetDeferred={() => persistDeferredReviews(new Set())}
          onFactConfirm={(fact, scope) => void handleFactConfirm(fact, scope)}
          onFactReject={(fact) => void handleFactReject(fact)}
          onSourceDeleted={() => void loadMemories(true)}
          onHypothesisFeedback={(id, verdict) =>
            void handleHypothesis(id, verdict)
          }
        />
      );
    }
    if (view === "settings") return renderSettings();
    if (view === "organize") return renderOrganize();
    return renderHome();
  };

  return (
    <div className={styles.memoryPage}>
      <PageHeader
        items={[{ title: "工作区" }, { title: "记忆" }]}
        description="澜台会在日常对话中慢慢理解你；项目资料和第三方内容不会自动当成你本人，你也可以随时查看、纠正或暂停。"
        extra={
          <div className={styles.headerActions}>
            {reviewCount ? (
              <Button onClick={() => navigate("review")}>
                {reviewCount} 项需要核对
              </Button>
            ) : null}
            <Tooltip title="刷新记忆">
              <Button
                type="text"
                aria-label="刷新记忆"
                icon={<RefreshCw size={15} />}
                onClick={() => void loadMemories()}
              />
            </Tooltip>
            <Tooltip title="记忆设置">
              <Button
                type="text"
                aria-label="记忆设置"
                icon={<Settings2 size={17} />}
                onClick={() => navigate("settings")}
              />
            </Tooltip>
          </div>
        }
      />
      <main className={styles.memoryContent}>
        {view === "home" || view === "library" ? (
          <nav className={styles.primaryNav} aria-label="记忆页面">
            <button
              type="button"
              data-active={view === "home"}
              onClick={() => navigate("home")}
            >
              概览
            </button>
            <button
              type="button"
              data-active={view === "library"}
              onClick={() => navigate("library")}
            >
              全部记忆
            </button>
            <span>
              {lastSyncedAt
                ? `更新于 ${new Date(lastSyncedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
                : ""}
            </span>
          </nav>
        ) : null}
        {loadError ? (
          <Alert
            type="error"
            showIcon
            message="记忆没有加载完成"
            description={loadError}
            action={
              <Button onClick={() => void loadMemories()}>重新加载</Button>
            }
          />
        ) : loading ? (
          <div className={styles.loadingState} aria-label="正在读取记忆">
            <Skeleton active paragraph={{ rows: 8 }} />
          </div>
        ) : (
          <>
            {Object.keys(optionalLoadErrors).length ? (
              <Alert
                type="warning"
                showIcon
                message="部分辅助信息没有更新"
                description={`未更新：${Object.keys(optionalLoadErrors)
                  .map(
                    (key) =>
                      OPTIONAL_MEMORY_PANEL_LABELS[key as OptionalMemoryPanel],
                  )
                  .join("、")}。核心记忆仍可查看；失败区域保留上次结果（若有）。`}
                action={
                  <Button onClick={() => void loadOptionalMemories()}>
                    重试辅助信息
                  </Button>
                }
              />
            ) : null}
            {renderContent()}
          </>
        )}
      </main>

      <Modal
        title="告诉澜台一件事"
        open={addOpen}
        okText="记住"
        cancelText="取消"
        okButtonProps={{ disabled: !addText.trim(), loading: addSaving }}
        onOk={() => void handleAdd()}
        onCancel={() => setAddOpen(false)}
      >
        <p className={styles.modalHint}>
          直接写你希望以后继续使用的信息，分类由澜台处理。
        </p>
        <Input.TextArea
          autoFocus
          value={addText}
          onChange={(event) => setAddText(event.target.value)}
          rows={5}
          maxLength={600}
          showCount
          aria-label="希望澜台记住的内容"
          placeholder="例如：正式方案先给明确推荐和理由，不要无限扩展选项。"
        />
      </Modal>

      <Modal
        title="修改记忆"
        open={Boolean(editingEntry)}
        okText="保存修改"
        cancelText="取消"
        okButtonProps={{ disabled: !editText.trim(), loading: editSaving }}
        onOk={() => void handleEdit()}
        onCancel={closeEdit}
      >
        {editDraftRestored ? (
          <Alert
            type="info"
            showIcon
            message="已恢复你离开此页面前未保存的修改"
          />
        ) : null}
        <p className={styles.modalHint}>
          修改后，旧说法会停止使用，相关主题也会随之更新。
        </p>
        <Input.TextArea
          value={editText}
          onChange={handleEditTextChange}
          rows={5}
          maxLength={600}
          showCount
          aria-label="记忆内容"
        />
      </Modal>

      <Modal
        title={selectedTopic ? `纠正“${selectedTopic.title}”` : "纠正这项理解"}
        open={correctionOpen}
        okText="提交正确说法"
        cancelText="取消"
        okButtonProps={{
          disabled: !correctionText.trim(),
          loading: correctionSaving,
        }}
        onOk={() => void handleCorrection()}
        onCancel={() => {
          if (!correctionSaving) setCorrectionOpen(false);
        }}
      >
        <p className={styles.modalHint}>
          写下现在准确的情况。澜台会把它作为你的明确纠正，并优先显示在这个主题中；原始聊天和文件不会删除。
        </p>
        <Input.TextArea
          autoFocus
          rows={5}
          maxLength={600}
          showCount
          value={correctionText}
          onChange={(event) => setCorrectionText(event.target.value)}
          aria-label="正确的情况"
          placeholder="正确的情况是……"
        />
      </Modal>

      <Modal
        title="从其他 AI 带入记忆"
        width={760}
        open={importOpen}
        footer={null}
        onCancel={closeImport}
      >
        {importStage === "paste" ? (
          <div className={styles.importFlow}>
            <Alert
              type="info"
              showIcon
              message="先预览，后导入"
              description="粘贴内容只在本机解析；不会自动覆盖现有记忆。"
            />
            <div className={styles.importPromptRow}>
              <span>
                {importPrompt ? "迁移提示词已准备好" : "正在准备迁移提示词"}
              </span>
              <Button
                icon={<Copy size={15} />}
                disabled={!importPrompt}
                onClick={() => void copyImportPrompt()}
              >
                复制给其他 AI
              </Button>
            </div>
            <Select
              value={importProvider}
              options={IMPORT_PROVIDERS}
              onChange={setImportProvider}
              aria-label="记忆来源"
            />
            <Input.TextArea
              rows={9}
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
              aria-label="其他 AI 返回的记忆"
              placeholder="把其他 AI 整理出的记忆粘贴到这里"
            />
            <div className={styles.importFooter}>
              <Button onClick={closeImport}>取消</Button>
              <Button
                type="primary"
                loading={importBusy}
                disabled={!importText.trim()}
                onClick={() => void previewImport()}
              >
                分析并预览
              </Button>
            </div>
          </div>
        ) : null}
        {importStage === "review" && importPreview ? (
          <div className={styles.importFlow}>
            <div className={styles.importSummary}>
              <strong>选择要带入的内容</strong>
              <span>
                已推荐 {importPreview.stats.recommended}{" "}
                条；冲突和一次性内容不会默认选中。
              </span>
            </div>
            <div className={styles.importCandidates}>
              {importPreview.candidates.map((candidate) => (
                <label key={candidate.id} data-disabled={!candidate.selectable}>
                  <Checkbox
                    checked={importSelected.includes(candidate.id)}
                    disabled={!candidate.selectable}
                    aria-label={candidate.content}
                    onChange={(event) =>
                      setImportSelected((current) =>
                        event.target.checked
                          ? [...new Set([...current, candidate.id])]
                          : current.filter((id) => id !== candidate.id),
                      )
                    }
                  />
                  <span>
                    <strong>{candidate.content}</strong>
                    <small>{candidate.reason}</small>
                  </span>
                </label>
              ))}
            </div>
            <div className={styles.importFooter}>
              <Button onClick={() => setImportStage("paste")}>返回修改</Button>
              <Button
                type="primary"
                loading={importBusy}
                disabled={!importSelected.length}
                onClick={() => void commitImport()}
              >
                导入选中的 {importSelected.length} 条
              </Button>
            </div>
          </div>
        ) : null}
        {importStage === "done" && importResult ? (
          <div className={styles.importDone}>
            <Check size={24} />
            <h3>记忆导入完成</h3>
            <p>
              已带入 {importResult.imported_count}{" "}
              条；现有其他记忆没有被整批替换。
            </p>
            <Button type="primary" onClick={() => setImportOpen(false)}>
              完成
            </Button>
          </div>
        ) : null}
      </Modal>

      <Modal
        title="采用这份记忆包"
        open={Boolean(packFile)}
        okText="采用记忆包"
        cancelText="取消"
        okButtonProps={{
          disabled:
            !packReport || Boolean(packReport.decision_conflicts?.length) || packConfirmation !== MEMORY_PACK_ADOPT_CONFIRMATION,
          loading: packBusy === "adopt",
        }}
        onOk={() => void adoptPack()}
        onCancel={() => {
          if (packBusy !== "adopt") {
            setPackFile(null);
            setPackReport(null);
          }
        }}
      >
        {packReport ? (
          <div className={styles.packPreview}>
            <p>来源智能体：{packReport.source_agent_id || "未知"}</p>
            <p>
              {packReport.contains_original_sources
                ? "包含记忆所引用的原文消息快照，采用后可从记忆事实打开查看。"
                : "未包含可追溯的原文消息快照；旧记忆可能只能显示摘要。"}
            </p>
            {packReport.decision_conflicts?.map((conflict) => (
              <p key={conflict} role="alert">{conflict}。请先处理记忆包中的旧内容。</p>
            ))}
            <p>
              将替换：
              {packReport.would_replace
                .map((item) => PACK_CATEGORY_LABELS[item] || item)
                .join("、") || "无"}
            </p>
            <p>
              {packReport.would_remove.length
                ? `包内缺少 ${packReport.would_remove.length} 类，采用后将移除现有内容`
                : "包内内容完整，不会因缺少类别删除现有内容"}
            </p>
            <Input
              value={packConfirmation}
              onChange={(event) => setPackConfirmation(event.target.value)}
              aria-label="输入“采用记忆包”以确认"
              placeholder={MEMORY_PACK_ADOPT_CONFIRMATION}
            />
          </div>
        ) : (
          <Skeleton active paragraph={{ rows: 4 }} />
        )}
      </Modal>

      <Modal
        title="删除所有长期记忆"
        open={clearOpen}
        okText="确认删除所有记忆"
        cancelText="取消"
        okButtonProps={{
          danger: true,
          disabled:
            !clearPreview ||
            clearConfirmation !==
              (clearPreview?.confirmation_text || MEMORY_CLEAR_CONFIRMATION),
          loading: clearBusy,
        }}
        onOk={() => void clearAll()}
        onCancel={() => {
          if (!clearBusy) setClearOpen(false);
        }}
      >
        {clearPreview ? (
          <div className={styles.clearPreview}>
            <Alert
              type="warning"
              showIcon
              message={`将清除约 ${clearPreview.active_items} 项长期记忆`}
              description="聊天记录、文件、知识库和技能不会删除。"
            />
            <Checkbox
              checked={clearBackup}
              onChange={(event) => setClearBackup(event.target.checked)}
            >
              删除前创建一份本地恢复备份
            </Checkbox>
            <Input
              value={clearConfirmation}
              onChange={(event) => setClearConfirmation(event.target.value)}
              placeholder={
                clearPreview.confirmation_text || MEMORY_CLEAR_CONFIRMATION
              }
              aria-label="输入删除确认文字"
            />
          </div>
        ) : (
          <Skeleton active paragraph={{ rows: 3 }} />
        )}
      </Modal>
    </div>
  );
}
