import {
  ArrowRightOutlined,
  BookOutlined,
  CheckCircleFilled,
  EditOutlined,
  FileTextOutlined,
  FolderOutlined,
  GlobalOutlined,
  InboxOutlined,
  LinkOutlined,
  ReloadOutlined,
  SearchOutlined,
  UploadOutlined,
  WarningFilled,
  ReadOutlined,
  CompassOutlined,
  DeleteOutlined,
  PlusOutlined,
  CloseOutlined,
} from "@ant-design/icons";
import { Checkbox, Drawer, Empty, Input, Modal, Radio, Tag } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Button } from "@/vendor/lantaiDesign";
import { PageHeader } from "@/components/PageHeader";
import {
  CREATE_NEW_CHAT_CONTEXT_KEY,
  type ChatKnowledgeSourceReference,
} from "@/pages/Chat/chatSessionEvents";
import {
  workspaceApi,
  type WikiSourceUploadResult,
} from "../../../api/modules/workspace";
import type {
  WikiSearchHit,
  WikiSourceContent,
  WikiSourceInfo,
} from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import { createAsyncStateScope } from "../../../utils/latestRequest";
import { FileEditor, useAgentsData } from "./components";
import {
  buildKnowledgeItems,
  knowledgeKindLabel,
  knowledgeStatusLabel,
  summarizeKnowledge,
  type KnowledgeListItem,
  type KnowledgeItemStatus,
} from "./presentation";
import styles from "./knowledge.module.less";
import ReadingPanel from "./ReadingPanel";
import ChannelDiscovery from "./ChannelDiscovery";
import RecycleBin from "./RecycleBin";
import ResearchPanel from "./ResearchPanel";

type ContentFilter = "all" | "page" | "source" | "attention";
type DetailKind = "page" | "source" | null;
type AddMode = "file" | "web" | "text";
type SourceEvidence = Pick<WikiSearchHit, "source_revision" | "source_text_hash" | "source_start" | "source_end">;
type LibraryArea = "knowledge" | "reading" | "research" | "channels" | "trash";
type KnowledgeImportStatus =
  | "processing"
  | "accepted"
  | "duplicate"
  | "attention"
  | "failed"
  | "unknown";

interface KnowledgeImportReceipt {
  key: string;
  mode: "file" | "web";
  label: string;
  status: KnowledgeImportStatus;
  detail: string;
  payload: File | string;
  retryable: boolean;
}

const WIKI_SOURCE_ACCEPT = [
  ".zip",
  ".md",
  ".markdown",
  ".txt",
  ".csv",
  ".json",
  ".jsonl",
  ".yaml",
  ".yml",
  ".xml",
  ".html",
  ".htm",
  ".log",
  ".py",
  ".js",
  ".jsx",
  ".ts",
  ".tsx",
  ".css",
  ".less",
  ".pdf",
  ".docx",
  ".pptx",
  ".xlsx",
  ".doc",
  ".ppt",
  ".xls",
  ".png",
  ".jpg",
  ".jpeg",
  ".webp",
  ".gif",
  ".bmp",
  ".tif",
  ".tiff",
  ".heic",
  ".heif",
].join(",");

const statusClassName = (status: KnowledgeItemStatus) => {
  if (status === "processing") return styles.statusProcessing;
  if (status === "saved") return styles.statusSaved;
  if (status === "attention") return styles.statusAttention;
  if (status === "failed") return styles.statusFailed;
  return styles.statusReady;
};

const formatUpdatedAt = (timestamp: number) => {
  if (!timestamp || Number.isNaN(timestamp)) return "更新时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
};

const importErrorMessage = (error: unknown) =>
  error instanceof Error && error.message.trim()
    ? error.message.trim()
    : "服务没有返回可读的失败原因";

const importRequestOutcome = (
  receipt: KnowledgeImportReceipt,
  result: WikiSourceUploadResult,
): KnowledgeImportReceipt => {
  const failedDetails = (result.details ?? []).filter(
    (detail) => detail.status === "failed",
  );
  if (failedDetails.length) {
    const failedNames = failedDetails
      .map((detail) =>
        [detail.filename, detail.message].filter(Boolean).join("："),
      )
      .join("；");
    return {
      ...receipt,
      status: "attention",
      detail: `已接收部分内容；未处理：${failedNames}。请将失败文件单独选择后再添加。`,
      retryable: false,
    };
  }
  if (result.imported.length > 0) {
    return {
      ...receipt,
      status: "accepted",
      detail: "已保存来源；整理和索引状态以资料列表为准。",
      retryable: false,
    };
  }
  if (result.skipped.length > 0) {
    return {
      ...receipt,
      status: "duplicate",
      detail: "内容已存在，本次没有重复添加。",
      retryable: false,
    };
  }
  return {
    ...receipt,
    status: "unknown",
    detail:
      "请求已返回，但没有提供接收结果。请先刷新资料列表核实，不要直接重复添加。",
    retryable: false,
  };
};

const importFailureOutcome = (
  receipt: KnowledgeImportReceipt,
  error: unknown,
): KnowledgeImportReceipt => {
  const resultUnknown =
    (error instanceof TypeError && /fetch|network/i.test(error.message)) ||
    (error instanceof DOMException && error.name === "AbortError");
  return {
    ...receipt,
    status: resultUnknown ? "unknown" : "failed",
    detail: resultUnknown
      ? `响应中断，不能确定是否已接收：${importErrorMessage(error)}。请先刷新资料列表核实。`
      : importErrorMessage(error),
    retryable: !resultUnknown,
  };
};

const importReceiptStatusLabel = (receipt: KnowledgeImportReceipt) => {
  if (receipt.status === "processing") return "正在处理";
  if (receipt.status === "accepted") return "已接收，等待整理";
  if (receipt.status === "duplicate") return "已存在，未重复添加";
  if (receipt.status === "attention") return "部分内容需处理";
  if (receipt.status === "unknown") return "结果待核实";
  return receipt.mode === "web" ? "读取失败" : "添加失败";
};

const importReceiptClassName = (status: KnowledgeImportStatus) => {
  if (status === "accepted" || status === "duplicate") {
    return styles.importReceiptAccepted;
  }
  if (status === "failed") return styles.importReceiptFailed;
  if (status === "attention" || status === "unknown") {
    return styles.importReceiptAttention;
  }
  return styles.importReceiptProcessing;
};

