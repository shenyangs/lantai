import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Dropdown,
  Modal,
  Select,
  Skeleton,
  Switch,
  Tooltip,
  type MenuProps,
} from "antd";
import {
  AppWindow,
  BookOpenCheck,
  File,
  FileImage,
  FileText,
  FolderPlus,
  Grid2X2,
  List,
  MessageSquareText,
  MoreHorizontal,
  Music2,
  PencilLine,
  Play,
  Presentation,
  RefreshCw,
  Search,
  Settings2,
  Sheet,
  Sparkles,
  Trash2,
  Video,
  X,
} from "lucide-react";
import { capabilityApi } from "../../api/modules/capability";
import { imageGenerationApi } from "../../api/modules/imageGeneration";
import { privateEditorApi } from "../../api/modules/privateEditor";
import { worksApi } from "../../api/modules/works";
import type { Artifact, TaskRun } from "../../api/types/capability";
import type { ImageGenerationConfig } from "../../api/types/imageGeneration";
import type {
  WorkItem,
  WorkKind,
  WorksIndexInspection,
} from "../../api/types/works";
import { useAppMessage } from "../../hooks/useAppMessage";
import { useAgentStore } from "../../stores/agentStore";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../utils/latestRequest";
import { canInlinePreviewWork, workCardPreviewMode } from "./previewPolicy";
import styles from "./index.module.less";

type ViewMode = "gallery" | "list";

interface WorkCandidate {
  key: string;
  path: string;
  title: string;
  runId: string;
  runTitle: string;
  chatId?: string | null;
  createdAt: string;
}

export function workSourceDestination(
  work: Pick<WorkItem, "source_chat_id" | "source_run_id">,
): string | null {
  if (work.source_chat_id) {
    return `/chat/${encodeURIComponent(work.source_chat_id)}`;
  }
  return null;
}

const VIEW_MODE_KEY = "lantai:works-view-mode";
const DEFAULT_COVER_CONFIG: ImageGenerationConfig = {
  enabled: false,
  models: [],
  timeout_seconds: 120,
  works_cover_enabled: false,
  works_cover_model: { provider_id: "", model: "" },
};

function modelSlotValue(slot: { provider_id: string; model: string }): string {
  return `${slot.provider_id}\u0000${slot.model}`;
}

function storedViewMode(): ViewMode {
  try {
    return window.localStorage.getItem(VIEW_MODE_KEY) === "list"
      ? "list"
      : "gallery";
  } catch {
    return "gallery";
  }
}