export default function WorkspacePage() {
  const [activeArea, setActiveArea] = useState<LibraryArea>("knowledge");
  const navigate = useNavigate();
  const location = useLocation();
  const { message } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const mutationScopeRef = useRef(createAsyncStateScope());
  const previousAgentRef = useRef(selectedAgent);
  const messageRef = useRef(message);
  messageRef.current = message;
  const fileInputRef = useRef<HTMLInputElement>(null);
  const sourceLoadTokenRef = useRef(0);
  const citationMarkRef = useRef<HTMLElement>(null);
  const detailPaneRef = useRef<HTMLElement>(null);
  const citationRequestRef = useRef("");
  const citationBlockedAgentRef = useRef("");
  const locationRef = useRef(location);
  const navigateRef = useRef(navigate);
  locationRef.current = location;
  navigateRef.current = navigate;
  const [filter, setFilter] = useState<ContentFilter>("all");
  const [detailKind, setDetailKind] = useState<DetailKind>(null);
  const [selectedSource, setSelectedSource] = useState<WikiSourceInfo | null>(
    null,
  );
  const [searchValue, setSearchValue] = useState("");
  const [searchSubmitted, setSearchSubmitted] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [addMode, setAddMode] = useState<AddMode>("file");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [attentionOpen, setAttentionOpen] = useState(false);
  const [urlValue, setUrlValue] = useState("");
  const [importing, setImporting] = useState(false);
  const [importProgress, setImportProgress] = useState("");
  const [importReceipts, setImportReceipts] = useState<
    KnowledgeImportReceipt[]
  >([]);
  const importReceiptSequenceRef = useRef(0);
  const [textTitle, setTextTitle] = useState("");
  const [textValue, setTextValue] = useState("");
  const [collectionFilter, setCollectionFilter] = useState("all");
  const [collectionOpen, setCollectionOpen] = useState(false);
  const [collectionName, setCollectionName] = useState("");
  const [collectionDescription, setCollectionDescription] = useState("");
  const [editingCollectionId, setEditingCollectionId] = useState<string | null>(
    null,
  );
  const [selectedSourcePaths, setSelectedSourcePaths] = useState<string[]>([]);
  const [selectedPageNames, setSelectedPageNames] = useState<string[]>([]);
  const [scopeMode, setScopeMode] = useState<"exact" | "expanded">("exact");
  const [sourceContent, setSourceContent] = useState<WikiSourceContent | null>(
    null,
  );
  const [sourceContentError, setSourceContentError] = useState("");
  const [sourceAnchor, setSourceAnchor] = useState<{
    start: number;
    end: number;
  } | null>(null);
  const [sourceEvidence, setSourceEvidence] = useState<SourceEvidence | null>(null);
  const sourceCodepoints = useMemo(
    () => Array.from(sourceContent?.content ?? ""),
    [sourceContent?.content],
  );
  const [sourceContentLoading, setSourceContentLoading] = useState(false);
  const [sourceTitleDraft, setSourceTitleDraft] = useState("");
  const [savingSource, setSavingSource] = useState(false);

  const {
    files,
    sources,
    collections = [],
    graph,
    queueSummary,
    lint,
    insights,
    review,
    schema,
    purpose,
    searchResults,
    searchLoading,
    selectedFile,
    fileContent,
    loading,
    deleting,
    workspacePath,
    hasChanges,
    purposeContent,
    purposeHasChanges,
    setFileContent,
    setPurposeContent,
    fetchFiles,
    handleSearch,
    handleRunIngest,
    handleResolveReview,
    handleFileClick,
    handleSave,
    handleReset,
    handleDelete,
    handlePurposeSave,
    handlePurposeReset,
  } = useAgentsData();

  const items = useMemo(
    () => buildKnowledgeItems(files, sources),
    [files, sources],
  );
  const summary = useMemo(
    () => summarizeKnowledge(items, queueSummary, review),
    [items, queueSummary, review],
  );

  const pendingReviews = useMemo(
    () => review?.items.filter((item) => item.status === "pending") ?? [],
    [review],
  );
  const failedSources = useMemo(
    () => items.filter((item) => item.status === "failed"),
    [items],
  );
  const attentionSources = useMemo(
    () =>
      items.filter(
        (item) => item.status === "attention" || item.status === "saved",
      ),
    [items],
  );
  const unlistedQueueFailures = Math.max(
    0,
    queueSummary.failed - failedSources.length,
  );
  const attentionCount =
    pendingReviews.length +
    attentionSources.length +
    failedSources.length +
    unlistedQueueFailures;

  const visibleItems = useMemo(() => {
    let filtered = items;
    if (filter === "page")
      filtered = items.filter((item) => item.kind === "page");
    if (filter === "source")
      filtered = items.filter((item) => item.kind === "source");
    if (filter === "attention") {
      filtered = items.filter(
        (item) => item.status === "attention" || item.status === "failed",
      );
    }
    if (collectionFilter !== "all") {
      filtered = filtered.filter(
        (item) =>
          item.kind === "source" &&
          item.source?.collection_ids?.includes(collectionFilter),
      );
    }
    return filtered;
  }, [collectionFilter, filter, items]);

  const openPage = (item: KnowledgeListItem) => {
    if (!item.page) return;
    setSelectedSource(null);
    setDetailKind("page");
    void handleFileClick(item.page);
  };

  const openSource = useCallback(
    (
      source: WikiSourceInfo,
      anchor: { start: number; end: number } | null = null,
      evidence: SourceEvidence | null = null,
    ) => {
      const loadToken = sourceLoadTokenRef.current + 1;
      sourceLoadTokenRef.current = loadToken;
      setSelectedSource(source);
      setDetailKind("source");
      setSourceTitleDraft(source.display_title || source.filename);
      setSourceAnchor(anchor);
      setSourceEvidence(evidence);
      setSourceContent(null);
      setSourceContentError("");
      setSourceContentLoading(true);
      const contentRequest = evidence
        ? workspaceApi.loadWikiSourceContent(source.path, evidence)
        : workspaceApi.loadWikiSourceContent(source.path);
      void contentRequest
        .then((content) => {
          if (sourceLoadTokenRef.current === loadToken)
            setSourceContent(content);
        })
        .catch((error) => {
          if (sourceLoadTokenRef.current !== loadToken) return;
          console.error("Failed to load knowledge source content", error);
          setSourceContentError(
            "资料正文读取失败；资料本身没有被删除，可以重新读取。",
          );
          messageRef.current.error("暂时无法打开这份资料的正文。");
        })
        .finally(() => {
          if (sourceLoadTokenRef.current === loadToken) {
            setSourceContentLoading(false);
          }
        });
    },
    [],
  );

  useEffect(() => {
    const mutationScope = mutationScopeRef.current;
    const agentChanged = previousAgentRef.current !== selectedAgent;
    if (agentChanged) {
      Modal.destroyAll();
      citationBlockedAgentRef.current = selectedAgent;
      const currentLocation = locationRef.current;
      const params = new URLSearchParams(currentLocation.search);
      if (params.has("source")) {
        ["source", "start", "end", "rev"].forEach((key) => params.delete(key));
        navigateRef.current(
          `${currentLocation.pathname}${params.size ? `?${params.toString()}` : ""}`,
          { replace: true },
        );
      }
    }
    previousAgentRef.current = selectedAgent;
    mutationScope.invalidate();
    sourceLoadTokenRef.current += 1;
    citationRequestRef.current = "";
    setDetailKind(null);
    setSelectedSource(null);
    setSelectedSourcePaths([]);
    setSourceContent(null);
    setSourceContentError("");
    setSourceAnchor(null);
    setSourceContentLoading(false);
    setCollectionFilter("all");
    setSearchValue("");
    setSearchSubmitted(false);
    setAttentionOpen(false);
    setAdvancedOpen(false);
    setAddOpen(false);
    setImportReceipts([]);
    setUrlValue("");
    setImportProgress("");
    setCollectionOpen(false);
    setImporting(false);
    setSavingSource(false);
    return () => mutationScope.invalidate();
  }, [selectedAgent]);

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const sourcePath = params.get("source")?.trim();
    if (!sourcePath) {
      citationRequestRef.current = "";
      citationBlockedAgentRef.current = "";
      return;
    }
    if (citationBlockedAgentRef.current === selectedAgent) return;
    const source = sources.find((item) => item.path === sourcePath);
    if (!source) return;
    const revision = params.get("rev")?.trim();
    const citationRequest = `${sourcePath}\u0000${params.get("start") || ""}\u0000${params.get("end") || ""}\u0000${revision || ""}`;
    if (citationRequestRef.current === citationRequest) return;
    citationRequestRef.current = citationRequest;
    if (revision && source.sha256 && revision !== source.sha256) {
      openSource(source);
      messageRef.current.warning(
        "这份资料已经更新，当前显示的是最新版本，原引用位置可能已变化。",
      );
      return;
    }
    const start = Math.max(0, Number(params.get("start")) || 0);
    const end = Math.max(start, Number(params.get("end")) || start);
    openSource(source, { start, end });
  }, [location.search, openSource, selectedAgent, sources]);

  useEffect(() => {
    if (!sourceContent || !sourceAnchor || !citationMarkRef.current) return;
    citationMarkRef.current.scrollIntoView?.({ block: "center" });
  }, [sourceAnchor, sourceContent]);

  useEffect(() => {
    if (detailKind && window.matchMedia?.("(max-width: 980px)")?.matches) {
      detailPaneRef.current?.scrollIntoView?.({ block: "start" });
    }
  }, [detailKind, selectedFile?.filename, selectedSource?.path]);

  const askWithSources = (selectedSources: WikiSourceInfo[]) => {
    if (!selectedSources.length) return;
    const references: ChatKnowledgeSourceReference[] = selectedSources.map(
      (source) => ({
        path: source.path,
        filename: source.display_title || source.filename,
        kind: source.kind,
      }),
    );
    window.sessionStorage.setItem(
      CREATE_NEW_CHAT_CONTEXT_KEY,
      JSON.stringify({
        knowledgeSources: references,
        knowledgeScopeMode: scopeMode,
        ...(references.length === 1 ? { knowledgeSource: references[0] } : {}),
      }),
    );
    navigate("/chat?lantaiColdStart=1");
  };

  const selectedSources = useMemo(
    () => sources.filter((source) => selectedSourcePaths.includes(source.path)),
    [selectedSourcePaths, sources],
  );
  const selectedKnowledgeItems = useMemo(
    () =>
      items.filter(
        (item) =>
          (item.source && selectedSourcePaths.includes(item.source.path)) ||
          (item.page && selectedPageNames.includes(item.page.filename)),
      ),
    [items, selectedPageNames, selectedSourcePaths],
  );

  const openItem = (item: KnowledgeListItem) => {
    if (item.kind === "page") openPage(item);
    else if (item.source) openSource(item.source);
  };

  const openLinkedPage = (filename: string) => {
    const page = files.find((file) => file.filename === filename);
    if (!page) return;
    openPage(
      items.find((item) => item.page?.filename === page.filename) ?? {
        id: `page:${page.filename}`,
        kind: "page",
        title: page.title,
        description: page.summary,
        path: page.path,
        updatedAt: page.updated_at,
        status: "ready",
        page,
      },
    );
  };

  const openSearchHit = (hit: WikiSearchHit) => {
    if (hit.kind === "page") {
      const page = files.find((file) => file.filename === hit.filename);
      if (page) openLinkedPage(page.filename);
      return;
    }
    const source = sources.find(
      (item) => item.path === hit.path || item.filename === hit.filename,
    );
    if (source) {
      const start = hit.source_start;
      const end = hit.source_end;
      const hasEvidence = Boolean(hit.source_text_hash) &&
        start != null && end != null && end > start;
      openSource(
        source,
        hasEvidence ? { start, end } : null,
        hasEvidence ? hit : null,
      );
    }
  };

  const submitSearch = useCallback(
    (value: string) => {
      const normalized = value.trim();
      setSearchValue(value);
      setSearchSubmitted(Boolean(normalized));
      void handleSearch(normalized);
    },
    [handleSearch],
  );

  useEffect(() => {
    const query = new URLSearchParams(location.search).get("query")?.trim();
    if (!query) return;
    submitSearch(query);
  }, [location.search, submitSearch]);

  const clearSearchIfEmpty = (value: string) => {
    setSearchValue(value);
    if (!value.trim()) {
      setSearchSubmitted(false);
      void handleSearch("");
    }
  };

  const handleFileUpload = async (
    event: React.ChangeEvent<HTMLInputElement>,
  ) => {
    const selectedFiles = Array.from(event.target.files || []);
    if (!selectedFiles.length) return;
    const ownsScope = mutationScopeRef.current.capture();
    const receipts = selectedFiles.map((file) => ({
      key: `file-${(importReceiptSequenceRef.current += 1)}`,
      mode: "file" as const,
      label: file.name,
      status: "processing" as const,
      detail: "等待接收",
      payload: file,
      retryable: false,
    }));
    setImportReceipts((current) => [
      ...current.filter((receipt) => receipt.mode !== "file"),
      ...receipts,
    ]);
    setImporting(true);
    let imported = 0;
    let skipped = 0;
    let failed = 0;
    let unknown = 0;
    let attention = 0;
    try {
      for (const [index, receipt] of receipts.entries()) {
        if (!ownsScope()) return;
        const file = receipt.payload as File;
        setImportProgress(
          `正在处理 ${index + 1}/${selectedFiles.length}：${file.name}`,
        );
        if (file.size > 100 * 1024 * 1024) {
          failed += 1;
          setImportReceipts((current) =>
            current.map((item) =>
              item.key === receipt.key
                ? {
                    ...item,
                    status: "failed",
                    detail: "文件超过 100MB，请缩小后重新选择。",
                    retryable: false,
                  }
                : item,
            ),
          );
          continue;
        }
        try {
          const result = await workspaceApi.uploadWikiSource(file);
          if (!ownsScope()) return;
          imported += result.imported.length;
          skipped += result.skipped.length;
          const outcome = importRequestOutcome(receipt, result);
          if (outcome.status === "unknown") unknown += 1;
          if (outcome.status === "attention") attention += 1;
          setImportReceipts((current) =>
            current.map((item) =>
              item.key === receipt.key
                ? {
                    ...outcome,
                    ...item,
                    status: outcome.status,
                    detail: outcome.detail,
                    retryable: outcome.retryable,
                  }
                : item,
            ),
          );
        } catch (error) {
          const outcome = importFailureOutcome(receipt, error);
          if (outcome.status === "unknown") unknown += 1;
          else failed += 1;
          console.error("Failed to add knowledge source", error);
          setImportReceipts((current) =>
            current.map((item) =>
              item.key === receipt.key
                ? {
                    ...outcome,
                    ...item,
                    status: outcome.status,
                    detail: outcome.detail,
                    retryable: outcome.retryable,
                  }
                : item,
            ),
          );
        }
      }
      if (!ownsScope()) return;
      const resultMessage = `已接收 ${imported} 份资料${skipped ? `，${skipped} 份重复内容未再添加` : ""}${attention ? `，${attention} 份需处理` : ""}${failed ? `，${failed} 份失败` : ""}${unknown ? `，${unknown} 份结果待核实` : ""}。整理状态以资料列表为准。`;
      if (failed && !imported && !skipped) {
        message.error(resultMessage);
      } else if (failed || unknown || attention) {
        message.warning(resultMessage);
      } else {
        message.success(resultMessage);
      }
      await fetchFiles();
      if (!ownsScope()) return;
    } finally {
      if (ownsScope()) {
        setImporting(false);
        setImportProgress("");
        event.target.value = "";
      }
    }
  };

  const handleUrlImport = async () => {
    const urls = urlValue
      .split(/\s+/)
      .map((url) => url.trim())
      .filter(Boolean);
    if (!urls.length || urls.some((url) => !/^https?:\/\//i.test(url))) {
      message.warning("每个网页地址都需要以 http:// 或 https:// 开头。");
      return;
    }
    const ownsScope = mutationScopeRef.current.capture();
    const receipts = urls.map((url) => ({
      key: `web-${(importReceiptSequenceRef.current += 1)}`,
      mode: "web" as const,
      label: url,
      status: "processing" as const,
      detail: "等待读取",
      payload: url,
      retryable: false,
    }));
    setImportReceipts((current) => [
      ...current.filter((receipt) => receipt.mode !== "web"),
      ...receipts,
    ]);
    setImporting(true);
    let imported = 0;
    let skipped = 0;
    let failed = 0;
    let unknown = 0;
    let attention = 0;
    const failedUrls: string[] = [];
    try {
      for (const [index, receipt] of receipts.entries()) {
        if (!ownsScope()) return;
        const url = receipt.payload as string;
        setImportProgress(`正在读取 ${index + 1}/${urls.length} 个网页`);
        try {
          const result = await workspaceApi.importWikiUrl(url);
          if (!ownsScope()) return;
          imported += result.imported.length;
          skipped += result.skipped.length;
          const outcome = importRequestOutcome(receipt, result);
          if (outcome.status === "unknown") unknown += 1;
          if (outcome.status === "attention") attention += 1;
          setImportReceipts((current) =>
            current.map((item) =>
              item.key === receipt.key
                ? {
                    ...outcome,
                    ...item,
                    status: outcome.status,
                    detail: outcome.detail,
                    retryable: outcome.retryable,
                  }
                : item,
            ),
          );
        } catch (error) {
          const outcome = importFailureOutcome(receipt, error);
          if (outcome.status === "unknown") unknown += 1;
          else failed += 1;
          failedUrls.push(url);
          console.error("Failed to add knowledge URL", error);
          setImportReceipts((current) =>
            current.map((item) =>
              item.key === receipt.key
                ? {
                    ...outcome,
                    ...item,
                    status: outcome.status,
                    detail: outcome.detail,
                    retryable: outcome.retryable,
                  }
                : item,
            ),
          );
        }
      }
      if (!ownsScope()) return;
      setUrlValue(failedUrls.join("\n"));
      const resultMessage = `已接收 ${imported} 个网页${skipped ? `，${skipped} 个重复网页未再添加` : ""}${attention ? `，${attention} 个需处理` : ""}${failed ? `，${failed} 个读取失败` : ""}${unknown ? `，${unknown} 个结果待核实` : ""}。整理状态以资料列表为准。`;
      if (failed && !imported && !skipped) {
        message.error(resultMessage);
      } else if (failed || unknown || attention) {
        message.warning(resultMessage);
      } else {
        message.success(resultMessage);
      }
      await fetchFiles();
      if (!ownsScope()) return;
    } finally {
      if (ownsScope()) {
        setImporting(false);
        setImportProgress("");
      }
    }
  };

  const retryFailedImports = async () => {
    const failedReceipts = importReceipts.filter(
      (receipt) =>
        receipt.mode === addMode &&
        receipt.status === "failed" &&
        receipt.retryable,
    );
    if (!failedReceipts.length) return;
    const ownsScope = mutationScopeRef.current.capture();
    setImporting(true);
    let accepted = 0;
    const stillFailedUrls: string[] = [];
    try {
      for (const [index, receipt] of failedReceipts.entries()) {
        if (!ownsScope()) return;
        setImportProgress(
          `正在重试 ${index + 1}/${failedReceipts.length}：${receipt.label}`,
        );
        setImportReceipts((current) =>
          current.map((item) =>
            item.key === receipt.key
              ? {
                  ...item,
                  status: "processing",
                  detail: "正在重试",
                  retryable: false,
                }
              : item,
          ),
        );
        try {
          const result =
            receipt.mode === "file"
              ? await workspaceApi.uploadWikiSource(receipt.payload as File)
              : await workspaceApi.importWikiUrl(receipt.payload as string);
          if (!ownsScope()) return;
          accepted += result.imported.length + result.skipped.length;
          setImportReceipts((current) =>
            current.map((item) =>
              item.key === receipt.key
                ? importRequestOutcome(item, result)
                : item,
            ),
          );
        } catch (error) {
          if (!ownsScope()) return;
          if (receipt.mode === "web") {
            stillFailedUrls.push(receipt.payload as string);
          }
          setImportReceipts((current) =>
            current.map((item) =>
              item.key === receipt.key
                ? importFailureOutcome(item, error)
                : item,
            ),
          );
        }
      }
      if (!ownsScope()) return;
      if (addMode === "web") setUrlValue(stillFailedUrls.join("\n"));
      if (accepted > 0) await fetchFiles();
    } finally {
      if (ownsScope()) {
        setImporting(false);
        setImportProgress("");
      }
    }
  };

  const visibleImportReceipts = importReceipts.filter(
    (receipt) => receipt.mode === addMode,
  );
  const retryableImportCount = visibleImportReceipts.filter(
    (receipt) => receipt.status === "failed" && receipt.retryable,
  ).length;

  const handleTextImport = async () => {
    const content = textValue.trim();
    if (!content) {
      message.warning("请先粘贴需要保存的内容。");
      return;
    }
    const ownsScope = mutationScopeRef.current.capture();
    setImporting(true);
    setImportProgress("正在保存粘贴内容");
    try {
      const filename = `${textTitle.trim() || "粘贴资料"}.txt`;
      await workspaceApi.uploadWikiSource(
        new File([content], filename, { type: "text/plain" }),
      );
      if (!ownsScope()) return;
      setTextTitle("");
      setTextValue("");
      setAddOpen(false);
      message.success("粘贴内容已添加到知识库。");
      await fetchFiles();
      if (!ownsScope()) return;
    } catch (error) {
      if (!ownsScope()) return;
      console.error("Failed to add pasted knowledge", error);
      message.error("粘贴内容保存失败，请重试。");
    } finally {
      if (ownsScope()) {
        setImporting(false);
        setImportProgress("");
      }
    }
  };

  const handleCreateCollection = async () => {
    const name = collectionName.trim();
    if (!name) return;
    const ownsScope = mutationScopeRef.current.capture();
    try {
      const collection = editingCollectionId
        ? await workspaceApi.updateWikiCollection(editingCollectionId, {
            name,
            description: collectionDescription.trim(),
          })
        : await workspaceApi.createWikiCollection(
            name,
            collectionDescription.trim(),
          );
      if (!ownsScope()) return;
      setCollectionName("");
      setCollectionDescription("");
      setEditingCollectionId(null);
      setCollectionOpen(false);
      setCollectionFilter(collection.id);
      await fetchFiles();
      if (!ownsScope()) return;
      message.success(`资料集“${collection.name}”已保存。`);
    } catch (error) {
      if (!ownsScope()) return;
      console.error("Failed to create knowledge collection", error);
      message.error("资料集保存失败，请重试。");
    }
  };

  const openCreateCollection = () => {
    setEditingCollectionId(null);
    setCollectionName("");
    setCollectionDescription("");
    setCollectionOpen(true);
  };

  const openEditCollection = () => {
    const collection = collections.find((item) => item.id === collectionFilter);
    if (!collection) return;
    setEditingCollectionId(collection.id);
    setCollectionName(collection.name);
    setCollectionDescription(collection.description || "");
    setCollectionOpen(true);
  };

  const confirmDeleteCollection = () => {
    const collection = collections.find(
      (item) => item.id === editingCollectionId,
    );
    if (!collection) return;
    const ownsScope = mutationScopeRef.current.capture();
    Modal.confirm({
      title: `删除资料集“${collection.name}”？`,
      content: "资料仍会保留在知识库中，只会移除这一层整理方式。",
      okText: "删除资料集",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        if (!ownsScope()) return;
        await workspaceApi.deleteWikiCollection(collection.id);
        if (!ownsScope()) return;
        setCollectionOpen(false);
        setEditingCollectionId(null);
        setCollectionFilter("all");
        await fetchFiles();
        if (!ownsScope()) return;
      },
    });
  };

  const saveSourceTitle = async () => {
    if (!selectedSource || !sourceTitleDraft.trim()) return;
    const ownsScope = mutationScopeRef.current.capture();
    setSavingSource(true);
    try {
      await workspaceApi.updateWikiSource(selectedSource.path, {
        display_title: sourceTitleDraft.trim(),
      });
      if (!ownsScope()) return;
      await fetchFiles();
      if (!ownsScope()) return;
      setSelectedSource({
        ...selectedSource,
        display_title: sourceTitleDraft.trim(),
      });
      message.success("资料标题已更新。");
    } catch (error) {
      if (!ownsScope()) return;
      console.error("Failed to update knowledge source title", error);
      message.error("资料标题保存失败，请重试。");
    } finally {
      if (ownsScope()) setSavingSource(false);
    }
  };

  const toggleSourceCollection = async (
    collectionId: string,
    checked: boolean,
  ) => {
    if (!selectedSource) return;
    const ownsScope = mutationScopeRef.current.capture();
    const nextIds = checked
      ? [...new Set([...(selectedSource.collection_ids || []), collectionId])]
      : (selectedSource.collection_ids || []).filter(
          (id) => id !== collectionId,
        );
    try {
      await workspaceApi.updateWikiSource(selectedSource.path, {
        collection_ids: nextIds,
      });
      if (!ownsScope()) return;
      setSelectedSource({ ...selectedSource, collection_ids: nextIds });
      await fetchFiles();
      if (!ownsScope()) return;
    } catch (error) {
      if (!ownsScope()) return;
      console.error("Failed to update source collections", error);
      message.error("资料集归类失败，请重试。");
    }
  };

  const confirmArchiveSource = () => {
    if (!selectedSource) return;
    const title = selectedSource.display_title || selectedSource.filename;
    const ownsScope = mutationScopeRef.current.capture();
    Modal.confirm({
      title: `删除“${title}”？`,
      content:
        "澜台会停止检索这份资料。你电脑上的原文件不会被修改，知识库副本会保留在本地回收目录。",
      okText: "移到回收站",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          if (!ownsScope()) return;
          await workspaceApi.archiveWikiSource(selectedSource.path);
          if (!ownsScope()) return;
          setSelectedSourcePaths((paths) =>
            paths.filter((path) => path !== selectedSource.path),
          );
          setSelectedSource(null);
          setDetailKind(null);
          await fetchFiles();
          if (!ownsScope()) return;
          message.success("资料已移到回收站，原文件未受影响。");
        } catch (error) {
          if (!ownsScope()) return;
          message.error(
            error instanceof Error ? error.message : "删除失败，请重试。",
          );
        }
      },
    });
  };

  const confirmDeleteSelectedPage = () => {
    if (!selectedFile) return;
    const ownsScope = mutationScopeRef.current.capture();
    Modal.confirm({
      title: `删除“${selectedFile.title || selectedFile.filename}”？`,
      content: "这条知识会移到知识库回收站并停止参与检索，可以随时恢复。",
      okText: "移到回收站",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: async () => {
        if (!ownsScope()) return;
        if (await handleDelete()) {
          if (!ownsScope()) return;
          setDetailKind(null);
        }
      },
    });
  };

  const archiveKnowledgeItems = async (targets: KnowledgeListItem[]) => {
    const completed: KnowledgeListItem[] = [];
    for (const item of targets) {
      try {
        if (item.source) await workspaceApi.archiveWikiSource(item.source.path);
        else if (item.page)
          await workspaceApi.deleteWikiPage(item.page.filename);
        completed.push(item);
      } catch (_error) {
        setSelectedSourcePaths((current) =>
          current.filter(
            (path) => !completed.some((done) => done.source?.path === path),
          ),
        );
        setSelectedPageNames((current) =>
          current.filter(
            (name) => !completed.some((done) => done.page?.filename === name),
          ),
        );
        await fetchFiles();
        message.error(
          `已移到回收站 ${completed.length}/${targets.length} 项；“${item.title}”删除失败，其余项目尚未处理。`,
        );
        return;
      }
    }
    setSelectedSourcePaths([]);
    setSelectedPageNames([]);
    setSelectedSource(null);
    setDetailKind(null);
    await fetchFiles();
    message.success(`已将 ${completed.length} 项内容移到回收站`);
  };

  const confirmArchiveKnowledgeItems = (
    targets: KnowledgeListItem[],
    title: string,
  ) => {
    if (!targets.length) return;
    Modal.confirm({
      title,
      content:
        "内容会停止参与检索并移到知识库回收站，可以随时恢复。电脑上的原始文件不会被修改。",
      okText: "移到回收站",
      cancelText: "取消",
      okButtonProps: { danger: true },
      onOk: () => archiveKnowledgeItems(targets),
    });
  };

  const renderKnowledgeRow = (item: KnowledgeListItem) => (
    <div className={styles.knowledgeRowShell} key={item.id}>
      <Checkbox
        aria-label={`选择 ${item.title}`}
        checked={
          item.source
            ? selectedSourcePaths.includes(item.source.path)
            : Boolean(
                item.page && selectedPageNames.includes(item.page.filename),
              )
        }
        onChange={(event) => {
          if (item.source) {
            setSelectedSourcePaths((current) =>
              event.target.checked
                ? [...new Set([...current, item.source!.path])]
                : current.filter((path) => path !== item.source!.path),
            );
          } else if (item.page) {
            setSelectedPageNames((current) =>
              event.target.checked
                ? [...new Set([...current, item.page!.filename])]
                : current.filter((name) => name !== item.page!.filename),
            );
          }
        }}
      />
      <button
        type="button"
        aria-label={item.title}
        className={`${styles.knowledgeRow} ${
          (detailKind === "page" &&
            selectedFile?.filename === item.page?.filename) ||
          (detailKind === "source" &&
            selectedSource?.path === item.source?.path)
            ? styles.knowledgeRowActive
            : ""
        }`}
        onClick={() => openItem(item)}
      >
        <span className={styles.itemIcon}>
          {item.source?.kind === "web" ? (
            <GlobalOutlined />
          ) : item.kind === "source" ? (
            <InboxOutlined />
          ) : (
            <FileTextOutlined />
          )}
        </span>
        <span className={styles.itemBody}>
          <span className={styles.itemHeading}>
            <strong>{item.title}</strong>
            <span
              className={`${styles.statusText} ${statusClassName(item.status)}`}
            >
              {knowledgeStatusLabel(item.status)}
            </span>
          </span>
          <span className={styles.itemDescription}>{item.description}</span>
          <span className={styles.itemMeta}>
            {knowledgeKindLabel(item)} · {formatUpdatedAt(item.updatedAt)}
          </span>
        </span>
      </button>
      <Button
        className={styles.rowDelete}
        type="text"
        danger
        size="small"
        icon={<DeleteOutlined />}
        aria-label={`删除 ${item.title}`}
        onClick={() =>
          confirmArchiveKnowledgeItems([item], `删除“${item.title}”？`)
        }
      >
        删除
      </Button>
    </div>
  );

  const renderSearchResults = () => {
    if (searchLoading) {
      return <div className={styles.loadingState}>正在查找相关内容…</div>;
    }
    if (!searchResults?.hits.length) {
      return (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="没有找到相关内容，换个关键词试试"
        />
      );
    }
    return searchResults.hits.map((hit) => (
      <button
        type="button"
        className={styles.knowledgeRow}
        key={`${hit.kind}:${hit.path}`}
        onClick={() => openSearchHit(hit)}
      >
        <span className={styles.itemIcon}>
          {hit.kind === "source" ? <InboxOutlined /> : <FileTextOutlined />}
        </span>
        <span className={styles.itemBody}>
          <span className={styles.itemHeading}>
            <strong>{hit.title}</strong>
            <span className={styles.kindPill}>
              {hit.kind === "source" ? "资料" : "知识"}
            </span>
          </span>
          <span className={styles.itemDescription}>{hit.snippet}</span>
        </span>
        <ArrowRightOutlined className={styles.rowArrow} />
      </button>
    ));
  };

  const libraryNavigation = (
    <nav className={styles.libraryNavigation} aria-label="知识库分区">
      {(
        [
          ["knowledge", "资料", <BookOutlined key="knowledge-icon" />],
          ["reading", "阅读", <ReadOutlined key="reading-icon" />],
          ["research", "研究", <FileTextOutlined key="research-icon" />],
          ["channels", "频道", <CompassOutlined key="channel-icon" />],
          ["trash", "回收站", <DeleteOutlined key="trash-icon" />],
        ] as const
      ).map(([key, label, icon]) => (
        <button
          type="button"
          key={key}
          aria-label={label}
          aria-current={activeArea === key ? "page" : undefined}
          onClick={() => setActiveArea(key)}
        >
          {icon}
          <span>{label}</span>
        </button>
      ))}
    </nav>
  );

  const libraryHeader = (
    <>
      <PageHeader
        className={styles.header}
        items={[{ title: "工作区" }, { title: "知识库" }]}
        description="收集、阅读和整理资料，再把选中的内容带进对话；资料不会自动变成关于你的长期记忆。"
        extra={
          activeArea === "knowledge" ? (
            <div className={styles.headerActions}>
              {selectedSources.length > 0 && <Button size="small" onClick={() => setActiveArea("research")}>用选中资料研究</Button>}
              <Button size="small" onClick={() => setAdvancedOpen(true)}>
                知识库设置
              </Button>
              <Button
                type="primary"
                size="small"
                icon={<PlusOutlined />}
                aria-label="添加资料"
                onClick={() => setAddOpen(true)}
              >
                添加资料
              </Button>
            </div>
          ) : null
        }
      />
      {libraryNavigation}
    </>
  );

  if (activeArea !== "knowledge") {
    return (
      <div className={styles.page} data-design-seed="4d16f909">
        {libraryHeader}
        {activeArea === "research" ? (
          <ResearchPanel key={`research:${selectedAgent}`} sources={sources} selectedSources={selectedSources} />
        ) : activeArea === "reading" ? (
          <ReadingPanel
            key={`reading:${selectedAgent}`}
            onKnowledgeChange={() => void fetchFiles()}
            onOpenChannels={() => setActiveArea("channels")}
          />
        ) : activeArea === "channels" ? (
          <main className={styles.channelWorkspace}>
            <ChannelDiscovery
              key={`channels:${selectedAgent}`}
              onSubscribed={() => setActiveArea("reading")}
            />
          </main>
        ) : (
          <RecycleBin
            key={`trash:${selectedAgent}`}
            onRestore={() => void fetchFiles()}
          />
        )}
      </div>
    );
  }

  return (
    <div className={styles.page} data-design-seed="4d16f909">
      {libraryHeader}

      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={WIKI_SOURCE_ACCEPT}
        onChange={handleFileUpload}
        className={styles.hiddenInput}
      />

      <main className={styles.workspace}>
        <section className={styles.objectGuide} aria-label="资料关系说明">
          <strong>资料、知识、记忆和作品各自独立</strong>
          <span>
            文件保存后还要整理才能检索；阅读订阅和频道内容只有加入知识库后才参与知识检索。回答实际使用了哪段资料，会给出来源链接。知识库内容不会自动写成“关于你”的记忆，任务产物也只有加入作品后才成为作品副本。
          </span>
        </section>

        {selectedKnowledgeItems.length ? (
          <section className={styles.selectionBar} aria-label="已选资料操作">
            <span>
              已选 <strong>{selectedKnowledgeItems.length}</strong> 项内容
            </span>
            {selectedSources.length ? (
              <>
                <Radio.Group
                  size="small"
                  value={scopeMode}
                  onChange={(event) => setScopeMode(event.target.value)}
                  optionType="button"
                  buttonStyle="solid"
                  options={[
                    { label: "仅依据所选资料", value: "exact" },
                    { label: "允许补充其他信息", value: "expanded" },
                  ]}
                />
                <Button
                  type="primary"
                  size="small"
                  onClick={() => askWithSources(selectedSources)}
                >
                  用 {selectedSources.length} 份资料提问
                </Button>
              </>
            ) : null}
            <Button
              danger
              size="small"
              icon={<DeleteOutlined />}
              onClick={() =>
                confirmArchiveKnowledgeItems(
                  selectedKnowledgeItems,
                  `删除所选 ${selectedKnowledgeItems.length} 项内容？`,
                )
              }
            >
              删除
            </Button>
            <Button
              size="small"
              type="text"
              onClick={() => {
                setSelectedSourcePaths([]);
                setSelectedPageNames([]);
              }}
            >
              取消选择
            </Button>
          </section>
        ) : null}

        <section
          className={`${styles.contentShell} ${
            detailKind ? "" : styles.contentShellEmpty
          }`}
        >
          <aside
            className={styles.collectionPane}
            aria-label="资料视图与资料集"
          >
            <div className={styles.collectionHeading}>
              <span>资料视图</span>
            </div>
            {(
              [
                ["all", "全部内容", <BookOutlined key="all" />, items.length],
                [
                  "source",
                  "原始资料",
                  <InboxOutlined key="source" />,
                  sources.length,
                ],
                [
                  "page",
                  "知识条目",
                  <FileTextOutlined key="page" />,
                  files.length,
                ],
                [
                  "attention",
                  "需要处理",
                  <WarningFilled key="attention" />,
                  attentionCount,
                ],
              ] as const
            ).map(([key, label, icon, count]) => (
              <button
                type="button"
                key={key}
                aria-label={
                  key === "attention" && attentionCount
                    ? `${label}，${attentionCount} 项`
                    : label
                }
                className={
                  filter === key && collectionFilter === "all"
                    ? styles.collectionActive
                    : undefined
                }
                onClick={() => {
                  setFilter(key);
                  setCollectionFilter("all");
                  setSearchSubmitted(false);
                  setSearchValue("");
                  void handleSearch("");
                  sourceLoadTokenRef.current += 1;
                  setSelectedSource(null);
                  setDetailKind(null);
                  if (key === "attention" && attentionCount > 0)
                    setAttentionOpen(true);
                }}
              >
                {icon}
                <span>{label}</span>
                <b>{count}</b>
              </button>
            ))}
            <div className={styles.collectionDivider} />
            <div className={styles.collectionHeading}>
              <span>资料集</span>
              <button
                type="button"
                aria-label="新建资料集"
                onClick={openCreateCollection}
              >
                <PlusOutlined />
              </button>
            </div>
            <button
              type="button"
              className={
                collectionFilter === "all" ? styles.collectionActive : undefined
              }
              onClick={() => {
                setCollectionFilter("all");
                setFilter("source");
                setSelectedSource(null);
                setDetailKind(null);
              }}
            >
              <BookOutlined />
              <span>原始资料</span>
              <b>{sources.length}</b>
            </button>
            {collections.map((collection) => (
              <button
                type="button"
                key={collection.id}
                className={
                  collectionFilter === collection.id
                    ? styles.collectionActive
                    : undefined
                }
                onClick={() => {
                  setCollectionFilter(collection.id);
                  setFilter("source");
                  setSelectedSource(null);
                  setDetailKind(null);
                }}
                title={collection.description || collection.name}
              >
                <FolderOutlined />
                <span>{collection.name}</span>
                <b>{collection.source_paths.length}</b>
              </button>
            ))}
            <button
              type="button"
              className={styles.collectionCreate}
              onClick={openCreateCollection}
            >
              <PlusOutlined /> 新建资料集
            </button>
          </aside>
          <div className={styles.listPane}>
            <div className={styles.listHeader}>
              <div>
                <h2>
                  {searchSubmitted
                    ? "搜索结果"
                    : collectionFilter === "all"
                      ? filter === "source"
                        ? "原始资料"
                        : filter === "page"
                          ? "知识条目"
                          : filter === "attention"
                            ? "需要处理"
                            : "全部内容"
                      : collections.find((item) => item.id === collectionFilter)
                          ?.name || "资料集"}
                </h2>
                <p>
                  {searchSubmitted
                    ? `正在搜索“${searchValue.trim()}”`
                    : collectionFilter === "all"
                      ? `${files.length} 条知识笔记 · ${sources.length} 份原始资料 · ${summary.ready} 项可用${summary.processing ? ` · ${summary.processing} 项整理中` : ""}`
                      : `${visibleItems.length} 项内容，最近更新在前`}
                </p>
              </div>
              <div className={styles.listHeaderActions}>
                {collectionFilter !== "all" ? (
                  <Button type="text" size="small" onClick={openEditCollection}>
                    管理资料集
                  </Button>
                ) : null}
                <Checkbox
                  aria-label="选择当前内容"
                  checked={
                    visibleItems.length > 0 &&
                    visibleItems.every((item) =>
                      item.source
                        ? selectedSourcePaths.includes(item.source.path)
                        : Boolean(
                            item.page &&
                            selectedPageNames.includes(item.page.filename),
                          ),
                    )
                  }
                  onChange={(event) => {
                    const paths = visibleItems.flatMap((item) =>
                      item.source ? [item.source.path] : [],
                    );
                    const pageNames = visibleItems.flatMap((item) =>
                      item.page ? [item.page.filename] : [],
                    );
                    setSelectedSourcePaths((current) =>
                      event.target.checked
                        ? [...new Set([...current, ...paths])]
                        : current.filter((path) => !paths.includes(path)),
                    );
                    setSelectedPageNames((current) =>
                      event.target.checked
                        ? [...new Set([...current, ...pageNames])]
                        : current.filter((name) => !pageNames.includes(name)),
                    );
                  }}
                >
                  全选
                </Checkbox>
                <Button
                  type="text"
                  size="small"
                  icon={<ReloadOutlined />}
                  onClick={() => void fetchFiles()}
                >
                  刷新
                </Button>
              </div>
            </div>

            <div className={styles.listTools}>
              <Input.Search
                allowClear
                prefix={<SearchOutlined />}
                aria-label="搜索知识库"
                enterButton="搜索"
                value={searchValue}
                onChange={(event) => clearSearchIfEmpty(event.target.value)}
                onSearch={submitSearch}
                loading={searchLoading}
                placeholder="搜索标题或正文"
                className={styles.search}
              />
            </div>

            <div className={styles.listBody}>
              {searchSubmitted ? (
                renderSearchResults()
              ) : visibleItems.length ? (
                visibleItems.map(renderKnowledgeRow)
              ) : items.length === 0 ? (
                <div className={styles.firstRun}>
                  <span className={styles.firstRunIcon}>
                    <BookOutlined />
                  </span>
                  <h2>从一份资料开始</h2>
                  <p>
                    上传文件或添加网页，澜台会自动整理、保留来源并用于后续对话。
                  </p>
                  <div className={styles.firstRunSteps}>
                    <span>
                      <b>1</b> 添加资料
                    </span>
                    <span>
                      <b>2</b> 自动整理
                    </span>
                    <span>
                      <b>3</b> 在聊天中使用
                    </span>
                  </div>
                  <Button type="primary" onClick={() => setAddOpen(true)}>
                    添加第一份资料
                  </Button>
                </div>
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="这个筛选条件下没有内容"
                />
              )}
            </div>
          </div>

          {detailKind ? (
            <aside className={styles.detailPane} ref={detailPaneRef}>
              {detailKind === "page" && selectedFile ? (
                <div className={styles.editorWrap}>
                  <FileEditor
                    selectedFile={selectedFile}
                    fileContent={fileContent}
                    loading={loading}
                    deleting={deleting}
                    hasChanges={hasChanges}
                    onContentChange={setFileContent}
                    onSave={handleSave}
                    onReset={handleReset}
                    onDelete={confirmDeleteSelectedPage}
                  />
                  <div className={styles.detailFooter}>
                    <Button type="primary" onClick={() => navigate("/chat")}>
                      去聊天中使用
                    </Button>
                  </div>
                </div>
              ) : detailKind === "source" && selectedSource ? (
                <div className={styles.sourceDetail}>
                  <div className={styles.sourceDetailHeading}>
                    <span className={styles.sourceDetailIcon}>
                      {selectedSource.kind === "web" ? (
                        <GlobalOutlined />
                      ) : (
                        <InboxOutlined />
                      )}
                    </span>
                    <div>
                      <Tag bordered={false}>
                        {selectedSource.kind === "web" ? "网页" : "资料"}
                      </Tag>
                      <span>
                        {selectedSource.original_path ||
                          selectedSource.filename}
                      </span>
                    </div>
                    <div className={styles.detailHeadingActions}>
                      <Button
                        type="text"
                        danger
                        size="small"
                        icon={<DeleteOutlined />}
                        aria-label="将当前资料移到知识库回收站"
                        onClick={confirmArchiveSource}
                      >
                        移到回收站
                      </Button>
                      <button
                        type="button"
                        className={styles.detailClose}
                        aria-label="关闭资料详情"
                        onClick={() => {
                          sourceLoadTokenRef.current += 1;
                          setDetailKind(null);
                        }}
                      >
                        <CloseOutlined />
                      </button>
                    </div>
                  </div>
                  <div className={styles.sourceTitleEditor}>
                    <Input
                      value={sourceTitleDraft}
                      onChange={(event) =>
                        setSourceTitleDraft(event.target.value)
                      }
                      aria-label="资料标题"
                    />
                    <Button
                      icon={<EditOutlined />}
                      loading={savingSource}
                      disabled={
                        !sourceTitleDraft.trim() ||
                        sourceTitleDraft.trim() ===
                          (selectedSource.display_title ||
                            selectedSource.filename)
                      }
                      onClick={() => void saveSourceTitle()}
                    >
                      保存标题
                    </Button>
                  </div>
                  <p>
                    {selectedSource.semantic_summary ||
                      selectedSource.understanding_summary ||
                      "这份资料已经保留在当前智能体的知识库中。"}
                  </p>
                  <div className={styles.sourceFacts}>
                    <div>
                      <span>整理与检索</span>
                      <strong>
                        {knowledgeStatusLabel(
                          items.find(
                            (item) => item.source?.path === selectedSource.path,
                          )?.status ?? "ready",
                        )}
                      </strong>
                    </div>
                    <div>
                      <span>正文读取</span>
                      <strong>{selectedSource.extracted_chars || 0} 字</strong>
                    </div>
                    <div>
                      <span>最近更新</span>
                      <strong>
                        {formatUpdatedAt(selectedSource.updated_at)}
                      </strong>
                    </div>
                  </div>
                  <section className={styles.sourceCollections}>
                    <h3>所属资料集</h3>
                    {collections.length ? (
                      <div>
                        {collections.map((collection) => (
                          <Checkbox
                            key={collection.id}
                            checked={selectedSource.collection_ids?.includes(
                              collection.id,
                            )}
                            onChange={(event) =>
                              void toggleSourceCollection(
                                collection.id,
                                event.target.checked,
                              )
                            }
                          >
                            {collection.name}
                          </Checkbox>
                        ))}
                      </div>
                    ) : (
                      <button type="button" onClick={openCreateCollection}>
                        <PlusOutlined /> 新建资料集
                      </button>
                    )}
                  </section>
                  <section className={styles.sourceReader}>
                    <div className={styles.sourceReaderHeading}>
                      <h3>资料正文</h3>
                      {sourceContent?.page_count ? (
                        <span>{sourceContent.page_count} 页</span>
                      ) : null}
                    </div>
                    {sourceContentLoading ? (
                      <div className={styles.loadingState}>正在打开资料…</div>
                    ) : sourceContentError ? (
                      <div className={styles.sourceReaderEmpty} role="alert">
                        <strong>资料正文读取失败</strong>
                        <span>{sourceContentError}</span>
                        <Button
                          size="small"
                          onClick={() =>
                            openSource(selectedSource, sourceAnchor, sourceEvidence)
                          }
                        >
                          重试读取正文
                        </Button>
                      </div>
                    ) : sourceContent?.content_status === "version_unavailable" ? (
                      <div className={styles.sourceReaderEmpty} role="status">
                        <strong>引用的资料版本已不可核对</strong>
                        <span>{sourceContent.message}</span>
                        <Button size="small" onClick={() => openSource(selectedSource)}>
                          查看最新版
                        </Button>
                      </div>
                    ) : sourceContent?.content ? (
                      <>
                        {sourceContent.content_status === "position_unavailable" ? (
                          <div role="status">{sourceContent.message}</div>
                        ) : null}
                      {sourceAnchor &&
                      sourceAnchor.end > sourceAnchor.start &&
                      sourceAnchor.start < sourceCodepoints.length &&
                      (!sourceEvidence || Boolean(sourceContent.source_excerpt)) ? (
                        <pre>
                          {sourceCodepoints.slice(0, sourceAnchor.start).join("")}
                          <mark ref={citationMarkRef}>
                            {sourceCodepoints.slice(
                              sourceAnchor.start,
                              sourceAnchor.end,
                            ).join("")}
                          </mark>
                          {sourceCodepoints.slice(sourceAnchor.end).join("")}
                        </pre>
                      ) : (
                        <pre>{sourceContent.content}</pre>
                      )}
                      </>
                    ) : (
                      <div className={styles.sourceReaderEmpty}>
                        <strong>暂时没有可读正文</strong>
                        <span>
                          {sourceContent?.message ||
                            selectedSource.extraction_message ||
                            "原始文件已经保留，可以重新整理或打开关联笔记。"}
                        </span>
                      </div>
                    )}
                  </section>
                  {selectedSource.linked_pages.length ? (
                    <div className={styles.linkedPages}>
                      <h3>由这份资料整理出的内容</h3>
                      {selectedSource.linked_pages.map((filename) => (
                        <button
                          type="button"
                          key={filename}
                          onClick={() => openLinkedPage(filename)}
                        >
                          <FileTextOutlined />
                          <span>
                            {files.find((file) => file.filename === filename)
                              ?.title || filename}
                          </span>
                          <ArrowRightOutlined />
                        </button>
                      ))}
                    </div>
                  ) : null}
                  <div className={styles.sourceActions}>
                    <Button
                      type="primary"
                      onClick={() => askWithSources([selectedSource])}
                    >
                      用这份资料提问
                    </Button>
                    <Button
                      onClick={() =>
                        void handleRunIngest("fast", selectedSource.path)
                      }
                    >
                      重新整理
                    </Button>
                  </div>
                </div>
              ) : null}
            </aside>
          ) : null}
        </section>
      </main>

      <Modal
        title="添加资料"
        open={addOpen}
        onCancel={() => setAddOpen(false)}
        footer={null}
        width={620}
        destroyOnHidden
      >
        <div className={styles.addModal}>
          <div
            className={styles.addModeTabs}
            role="tablist"
            aria-label="添加方式"
          >
            {(
              [
                ["file", "上传文件"],
                ["web", "添加网页"],
                ["text", "粘贴文本"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={addMode === key}
                className={addMode === key ? styles.addModeActive : undefined}
                onClick={() => setAddMode(key)}
              >
                {label}
              </button>
            ))}
          </div>
          {addMode === "file" ? (
            <button
              type="button"
              className={styles.addChoice}
              disabled={importing}
              onClick={() => fileInputRef.current?.click()}
              aria-label="上传文件"
            >
              <span>
                <UploadOutlined />
              </span>
              <div>
                <strong>选择一个或多个文件</strong>
                <p>
                  支持
                  PDF、Word、表格、演示文稿、Markdown、图片和压缩包，单个最大
                  100MB。
                </p>
              </div>
              <ArrowRightOutlined />
            </button>
          ) : null}
          {addMode === "web" ? (
            <div className={styles.urlImport}>
              <span className={styles.urlIcon}>
                <LinkOutlined />
              </span>
              <div>
                <strong>添加网页</strong>
                <p>
                  每行粘贴一个网址。澜台会保存正文和原始地址，方便后续查证。
                </p>
                <Input.TextArea
                  value={urlValue}
                  onChange={(event) => setUrlValue(event.target.value)}
                  autoSize={{ minRows: 2, maxRows: 5 }}
                  placeholder={
                    "https://example.com/article\nhttps://example.com/report"
                  }
                />
                <Button
                  size="small"
                  type="primary"
                  loading={importing}
                  onClick={() => void handleUrlImport()}
                >
                  添加网页
                </Button>
              </div>
            </div>
          ) : null}
          {addMode === "text" ? (
            <div className={styles.pasteImport}>
              <div>
                <strong>粘贴文本</strong>
                <p>适合会议记录、片段和暂时没有文件的资料。</p>
              </div>
              <Input
                value={textTitle}
                onChange={(event) => setTextTitle(event.target.value)}
                placeholder="资料标题（可选）"
              />
              <Input.TextArea
                value={textValue}
                onChange={(event) => setTextValue(event.target.value)}
                autoSize={{ minRows: 3, maxRows: 8 }}
                placeholder="在这里粘贴内容"
              />
              <Button
                size="small"
                loading={importing}
                onClick={() => void handleTextImport()}
              >
                保存文本
              </Button>
            </div>
          ) : null}
          {importProgress ? (
            <p className={styles.importProgress}>{importProgress}</p>
          ) : null}
          {visibleImportReceipts.length ? (
            <section
              className={styles.importReceipts}
              aria-label="本批导入结果"
            >
              <div className={styles.importReceiptHeader}>
                <strong>本批结果</strong>
                {retryableImportCount ? (
                  <Button
                    size="small"
                    disabled={importing}
                    onClick={() => void retryFailedImports()}
                  >
                    仅重试失败项（{retryableImportCount}）
                  </Button>
                ) : null}
              </div>
              <div className={styles.importReceiptList}>
                {visibleImportReceipts.map((receipt) => (
                  <article
                    className={`${styles.importReceipt} ${importReceiptClassName(receipt.status)}`}
                    key={receipt.key}
                  >
                    <div>
                      <strong>{receipt.label}</strong>
                      <span>{importReceiptStatusLabel(receipt)}</span>
                    </div>
                    <p>{receipt.detail}</p>
                  </article>
                ))}
              </div>
              <p className={styles.importReceiptAuthority}>
                “已接收”只表示来源已保存，不代表整理、索引或搜索已完成；请以资料列表状态为准。
              </p>
            </section>
          ) : null}
          <p className={styles.privacyNote}>
            资料保存在当前智能体的本地工作区，不会自动公开或分享。
          </p>
        </div>
      </Modal>

      <Modal
        title={editingCollectionId ? "管理资料集" : "新建资料集"}
        open={collectionOpen}
        okText="创建"
        cancelText="取消"
        okButtonProps={{ disabled: !collectionName.trim() }}
        onOk={() => void handleCreateCollection()}
        onCancel={() => setCollectionOpen(false)}
      >
        <p className={styles.modalHint}>
          资料集用于按项目或主题组织材料，同一份资料可以放进多个资料集。
        </p>
        <Input
          autoFocus
          value={collectionName}
          onChange={(event) => setCollectionName(event.target.value)}
          onPressEnter={() => void handleCreateCollection()}
          placeholder="例如：金山办公研究"
        />
        <Input.TextArea
          className={styles.collectionDescriptionInput}
          value={collectionDescription}
          onChange={(event) => setCollectionDescription(event.target.value)}
          autoSize={{ minRows: 2, maxRows: 5 }}
          placeholder="这个资料集主要解决什么问题（可选）"
        />
        {editingCollectionId ? (
          <Button
            className={styles.collectionDeleteButton}
            danger
            type="text"
            onClick={confirmDeleteCollection}
          >
            删除资料集
          </Button>
        ) : null}
      </Modal>

      <Drawer
        title="需要处理"
        open={attentionOpen}
        onClose={() => setAttentionOpen(false)}
        width={520}
      >
        <div className={styles.drawerStack}>
          {pendingReviews.map((item) => (
            <section className={styles.actionCard} key={item.id}>
              <Tag color="gold" bordered={false}>
                需要确认
              </Tag>
              <h3>{item.title}</h3>
              <p>{item.message}</p>
              {item.targets.length ? (
                <div className={styles.targetList}>
                  {item.targets.map((target) => (
                    <span key={target}>{target}</span>
                  ))}
                </div>
              ) : null}
              <div className={styles.actionRow}>
                <Button
                  type="primary"
                  size="small"
                  onClick={() => void handleResolveReview(item.id, "resolved")}
                >
                  标记已处理
                </Button>
                <Button
                  size="small"
                  onClick={() => void handleResolveReview(item.id, "skipped")}
                >
                  暂不处理
                </Button>
              </div>
              <p className={styles.actionHint}>
                “标记已处理”只关闭这条提醒，不会自动修改你的知识内容。
              </p>
            </section>
          ))}
          {failedSources.map((item) => (
            <section className={styles.actionCard} key={item.id}>
              <Tag color="red" bordered={false}>
                整理失败
              </Tag>
              <h3>{item.title}</h3>
              <p>{item.description}</p>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={() =>
                  void handleRunIngest("fast", item.source?.path || "")
                }
              >
                重试整理
              </Button>
            </section>
          ))}
          {unlistedQueueFailures ? (
            <section className={styles.actionCard}>
              <Tag color="red" bordered={false}>
                整理失败
              </Tag>
              <h3>{unlistedQueueFailures} 个整理任务未完成</h3>
              <p>
                原始资料仍然保留。重新检查会刷新索引，并再次处理可恢复的资料。
              </p>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={() => void handleRunIngest("fast")}
              >
                重新检查
              </Button>
            </section>
          ) : null}
          {attentionSources.map((item) => (
            <section className={styles.actionCard} key={item.id}>
              <Tag color="gold" bordered={false}>
                {item.status === "saved" ? "已保存，待整理" : "需要确认"}
              </Tag>
              <h3>{item.title}</h3>
              <p>{item.description}</p>
              <Button
                size="small"
                onClick={() => {
                  if (item.source) openSource(item.source);
                  setAttentionOpen(false);
                }}
              >
                查看资料
              </Button>
            </section>
          ))}
          {!attentionCount ? <Empty description="没有需要处理的内容" /> : null}
        </div>
      </Drawer>

      <Drawer
        title="知识库设置"
        open={advancedOpen}
        onClose={() => setAdvancedOpen(false)}
        width={600}
      >
        <div className={styles.drawerStack}>
          <section className={styles.manageSection}>
            <div className={styles.manageHeading}>
              <div>
                <h3>用途与范围</h3>
                <p>告诉澜台这批资料主要用来解决什么问题。</p>
              </div>
            </div>
            <Input.TextArea
              value={purposeContent}
              onChange={(event) => setPurposeContent(event.target.value)}
              autoSize={{ minRows: 6, maxRows: 14 }}
              placeholder="例如：帮助我研究中国现代诗，并优先引用原始作品和学术资料。"
            />
            <div className={styles.actionRow}>
              <Button
                type="primary"
                size="small"
                disabled={!purposeHasChanges}
                loading={loading}
                onClick={() => void handlePurposeSave()}
              >
                保存
              </Button>
              <Button
                size="small"
                disabled={!purposeHasChanges}
                onClick={handlePurposeReset}
              >
                撤销修改
              </Button>
            </div>
          </section>

          <section className={styles.manageSection}>
            <div className={styles.manageHeading}>
              <div>
                <h3>知识库健康</h3>
                <p>只有影响检索或来源追溯的问题才需要处理。</p>
              </div>
              <span
                className={
                  lint?.issues.length ? styles.healthWarn : styles.healthGood
                }
              >
                {lint?.issues.length
                  ? `${lint.issues.length} 个建议`
                  : "状态良好"}
              </span>
            </div>
            {lint?.issues.length ? (
              <div className={styles.healthList}>
                {lint.issues.slice(0, 6).map((issue) => (
                  <div key={`${issue.file}:${issue.code}`}>
                    <WarningFilled />
                    <span>
                      <strong>{issue.title}</strong>
                      {issue.message}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className={styles.healthyState}>
                <CheckCircleFilled /> 标题、来源和内容连接都没有明显问题。
              </div>
            )}
            {insights?.insights[0] ? (
              <p className={styles.manageInsight}>
                {insights.insights[0].message}
              </p>
            ) : null}
          </section>

          <section className={styles.manageSection}>
            <div className={styles.manageHeading}>
              <div>
                <h3>重新整理</h3>
                <p>
                  一般无需手动操作。只有新增文件未出现或搜索结果异常时再使用。
                </p>
              </div>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={loading}
                onClick={() => void handleRunIngest("fast")}
              >
                重新整理
              </Button>
            </div>
          </section>

          <details className={styles.technicalDetails}>
            <summary>高级信息</summary>
            <dl>
              <div>
                <dt>知识内容</dt>
                <dd>{files.length}</dd>
              </div>
              <div>
                <dt>原始资料</dt>
                <dd>{sources.length}</dd>
              </div>
              <div>
                <dt>内容关系</dt>
                <dd>{graph.edges.length}</dd>
              </div>
              <div>
                <dt>结构字段</dt>
                <dd>{schema?.stats.fields ?? 0}</dd>
              </div>
            </dl>
            <p>本地位置：{workspacePath || purpose?.path || "尚未创建"}</p>
            <p>这些信息用于排查问题，日常使用无需维护。</p>
          </details>
        </div>
      </Drawer>
    </div>
  );
}