function formatFileSize(value?: number | null): string {
  if (!value || value < 1) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function extensionOf(path: string): string {
  return (
    path
      .split(/[?#]/, 1)[0]
      ?.match(/\.([a-z0-9]+)$/i)?.[1]
      ?.toUpperCase() || "文件"
  );
}

function kindLabel(kind: WorkKind): string {
  const labels: Record<WorkKind, string> = {
    file: "文件",
    image: "图片",
    html: "网页 / 小工具",
    markdown: "Markdown",
    pdf: "PDF",
    presentation: "演示文稿",
    document: "文档",
    spreadsheet: "表格",
    audio: "音频",
    video: "视频",
  };
  return labels[kind];
}

function isTextualWork(work: WorkItem): boolean {
  if (work.kind === "markdown") return true;
  if (work.mime_type?.startsWith("text/")) return true;
  return /\.(?:txt|json|ya?ml|xml|css|js|jsx|ts|tsx|py|sh|toml|ini|log)$/i.test(
    work.path,
  );
}

interface WorkUsability {
  usable: boolean;
  label?: string;
  detail?: string;
}

function workUsability(work: WorkItem): WorkUsability {
  // Older API responses omitted both projection fields. Preserve compatibility
  // while treating every explicit unknown or unsafe state as non-actionable.
  if (!work.availability_status && !work.integrity_status) {
    return { usable: true };
  }
  if (work.availability_status === "missing") {
    return {
      usable: false,
      label: "保存副本已缺失",
      detail: "澜台保存的版本已不在原位置；来源记录仍保留。",
    };
  }
  if (work.availability_status === "permission_denied") {
    return {
      usable: false,
      label: "无法读取保存副本",
      detail: "当前没有权限读取澜台保存的版本；来源记录仍保留。",
    };
  }
  if (work.availability_status !== "available") {
    return {
      usable: false,
      label: "保存副本状态待核实",
      detail: "澜台暂时无法确认保存版本是否可用，请刷新后再决定下一步。",
    };
  }
  if (work.integrity_status === "modified") {
    return {
      usable: false,
      label: "保存版本已被改动",
      detail:
        "当前文件与加入作品时的版本不同，澜台不会把它当作原保存版本打开。",
    };
  }
  if (work.integrity_status !== "intact") {
    return {
      usable: false,
      label: "保存版本完整性待核实",
      detail: "澜台暂时无法确认当前文件仍是加入作品时的版本。",
    };
  }
  return { usable: true };
}

function supportsPrivateEditor(work: WorkItem): boolean {
  return /\.(?:txt|md|markdown|docx)$/i.test(work.path);
}

function iconForKind(kind: WorkKind, size = 22): ReactNode {
  const props = { size, strokeWidth: 1.65, "aria-hidden": true } as const;
  switch (kind) {
    case "image":
      return <FileImage {...props} />;
    case "html":
      return <AppWindow {...props} />;
    case "presentation":
      return <Presentation {...props} />;
    case "spreadsheet":
      return <Sheet {...props} />;
    case "audio":
      return <Music2 {...props} />;
    case "video":
      return <Video {...props} />;
    case "document":
    case "markdown":
    case "pdf":
      return <FileText {...props} />;
    default:
      return <File {...props} />;
  }
}

function workKindFromArtifact(artifact: Artifact): WorkKind {
  const path = artifact.path || artifact.title || "";
  const extension = extensionOf(path).toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "heic"].includes(extension))
    return "image";
  if (["html", "htm"].includes(extension)) return "html";
  if (["ppt", "pptx", "key"].includes(extension)) return "presentation";
  if (["doc", "docx", "pages", "rtf"].includes(extension)) return "document";
  if (["xls", "xlsx", "csv", "numbers"].includes(extension))
    return "spreadsheet";
  if (extension === "pdf") return "pdf";
  if (["md", "markdown"].includes(extension)) return "markdown";
  if (["mp3", "wav", "m4a", "aac"].includes(extension)) return "audio";
  if (["mp4", "mov", "webm", "avi"].includes(extension)) return "video";
  return "file";
}

function artifactCandidates(runs: TaskRun[]): WorkCandidate[] {
  const seen = new Set<string>();
  const candidates: WorkCandidate[] = [];
  for (const run of runs) {
    for (const artifact of run.artifacts || []) {
      const path = artifact.path?.trim();
      if (!path || seen.has(path)) continue;
      seen.add(path);
      candidates.push({
        key: `${run.id}:${artifact.id}`,
        path,
        title: artifact.title || path.split(/[\\/]/).pop() || "未命名产物",
        runId: run.id,
        runTitle: run.title,
        chatId: run.chat_id,
        createdAt: artifact.created_at || run.started_at,
      });
    }
  }
  return candidates;
}

type WorkPreviewAsset =
  | {
      kind: "image";
      value: string;
      generatedCover?: boolean;
      nativeThumbnail?: boolean;
    }
  | { kind: "pdf"; value: string };

type WorkPreviewFit = "cover" | "contain";

function nativeThumbnailFit(
  naturalWidth: number,
  naturalHeight: number,
): WorkPreviewFit {
  if (naturalWidth <= 0 || naturalHeight <= 0) return "cover";
  return naturalWidth / naturalHeight <= 4 / 3 ? "contain" : "cover";
}

async function isPdfBlob(blob: Blob): Promise<boolean> {
  if (blob.type.toLowerCase().includes("application/pdf")) return true;
  const signature = new Uint8Array(await blob.slice(0, 5).arrayBuffer());
  return String.fromCharCode(...signature) === "%PDF-";
}

function WorkPreviewSurface({ work }: { work: WorkItem }) {
  const [asset, setAsset] = useState<WorkPreviewAsset | null>(null);
  const [previewFit, setPreviewFit] = useState<WorkPreviewFit>("cover");

  useEffect(() => {
    let active = true;
    let objectUrl = "";
    setAsset(null);
    setPreviewFit("cover");
    if (!workUsability(work).usable) return;
    void (async () => {
      try {
        const mode = workCardPreviewMode(work);
        if (mode === "generated-cover") {
          const blob = await worksApi.coverBlob(work.id);
          if (!blob.type.startsWith("image/")) throw new Error("invalid cover");
          objectUrl = URL.createObjectURL(blob);
          if (active)
            setAsset({ kind: "image", value: objectUrl, generatedCover: true });
          return;
        }

        if (
          mode === "native-thumbnail" &&
          window.electronAPI?.createWorkThumbnail
        ) {
          const thumbnail = await window.electronAPI.createWorkThumbnail(
            work.path,
          );
          if (thumbnail.success && thumbnail.data_url) {
            if (active)
              setAsset({
                kind: "image",
                value: thumbnail.data_url,
                nativeThumbnail: true,
              });
            return;
          }
        }

        // HTML cards never execute user-authored scripts in the library. The
        // native thumbnail or the static title cover above is the preview.
        if (work.kind === "html") return;
        if (!["image", "pdf"].includes(work.kind)) return;
        const blob = await worksApi.contentBlob(work.id);
        if (!active) return;
        if (work.kind === "image") {
          if (!blob.type.startsWith("image/")) return;
          objectUrl = URL.createObjectURL(blob);
          setAsset({ kind: "image", value: objectUrl });
          return;
        }
        if (await isPdfBlob(blob)) {
          objectUrl = URL.createObjectURL(blob);
          setAsset({ kind: "pdf", value: objectUrl });
        }
      } catch {
        // A clean title cover is the guaranteed fallback.
      }
    })();
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [
    work.availability_status,
    work.cover_path,
    work.id,
    work.integrity_status,
    work.kind,
    work.path,
  ]);

  if (asset?.kind === "image") {
    return (
      <>
        <img
          className={styles.previewImage}
          src={asset.value}
          alt=""
          data-preview-fit={asset.nativeThumbnail ? previewFit : undefined}
          style={asset.nativeThumbnail ? { objectFit: previewFit } : undefined}
          onLoad={
            asset.nativeThumbnail
              ? (event) =>
                  setPreviewFit(
                    nativeThumbnailFit(
                      event.currentTarget.naturalWidth,
                      event.currentTarget.naturalHeight,
                    ),
                  )
              : undefined
          }
        />
        {asset.generatedCover ? (
          <span className={styles.generatedCoverTitle}>{work.title}</span>
        ) : null}
      </>
    );
  }
  if (asset?.kind === "pdf") {
    return (
      <iframe
        className={styles.previewFrame}
        title={`${work.title} 缩略预览`}
        src={asset.value}
        tabIndex={-1}
      />
    );
  }
  return (
    <div
      className={[styles.documentPreview, styles[`kind_${work.kind}`]]
        .filter(Boolean)
        .join(" ")}
    >
      <span className={styles.documentPreviewTop}>
        <span className={styles.documentExtension}>
          {extensionOf(work.path)}
        </span>
        <span className={styles.documentIcon}>
          {iconForKind(work.kind, 30)}
        </span>
      </span>
      <span className={styles.documentPreviewCopy}>
        <strong>{work.title}</strong>
        <span>{kindLabel(work.kind)}</span>
      </span>
    </div>
  );
}

function WorkCard({
  work,
  onOpen,
  onEdit,
  onCoauthor,
  onOpenSource,
  onRemove,
  onGenerateCover,
  canGenerateCover,
  generatingCover,
  startingEdit,
  startingCoauthor,
  canCoauthor,
}: {
  work: WorkItem;
  onOpen: (work: WorkItem) => void;
  onEdit: (work: WorkItem) => void;
  onCoauthor: (work: WorkItem) => void;
  onOpenSource: (work: WorkItem) => void;
  onRemove: (work: WorkItem) => void;
  onGenerateCover: (work: WorkItem) => void;
  canGenerateCover: boolean;
  generatingCover: boolean;
  startingEdit: boolean;
  startingCoauthor: boolean;
  canCoauthor: boolean;
}) {
  const usability = workUsability(work);
  const menuItems: MenuProps["items"] = [
    ...(canCoauthor
      ? [
          {
            key: "private-editor",
            label: startingCoauthor ? "正在继续共作" : "继续共作",
            icon: <BookOpenCheck size={15} aria-hidden="true" />,
            disabled: startingCoauthor,
            onClick: () => onCoauthor(work),
          },
        ]
      : []),
    {
      key: "edit",
      label: startingEdit ? "正在创建修改会话" : "修改作品",
      icon: <PencilLine size={15} aria-hidden="true" />,
      disabled: startingEdit || !usability.usable,
      onClick: () => onEdit(work),
    },
    ...(workSourceDestination(work)
      ? [
          {
            key: "source",
            label: work.source_chat_id ? "回到来源对话" : "查看来源任务",
            icon: <MessageSquareText size={15} aria-hidden="true" />,
            onClick: () => onOpenSource(work),
          },
        ]
      : []),
    ...(canGenerateCover
      ? [
          {
            key: "cover",
            label: work.cover_path ? "重新生成 AI 封面" : "生成 AI 封面",
            icon: <Sparkles size={15} aria-hidden="true" />,
            disabled: generatingCover || !usability.usable,
            onClick: () => onGenerateCover(work),
          },
        ]
      : []),
    {
      key: "remove",
      label: "从作品移除",
      danger: true,
      icon: <Trash2 size={15} aria-hidden="true" />,
      onClick: () => onRemove(work),
    },
  ];
  return (
    <article className={styles.workCard}>
      <button
        type="button"
        className={styles.workPreviewButton}
        onClick={() => onOpen(work)}
        aria-label={`打开作品：${work.title}`}
      >
        <WorkPreviewSurface work={work} />
        {work.kind === "html" && usability.usable ? (
          <span className={styles.playBadge}>
            <Play size={13} fill="currentColor" aria-hidden="true" />
            运行
          </span>
        ) : null}
      </button>
      <div className={styles.workCardBody}>
        <div className={styles.workCardCopy}>
          <strong title={work.title}>{work.title}</strong>
          <span>
            V{work.version || 1} · {kindLabel(work.kind)}
            {work.size ? ` · ${formatFileSize(work.size)}` : ""}
          </span>
          {!usability.usable ? (
            <span className={styles.workStateBadge}>{usability.label}</span>
          ) : null}
        </div>
        <Dropdown
          menu={{ items: menuItems }}
          trigger={["click"]}
          placement="bottomRight"
        >
          <button
            type="button"
            className={styles.moreButton}
            aria-label={`更多操作：${work.title}`}
          >
            <MoreHorizontal size={18} aria-hidden="true" />
          </button>
        </Dropdown>
      </div>
    </article>
  );
}

export default function WorksPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { message, modal } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const loadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());
  const previousAgentRef = useRef(selectedAgent);
  const [works, setWorks] = useState<WorkItem[]>([]);
  const [runs, setRuns] = useState<TaskRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [indexInspection, setIndexInspection] =
    useState<WorksIndexInspection | null>(null);
  const [indexRecoveryBusy, setIndexRecoveryBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>(storedViewMode);
  const [query, setQuery] = useState("");
  const [recentOpen, setRecentOpen] = useState(false);
  const [addingPaths, setAddingPaths] = useState<Set<string>>(new Set());
  const [previewWork, setPreviewWork] = useState<WorkItem | null>(null);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewText, setPreviewText] = useState("");
  const [coverConfig, setCoverConfig] =
    useState<ImageGenerationConfig>(DEFAULT_COVER_CONFIG);
  const [coverSettingsDraft, setCoverSettingsDraft] =
    useState<ImageGenerationConfig>(DEFAULT_COVER_CONFIG);
  const [coverSettingsOpen, setCoverSettingsOpen] = useState(false);
  const [savingCoverSettings, setSavingCoverSettings] = useState(false);
  const [generatingCoverIds, setGeneratingCoverIds] = useState<Set<string>>(
    new Set(),
  );
  const [startingEditIds, setStartingEditIds] = useState<Set<string>>(
    new Set(),
  );
  const [startingCoauthorIds, setStartingCoauthorIds] = useState<Set<string>>(
    new Set(),
  );
  const [privateEditorEnabled, setPrivateEditorEnabled] = useState(false);

  const load = useCallback(
    async (quiet = false) => {
      const ownsState = loadGateRef.current.begin();
      if (quiet) setRefreshing(true);
      else setLoading(true);
      setLoadError("");
      setIndexInspection(null);
      try {
        const [saved, taskRuns, imageConfig, privateEditorSettings] =
          await Promise.all([
            worksApi.list(),
            capabilityApi.listTaskRuns().catch(() => []),
            imageGenerationApi
              .getImageGenerationConfig()
              .catch(() => DEFAULT_COVER_CONFIG),
            privateEditorApi.getSettings().catch(() => null),
          ]);
        if (!ownsState()) return;
        setWorks(Array.isArray(saved) ? saved : []);
        setRuns(Array.isArray(taskRuns) ? taskRuns : []);
        setCoverConfig({ ...DEFAULT_COVER_CONFIG, ...imageConfig });
        setPrivateEditorEnabled(Boolean(privateEditorSettings?.enabled));
      } catch (reason) {
        if (!ownsState()) return;
        setLoadError(reason instanceof Error ? reason.message : "作品读取失败");
        const inspection = await worksApi.inspectIndex().catch(() => null);
        if (!ownsState()) return;
        setIndexInspection(inspection);
        message.error(
          reason instanceof Error ? reason.message : "作品读取失败",
        );
      } finally {
        if (ownsState()) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [message],
  );

  useEffect(() => {
    const loadGate = loadGateRef.current;
    const mutationScope = mutationScopeRef.current;
    if (previousAgentRef.current !== selectedAgent) {
      Modal.destroyAll();
    }
    previousAgentRef.current = selectedAgent;
    mutationScope.invalidate();
    loadGate.invalidate();
    setWorks([]);
    setRuns([]);
    setPreviewWork(null);
    setRecentOpen(false);
    setCoverSettingsOpen(false);
    setSavingCoverSettings(false);
    setQuery("");
    setLoadError("");
    setIndexInspection(null);
    setIndexRecoveryBusy(false);
    setAddingPaths(new Set());
    setGeneratingCoverIds(new Set());
    setStartingEditIds(new Set());
    setStartingCoauthorIds(new Set());
    setPrivateEditorEnabled(false);
    void load();
    return () => {
      loadGate.invalidate();
      mutationScope.invalidate();
    };
  }, [load, modal, selectedAgent]);

  useEffect(() => {
    try {
      window.localStorage.setItem(VIEW_MODE_KEY, viewMode);
    } catch {
      // Ignore restricted storage contexts.
    }
  }, [viewMode]);

  useEffect(() => {
    let active = true;
    let objectUrl = "";
    setPreviewUrl("");
    setPreviewText("");
    if (!previewWork || previewWork.kind === "html") return;
    if (!workUsability(previewWork).usable) return;
    if (!canInlinePreviewWork(previewWork)) return;
    void worksApi
      .contentBlob(previewWork.id)
      .then(async (blob) => {
        if (!active) return;
        if (isTextualWork(previewWork)) {
          setPreviewText(await blob.text());
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setPreviewUrl(objectUrl);
      })
      .catch((reason) =>
        message.error(
          reason instanceof Error ? reason.message : "无法预览作品",
        ),
      );
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [message, previewWork]);

  const candidates = useMemo(() => artifactCandidates(runs), [runs]);
  const savedSources = useMemo(
    () => new Set(works.map((item) => item.source_path)),
    [works],
  );
  const availableCandidates = candidates.filter(
    (item) => !savedSources.has(item.path),
  );
  const visibleWorks = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return works;
    return works.filter((work) =>
      `${work.title} ${kindLabel(work.kind)} ${work.path}`
        .toLocaleLowerCase()
        .includes(normalized),
    );
  }, [query, works]);

  const generateCover = useCallback(
    async (work: WorkItem, quiet = false) => {
      const usability = workUsability(work);
      if (!usability.usable) {
        message.warning(usability.detail || "这个保存版本当前不可用");
        return;
      }
      const ownsScope = mutationScopeRef.current.capture();
      setGeneratingCoverIds((current) => new Set(current).add(work.id));
      try {
        const updated = await worksApi.generateCover(work.id);
        if (!ownsScope()) return;
        setWorks((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)),
        );
        if (!quiet) message.success("已生成作品封面");
      } catch (reason) {
        if (!ownsScope()) return;
        message.error(
          reason instanceof Error ? reason.message : "作品封面生成失败",
        );
      } finally {
        if (ownsScope()) {
          setGeneratingCoverIds((current) => {
            const next = new Set(current);
            next.delete(work.id);
            return next;
          });
        }
      }
    },
    [message],
  );

  const confirmGenerateCover = useCallback(
    (work: WorkItem) => {
      const usability = workUsability(work);
      if (!usability.usable) {
        message.warning(usability.detail || "这个保存版本当前不可用");
        return;
      }
      modal.confirm({
        title: work.cover_path ? "重新生成 AI 封面？" : "生成 AI 封面？",
        content: work.cover_path
          ? "现有封面会被新结果替换，并会再次调用图片模型、消耗相应额度。"
          : "这会调用图片模型并消耗相应额度。生成完成前可继续浏览其他作品。",
        okText: work.cover_path ? "重新生成" : "开始生成",
        cancelText: "取消",
        onOk: () => generateCover(work),
      });
    },
    [generateCover, message, modal],
  );

  const addCandidate = async (candidate: WorkCandidate) => {
    const ownsScope = mutationScopeRef.current.capture();
    setAddingPaths((current) => new Set(current).add(candidate.path));
    try {
      const work = await worksApi.add({
        path: candidate.path,
        title: candidate.title,
        source_chat_id: candidate.chatId,
        source_run_id: candidate.runId,
      });
      if (!ownsScope()) return;
      setWorks((current) => [
        work,
        ...current.filter((item) => item.id !== work.id),
      ]);
      message.success("已加入作品");
      if (coverConfig.works_cover_enabled) {
        void generateCover(work, true);
      }
    } catch (reason) {
      if (!ownsScope()) return;
      message.error(reason instanceof Error ? reason.message : "加入作品失败");
    } finally {
      if (ownsScope()) {
        setAddingPaths((current) => {
          const next = new Set(current);
          next.delete(candidate.path);
          return next;
        });
      }
    }
  };

  const openCoverSettings = () => {
    setCoverSettingsDraft(coverConfig);
    setCoverSettingsOpen(true);
  };

  const saveCoverSettings = async () => {
    if (
      coverSettingsDraft.works_cover_enabled &&
      (!coverSettingsDraft.works_cover_model.provider_id ||
        !coverSettingsDraft.works_cover_model.model)
    ) {
      message.warning("请先选择用于作品封面的生图模型");
      return;
    }
    const ownsScope = mutationScopeRef.current.capture();
    setSavingCoverSettings(true);
    try {
      const saved =
        await imageGenerationApi.updateImageGenerationConfig(
          coverSettingsDraft,
        );
      if (!ownsScope()) return;
      setCoverConfig(saved);
      setCoverSettingsDraft(saved);
      setCoverSettingsOpen(false);
      message.success("作品封面设置已保存");
    } catch (reason) {
      if (!ownsScope()) return;
      message.error(reason instanceof Error ? reason.message : "设置保存失败");
    } finally {
      if (ownsScope()) setSavingCoverSettings(false);
    }
  };

  const openWork = useCallback(
    async (work: WorkItem) => {
      const usability = workUsability(work);
      if (!usability.usable) {
        message.warning(usability.detail || "这个保存版本当前不可用");
        return;
      }
      const api = window.electronAPI;
      if (work.kind === "html" && api?.openHtmlWork) {
        const result = await api.openHtmlWork(work.path, work.title);
        if (!result.success) message.error(result.error || "无法运行此作品");
        return;
      }
      // Saved Works are immutable versions. Never hand their managed paths to a
      // writable editor; "修改作品" first creates an isolated edit branch.
      setPreviewWork(work);
    },
    [message],
  );

  useEffect(() => {
    const requestedWorkId = searchParams.get("work");
    if (!requestedWorkId || loading) return;
    const work = works.find((item) => item.id === requestedWorkId);
    if (!work) return;
    void openWork(work);
    const next = new URLSearchParams(searchParams);
    next.delete("work");
    setSearchParams(next, { replace: true });
  }, [loading, openWork, searchParams, setSearchParams, works]);

  const revealWork = useCallback(
    async (work: WorkItem) => {
      const usability = workUsability(work);
      if (!usability.usable) {
        message.warning(usability.detail || "这个保存版本当前不可用");
        return;
      }
      const result = await window.electronAPI?.revealPath?.(work.path);
      if (!result?.success) {
        message.error(result?.error || "无法在访达中显示作品");
      }
    },
    [message],
  );

  const exportWorkCopy = useCallback(
    async (work: WorkItem) => {
      const usability = workUsability(work);
      if (!usability.usable) {
        message.warning(usability.detail || "这个保存版本当前不可用");
        return;
      }
      const result = await window.electronAPI?.saveFileAs?.({
        sourcePath: work.path,
        filename: work.path.split(/[\\/]/).pop() || work.title,
      });
      if (!result || result.canceled) return;
      if (result.success) message.success("作品副本已导出");
      else message.error(result.error || "无法导出作品副本");
    },
    [message],
  );

  const editWork = async (work: WorkItem) => {
    const usability = workUsability(work);
    if (!usability.usable) {
      message.warning(usability.detail || "这个保存版本当前不可用");
      return;
    }
    const ownsScope = mutationScopeRef.current.capture();
    setStartingEditIds((current) => new Set(current).add(work.id));
    try {
      const chat = await worksApi.createEditSession(work.id);
      if (!ownsScope()) return;
      message.success(`已从 V${work.version || 1} 创建独立修改副本`);
      navigate(`/chat/${encodeURIComponent(chat.id)}`);
    } catch (reason) {
      if (!ownsScope()) return;
      message.error(
        reason instanceof Error ? reason.message : "无法创建作品修改会话",
      );
    } finally {
      if (ownsScope()) {
        setStartingEditIds((current) => {
          const next = new Set(current);
          next.delete(work.id);
          return next;
        });
      }
    }
  };

  const continueCoauthoring = async (work: WorkItem) => {
    const ownsScope = mutationScopeRef.current.capture();
    setStartingCoauthorIds((current) => new Set(current).add(work.id));
    try {
      const session = await privateEditorApi.startOrContinue(work.id);
      if (!ownsScope()) return;
      message.success("已连接这件作品的编辑记录");
      navigate(
        `/chat/${encodeURIComponent(session.chat.id)}?privateEditorProject=${encodeURIComponent(session.project.id)}`,
      );
    } catch (reason) {
      if (!ownsScope()) return;
      message.error(
        reason instanceof Error ? reason.message : "无法继续这件作品",
      );
    } finally {
      if (ownsScope()) {
        setStartingCoauthorIds((current) => {
          const next = new Set(current);
          next.delete(work.id);
          return next;
        });
      }
    }
  };

  const openWorkSource = (work: WorkItem) => {
    const destination = workSourceDestination(work);
    if (!destination) {
      message.warning("这个作品没有可打开的来源记录。");
      return;
    }
    navigate(destination);
  };

  const confirmRemove = (work: WorkItem) => {
    const ownsScope = mutationScopeRef.current.capture();
    modal.confirm({
      title: `从作品移除“${work.title}”？`,
      content: "只会移除澜台保存的作品副本，不会删除原始文件。",
      okText: "移除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      async onOk() {
        if (!ownsScope()) return;
        await worksApi.remove(work.id);
        if (!ownsScope()) return;
        setWorks((current) => current.filter((item) => item.id !== work.id));
        message.success("已从作品移除");
      },
    });
  };

  const previewAndRecoverIndex = useCallback(async () => {
    const backup = indexInspection?.backups[0];
    if (!backup) {
      message.warning("没有可自动恢复的最后良好索引备份");
      return;
    }
    const ownsScope = mutationScopeRef.current.capture();
    setIndexRecoveryBusy(true);
    try {
      const preview = await worksApi.previewIndexRecovery(backup.digest);
      if (!ownsScope()) return;
      modal.confirm({
        title: "恢复作品索引？",
        content: (
          <div>
            <p>
              将恢复 {preview.snapshot_works.length}{" "}
              条作品记录。当前索引原件会先隔离保留，作品文件不会被删除。
            </p>
            {preview.orphaned_managed_ids.length > 0 ? (
              <p>
                恢复后仍有 {preview.orphaned_managed_ids.length}
                个未登记的作品目录，它们会原样保留以便后续核对。
              </p>
            ) : null}
          </div>
        ),
        okText: "确认恢复",
        cancelText: "取消",
        async onOk() {
          if (!ownsScope()) return;
          setIndexRecoveryBusy(true);
          try {
            const result = await worksApi.restoreIndex(
              preview.current_digest,
              preview.snapshot_digest,
            );
            if (!ownsScope()) return;
            message.success(
              result.status === "already_restored"
                ? "作品索引已在恢复状态"
                : `已恢复 ${result.work_count} 条作品记录`,
            );
            await load();
          } catch (reason) {
            if (!ownsScope()) return;
            const reconciled = await worksApi.inspectIndex().catch(() => null);
            if (!ownsScope()) return;
            if (
              reconciled?.status === "ready" &&
              reconciled.current_digest === preview.snapshot_digest
            ) {
              message.success("已核对：作品索引恢复成功");
              await load();
              return;
            }
            setIndexInspection(reconciled);
            message.error(
              `${
                reason instanceof Error ? reason.message : "作品索引恢复未完成"
              }。已重新诊断，没有自动重试。`,
            );
          } finally {
            if (ownsScope()) setIndexRecoveryBusy(false);
          }
        },
      });
    } catch (reason) {
      if (!ownsScope()) return;
      message.error(
        reason instanceof Error ? reason.message : "无法预览作品索引恢复",
      );
    } finally {
      if (ownsScope()) setIndexRecoveryBusy(false);
    }
  }, [indexInspection, load, message, modal]);

  const previewBody = (() => {
    if (!previewWork) return null;
    if (previewWork.kind === "image" && previewUrl) {
      return (
        <img
          className={styles.modalImage}
          src={previewUrl}
          alt={previewWork.title}
        />
      );
    }
    if (previewWork.kind === "pdf" && previewUrl) {
      return (
        <iframe
          className={styles.modalFrame}
          src={previewUrl}
          title={previewWork.title}
        />
      );
    }
    if (previewWork.kind === "video" && previewUrl) {
      return (
        <video
          className={styles.modalMedia}
          src={previewUrl}
          controls
          autoPlay
        />
      );
    }
    if (previewWork.kind === "audio" && previewUrl) {
      return (
        <audio
          className={styles.modalAudio}
          src={previewUrl}
          controls
          autoPlay
        />
      );
    }
    if (isTextualWork(previewWork)) {
      return (
        <pre className={styles.modalText}>{previewText || "正在读取..."}</pre>
      );
    }
    return (
      <div className={styles.unsupportedPreview}>
        <span>{iconForKind(previewWork.kind, 36)}</span>
        <strong>{previewWork.title}</strong>
        <p>
          当前类型不能在澜台内预览。你仍可导出一个副本、在访达中查看，或创建独立修改副本。
        </p>
      </div>
    );
  })();

  return (
    <main className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <h1>作品</h1>
          <p>
            保存任务产物的独立版本，方便继续展示和修改；从作品移除不会删除原始文件。
          </p>
        </div>
        <button
          type="button"
          className={styles.primaryButton}
          onClick={() => setRecentOpen(true)}
        >
          <FolderPlus size={17} aria-hidden="true" />
          从最近产物添加
          {availableCandidates.length > 0 ? (
            <span>{availableCandidates.length}</span>
          ) : null}
        </button>
      </header>

      <div className={styles.toolbar}>
        <label className={styles.searchBox}>
          <Search size={16} aria-hidden="true" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索作品"
            aria-label="搜索作品"
          />
          {query ? (
            <button
              type="button"
              onClick={() => setQuery("")}
              aria-label="清除搜索"
            >
              <X size={14} aria-hidden="true" />
            </button>
          ) : null}
        </label>
        <div className={styles.toolbarActions}>
          <Tooltip title="作品封面设置">
            <button
              type="button"
              className={styles.iconButton}
              onClick={openCoverSettings}
              aria-label="作品封面设置"
            >
              <Settings2 size={16} aria-hidden="true" />
            </button>
          </Tooltip>
          <Tooltip title="刷新">
            <button
              type="button"
              className={styles.iconButton}
              onClick={() => void load(true)}
              aria-label="刷新作品"
              disabled={refreshing}
            >
              <RefreshCw
                size={16}
                className={refreshing ? styles.spinning : ""}
                aria-hidden="true"
              />
            </button>
          </Tooltip>
          <div
            className={styles.viewSwitch}
            role="group"
            aria-label="作品显示方式"
          >
            <button
              type="button"
              className={viewMode === "gallery" ? styles.active : ""}
              onClick={() => setViewMode("gallery")}
              aria-label="截图视图"
              aria-pressed={viewMode === "gallery"}
            >
              <Grid2X2 size={16} aria-hidden="true" />
            </button>
            <button
              type="button"
              className={viewMode === "list" ? styles.active : ""}
              onClick={() => setViewMode("list")}
              aria-label="列表视图"
              aria-pressed={viewMode === "list"}
            >
              <List size={17} aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>

      {loading ? (
        <div className={styles.loadingGrid} aria-label="正在加载作品">
          {[0, 1, 2, 3].map((item) => (
            <Skeleton.Node key={item} active className={styles.loadingCard} />
          ))}
        </div>
      ) : loadError ? (
        <section className={styles.emptyState} role="alert">
          <h2>
            {indexInspection?.status === "recovery_required"
              ? "作品索引需要恢复"
              : indexInspection?.status === "unavailable"
                ? "作品索引暂不可用"
                : "无法加载作品"}
          </h2>
          <p>{loadError}</p>
          {indexInspection?.status === "recovery_required" ||
          indexInspection?.status === "unavailable" ? (
            <p>
              索引写入已暂停；恢复流程不会删除现有作品文件。
              {indexInspection.diagnostic_id
                ? `诊断编号：${indexInspection.diagnostic_id}。`
                : ""}
              {indexInspection.invalid_record_indexes.length > 0
                ? `需核对的记录：${indexInspection.invalid_record_indexes
                    .map((index) => index + 1)
                    .join("、")}。`
                : ""}
            </p>
          ) : null}
          {indexInspection?.status === "recovery_required" &&
          indexInspection.backups.length > 0 ? (
            <button
              type="button"
              className={styles.secondaryButton}
              onClick={() => void previewAndRecoverIndex()}
              disabled={indexRecoveryBusy}
            >
              {indexRecoveryBusy ? "正在核对备份" : "预览并恢复最近备份"}
            </button>
          ) : null}
          <button
            type="button"
            className={styles.secondaryButton}
            onClick={() => void load()}
          >
            重新加载
          </button>
        </section>
      ) : visibleWorks.length === 0 ? (
        <section className={styles.emptyState}>
          <div className={styles.emptyVisual}>
            <FileImage size={30} aria-hidden="true" />
            <AppWindow size={34} aria-hidden="true" />
            <Presentation size={30} aria-hidden="true" />
          </div>
          <h2>{query ? "没有匹配的作品" : "把完成的产物留在这里"}</h2>
          <p>
            {query
              ? "试试其他关键词。"
              : "以后想继续玩、展示或编辑时，不用回到旧对话里翻找。"}
          </p>
          {!query ? (
            <button
              type="button"
              className={styles.secondaryButton}
              onClick={() => setRecentOpen(true)}
            >
              从最近产物添加
            </button>
          ) : null}
        </section>
      ) : viewMode === "gallery" ? (
        <section className={styles.gallery} aria-label="作品截图视图">
          {visibleWorks.map((work) => (
            <WorkCard
              key={work.id}
              work={work}
              onOpen={openWork}
              onEdit={(item) => void editWork(item)}
              onCoauthor={(item) => void continueCoauthoring(item)}
              onOpenSource={openWorkSource}
              onRemove={confirmRemove}
              onGenerateCover={confirmGenerateCover}
              canGenerateCover={coverConfig.works_cover_enabled}
              generatingCover={generatingCoverIds.has(work.id)}
              startingEdit={startingEditIds.has(work.id)}
              startingCoauthor={startingCoauthorIds.has(work.id)}
              canCoauthor={privateEditorEnabled && supportsPrivateEditor(work)}
            />
          ))}
        </section>
      ) : (
        <section className={styles.list} aria-label="作品列表视图">
          {visibleWorks.map((work) => (
            <article key={work.id} className={styles.listRow}>
              <button
                type="button"
                className={styles.listOpenButton}
                onClick={() => void openWork(work)}
              >
                <span className={styles.listIcon}>
                  {iconForKind(work.kind)}
                </span>
                <span className={styles.listCopy}>
                  <strong>{work.title}</strong>
                  <span>
                    V{work.version || 1} · {kindLabel(work.kind)}
                  </span>
                  {!workUsability(work).usable ? (
                    <span className={styles.workStateBadge}>
                      {workUsability(work).label}
                    </span>
                  ) : null}
                </span>
                <span className={styles.listMeta}>
                  {formatFileSize(work.size)}
                </span>
                <span className={styles.listMeta}>
                  {formatDate(work.updated_at)}
                </span>
              </button>
              <Dropdown
                menu={{
                  items: [
                    ...(privateEditorEnabled && supportsPrivateEditor(work)
                      ? [
                          {
                            key: "private-editor",
                            label: startingCoauthorIds.has(work.id)
                              ? "正在继续共作"
                              : "继续共作",
                            icon: (
                              <BookOpenCheck size={15} aria-hidden="true" />
                            ),
                            disabled: startingCoauthorIds.has(work.id),
                            onClick: () => void continueCoauthoring(work),
                          },
                        ]
                      : []),
                    {
                      key: "edit",
                      label: startingEditIds.has(work.id)
                        ? "正在创建修改会话"
                        : "修改作品",
                      icon: <PencilLine size={15} aria-hidden="true" />,
                      disabled:
                        startingEditIds.has(work.id) ||
                        !workUsability(work).usable,
                      onClick: () => void editWork(work),
                    },
                    ...(workSourceDestination(work)
                      ? [
                          {
                            key: "source",
                            label: work.source_chat_id
                              ? "回到来源对话"
                              : "查看来源任务",
                            icon: (
                              <MessageSquareText size={15} aria-hidden="true" />
                            ),
                            onClick: () => openWorkSource(work),
                          },
                        ]
                      : []),
                    ...(coverConfig.works_cover_enabled
                      ? [
                          {
                            key: "cover",
                            label: work.cover_path
                              ? "重新生成 AI 封面"
                              : "生成 AI 封面",
                            icon: <Sparkles size={15} aria-hidden="true" />,
                            disabled:
                              generatingCoverIds.has(work.id) ||
                              !workUsability(work).usable,
                            onClick: () => confirmGenerateCover(work),
                          },
                        ]
                      : []),
                    {
                      key: "remove",
                      label: "从作品移除",
                      danger: true,
                      icon: <Trash2 size={15} aria-hidden="true" />,
                      onClick: () => confirmRemove(work),
                    },
                  ],
                }}
                trigger={["click"]}
                placement="bottomRight"
              >
                <button
                  type="button"
                  className={styles.moreButton}
                  aria-label={`更多操作：${work.title}`}
                >
                  <MoreHorizontal size={18} aria-hidden="true" />
                </button>
              </Dropdown>
            </article>
          ))}
        </section>
      )}

      <Modal
        open={coverSettingsOpen}
        onCancel={() => setCoverSettingsOpen(false)}
        onOk={() => void saveCoverSettings()}
        okText="保存设置"
        cancelText="取消"
        confirmLoading={savingCoverSettings}
        title="作品封面"
        width={520}
        destroyOnHidden
      >
        <div className={styles.coverSettings}>
          <div className={styles.coverSettingRow}>
            <span>
              <strong>为作品启用 AI 封面</strong>
              <small>新作品加入后自动生成，会消耗生图模型额度。</small>
            </span>
            <Switch
              checked={coverSettingsDraft.works_cover_enabled}
              disabled={coverSettingsDraft.models.length === 0}
              onChange={(checked) =>
                setCoverSettingsDraft((current) => ({
                  ...current,
                  works_cover_enabled: checked,
                }))
              }
              aria-label="为作品启用 AI 封面"
            />
          </div>
          <label className={styles.coverModelField}>
            <span>封面模型</span>
            <Select
              value={
                coverSettingsDraft.works_cover_model.provider_id
                  ? modelSlotValue(coverSettingsDraft.works_cover_model)
                  : undefined
              }
              disabled={coverSettingsDraft.models.length === 0}
              placeholder="选择已配置的生图模型"
              options={coverSettingsDraft.models.map((slot) => ({
                value: modelSlotValue(slot),
                label: `${slot.provider_id} / ${slot.model}`,
              }))}
              onChange={(value) => {
                const [provider_id, model] = String(value).split("\u0000");
                setCoverSettingsDraft((current) => ({
                  ...current,
                  works_cover_model: { provider_id, model },
                }));
              }}
            />
          </label>
          {coverSettingsDraft.models.length === 0 ? (
            <p className={styles.coverSettingHint}>
              请先在「设置 → 模型 →
              对话内生图」中配置至少一个模型。标题排版封面仍然始终可用。
            </p>
          ) : (
            <p className={styles.coverSettingHint}>
              AI
              只生成无文字的视觉画面，真实作品标题由澜台叠加排版，避免乱码和错别字。
            </p>
          )}
        </div>
      </Modal>

      <Modal
        open={recentOpen}
        onCancel={() => setRecentOpen(false)}
        footer={null}
        title="最近产物"
        width={640}
        destroyOnHidden
      >
        <p className={styles.recentIntro}>
          选择值得保留的产物。加入后会保存独立副本，不受原对话清理影响。
        </p>
        <div className={styles.candidateList}>
          {availableCandidates.length === 0 ? (
            <div className={styles.candidateEmpty}>最近没有可添加的产物。</div>
          ) : (
            availableCandidates.slice(0, 30).map((candidate) => {
              const artifact = runs
                .find((run) => run.id === candidate.runId)
                ?.artifacts.find(
                  (item) => `${candidate.runId}:${item.id}` === candidate.key,
                );
              const kind = artifact ? workKindFromArtifact(artifact) : "file";
              return (
                <div key={candidate.key} className={styles.candidateRow}>
                  <span className={styles.candidateIcon}>
                    {iconForKind(kind)}
                  </span>
                  <span className={styles.candidateCopy}>
                    <strong>{candidate.title}</strong>
                    <span>
                      {candidate.runTitle} · {formatDate(candidate.createdAt)}
                    </span>
                  </span>
                  <button
                    type="button"
                    className={styles.addButton}
                    onClick={() => void addCandidate(candidate)}
                    disabled={addingPaths.has(candidate.path)}
                  >
                    {addingPaths.has(candidate.path) ? "正在添加" : "加入作品"}
                  </button>
                </div>
              );
            })
          )}
        </div>
      </Modal>

      <Modal
        open={Boolean(previewWork)}
        onCancel={() => setPreviewWork(null)}
        footer={
          previewWork ? (
            <div className={styles.previewActions}>
              <button
                type="button"
                className={styles.secondaryButton}
                onClick={() => void revealWork(previewWork)}
              >
                在访达中显示
              </button>
              <button
                type="button"
                className={styles.secondaryButton}
                onClick={() => void exportWorkCopy(previewWork)}
              >
                导出副本
              </button>
              <button
                type="button"
                className={styles.primaryButton}
                onClick={() => void editWork(previewWork)}
              >
                创建修改副本
              </button>
              {privateEditorEnabled && supportsPrivateEditor(previewWork) ? (
                <button
                  type="button"
                  className={styles.primaryButton}
                  onClick={() => void continueCoauthoring(previewWork)}
                  disabled={startingCoauthorIds.has(previewWork.id)}
                >
                  <BookOpenCheck size={15} aria-hidden="true" />
                  {startingCoauthorIds.has(previewWork.id)
                    ? "正在继续共作"
                    : "继续共作"}
                </button>
              ) : null}
            </div>
          ) : null
        }
        title={previewWork?.title}
        width="min(960px, calc(100vw - 48px))"
        destroyOnHidden
        className={styles.previewModal}
      >
        <div className={styles.previewContent}>
          {previewBody}
          {previewWork ? (
            <footer className={styles.previewLineage}>
              <span>
                <strong>这是澜台保存的作品副本</strong>
                <small>原任务文件和来源记录仍然各自保留。</small>
              </span>
              {workSourceDestination(previewWork) ? (
                <button
                  type="button"
                  className={styles.secondaryButton}
                  onClick={() => openWorkSource(previewWork)}
                >
                  <MessageSquareText size={15} aria-hidden="true" />
                  {previewWork.source_chat_id ? "回到来源对话" : "查看来源任务"}
                </button>
              ) : null}
            </footer>
          ) : null}
        </div>
      </Modal>
    </main>
  );
}
