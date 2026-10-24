import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Dropdown, Empty, Spin, Tooltip, type MenuProps } from "antd";
import { Markdown } from "@/vendor/lantaiChat";
import {
  ArrowLeft,
  ChevronDown,
  ChevronRight,
  Download,
  File,
  FileCode2,
  Folder,
  FolderOpen,
  MonitorPlay,
  MoreHorizontal,
  PanelRightClose,
  PencilLine,
  RefreshCw,
  X,
} from "lucide-react";
import { codingApi } from "../../../api/modules/coding";
import { chatApi } from "../../../api/modules/chat";
import { worksApi } from "../../../api/modules/works";
import type {
  CodeFileContentResponse,
  CodeFileNode,
  CodeFileTreeResponse,
} from "../../../api/types";
import type { ChatRuntimeState } from "../../../api/types/chat";
import type {
  WorkEditCopy,
  WorkItem,
  WorkPreview,
  WorkTextPreview,
} from "../../../api/types/works";
import {
  getActiveTopicWorkspace,
  TOPIC_WORKSPACE_CHANGED_EVENT,
} from "../../../utils/topicWorkspace";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import { getAttachmentRoleLabel } from "../../../vendor/lantaiAttachmentRoles";
import styles from "./ChatArtifactPanel.module.less";
import {
  coCreationObjectLabelForFile,
  CO_CREATION_PHASE_LABELS,
  CO_CREATION_SURFACES,
} from "../coCreationLanguage";
import {
  activateArtifactTab,
  closeArtifactTab,
  loadArtifactWorkbenchState,
  openArtifactTab,
  reconcileArtifactWorkbenchState,
  saveArtifactWorkbenchState,
  type ArtifactWorkbenchState,
  type ArtifactWorkbenchView,
} from "./artifactWorkbenchState";
import {
  attachmentRolesFromMessages,
  buildArtifactEditTarget,
  projectArtifactOutcome,
  selectPrimaryOutcome,
  type ArtifactEditTarget,
  type CanonicalAttachmentProjection,
} from "./artifactExperience";

export type ChatArtifactView = "files" | "source" | "preview";
export type ChatArtifactPanelMode = "compact" | "workbench" | "studio";

interface ChatArtifactPanelProps {
  open: boolean;
  chatId?: string;
  requestedPath?: string | null;
  onClose: () => void;
  onPrepareArtifactEdit?: (input: {
    target: ArtifactEditTarget;
    title: string;
  }) => void;
}

type ArtifactPanelSurface = "outcome" | "files";
type OutcomePreviewStatus =
  "idle" | "loading" | "ready" | "unsupported" | "error";

interface FileTreeRowProps {
  node: CodeFileNode;
  depth: number;
  activePath: string | null;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  onOpen: (path: string) => void;
  onActivate: (path: string) => void;
  actionItems: (node: CodeFileNode) => MenuProps["items"];
  attachmentRoles: Map<string, CanonicalAttachmentProjection>;
}

const PREVIEWABLE_HTML = /\.html?$/i;
const PREVIEWABLE_MARKDOWN = /\.m(?:d|arkdown)$/i;
const EDITABLE_OFFICE = /\.(docx|xlsx|xls|csv|pptx|pdf|md|markdown)$/i;
const NATIVE_OPEN_EXTENSIONS = new Set([
  "7z",
  "app",
  "avi",
  "bin",
  "doc",
  "docx",
  "dmg",
  "exe",
  "csv",
  "gif",
  "heic",
  "jpeg",
  "jpg",
  "key",
  "m4a",
  "mov",
  "mp3",
  "mp4",
  "numbers",
  "odp",
  "ods",
  "odt",
  "pages",
  "pdf",
  "pkg",
  "png",
  "ppt",
  "pptx",
  "rar",
  "rtf",
  "tar",
  "tiff",
  "wav",
  "webp",
  "xls",
  "xlsx",
  "msi",
  "zip",
]);
const DEFAULT_TREE_DEPTH = 4;
const DEFAULT_TREE_LIMIT = 500;
const MANAGED_WORKSPACE_ROOT = /\/\.lantai\/workspaces\/[^/]+\/?$/;
const PANEL_WIDTH_STORAGE_KEY = "lantai:chat-artifact-panel-width";
const DEFAULT_PANEL_WIDTH = 320;
const MIN_PANEL_WIDTH = 240;
const MAX_PANEL_WIDTH = 960;
const MIN_CHAT_STAGE_WIDTH = 390;
const WORKBENCH_PANEL_WIDTH = 520;
const STUDIO_PANEL_WIDTH = 720;
const MAX_INLINE_TEXT_BYTES = 2 * 1024 * 1024;
const TEXT_WORK_EXTENSIONS =
  /\.(?:cjs|css|csv|html?|jsx?|json|m(?:d|arkdown)|py|rs|sh|tsx?|txt|ya?ml)$/i;

interface NativeOpenResult {
  success: boolean;
  canceled?: boolean;
  revealed?: boolean;
  error?: string;
}

function storedPanelWidth(): number | null {
  try {
    const value = Number(window.localStorage.getItem(PANEL_WIDTH_STORAGE_KEY));
    return Number.isFinite(value) && value >= MIN_PANEL_WIDTH ? value : null;
  } catch {
    return null;
  }
}

export function artifactPanelModeForWidth(
  width: number,
): ChatArtifactPanelMode {
  if (width >= STUDIO_PANEL_WIDTH) return "studio";
  if (width >= WORKBENCH_PANEL_WIDTH) return "workbench";
  return "compact";
}

export function prefersNativeFileOpen(path: string): boolean {
  const cleanPath = path.split(/[?#]/, 1)[0] || "";
  const extension = cleanPath.match(/\.([a-z0-9]+)$/i)?.[1]?.toLowerCase();
  return Boolean(extension && NATIVE_OPEN_EXTENSIONS.has(extension));
}

export function flattenArtifactFiles(nodes: CodeFileNode[]): CodeFileNode[] {
  const files: CodeFileNode[] = [];
  for (const node of nodes) {
    if (node.kind === "file") {
      files.push(node);
    }
    if (node.children?.length) {
      files.push(...flattenArtifactFiles(node.children));
    }
  }
  return files;
}

function flattenArtifactDirectories(nodes: CodeFileNode[]): CodeFileNode[] {
  const directories: CodeFileNode[] = [];
  for (const node of nodes) {
    if (node.kind !== "directory") continue;
    directories.push(node);
    if (node.children?.length) {
      directories.push(...flattenArtifactDirectories(node.children));
    }
  }
  return directories;
}

export function normalizeArtifactPath(
  requestedPath: string | null | undefined,
  rootPath: string,
): string {
  if (!requestedPath) return "";
  let normalized = requestedPath.trim();
  if (!normalized) return "";
  if (normalized.startsWith("file://")) {
    try {
      normalized = decodeURIComponent(new URL(normalized).pathname);
    } catch {
      normalized = normalized.slice("file://".length);
    }
  }
  normalized = normalized.replace(/:(?:L)?\d+(?:-\d+)?$/, "");
  const normalizedRoot = rootPath.replace(/\/+$/, "");
  if (normalizedRoot && normalized.startsWith(`${normalizedRoot}/`)) {
    return normalized.slice(normalizedRoot.length + 1);
  }
  return normalized.replace(/^\.?\//, "");
}

export function artifactSystemPath(
  rootPath: string,
  relativePath?: string | null,
): string {
  const root = rootPath.trim().replace(/\/+$/, "");
  if (!root) return "";
  if (!relativePath?.trim()) return root;

  let relative = relativePath.trim().replace(/\\/g, "/");
  if (relative === root) return root;
  if (relative.startsWith(`${root}/`)) {
    relative = relative.slice(root.length + 1);
  }
  if (relative.startsWith("/")) return "";

  const segments = relative.split("/").filter((segment) => segment !== ".");
  if (
    segments.length === 0 ||
    segments.some((segment) => !segment || segment === "..")
  ) {
    return "";
  }
  return `${root}/${segments.join("/")}`;
}

export function isManagedWorkspaceRoot(rootPath: string): boolean {
  return MANAGED_WORKSPACE_ROOT.test(rootPath.trim());
}

function retainRequestedArtifact(
  nodes: CodeFileNode[],
  requestedPath: string,
): CodeFileNode[] {
  const retained: CodeFileNode[] = [];
  for (const node of nodes) {
    if (node.kind === "file" && node.path === requestedPath) {
      retained.push(node);
      continue;
    }
    if (node.kind === "directory") {
      const children = retainRequestedArtifact(node.children, requestedPath);
      if (children.length > 0) retained.push({ ...node, children });
    }
  }
  return retained;
}

export function visibleArtifactNodes(
  nodes: CodeFileNode[],
  rootPath: string,
  requestedPath?: string | null,
): CodeFileNode[] {
  const userVisibleNodes = nodes.flatMap((node): CodeFileNode[] => {
    if (
      node.name === ".DS_Store" ||
      /^\.lantai-(?:topic-)?workspace(?:\.|$)/i.test(node.name)
    ) {
      return [];
    }
    if (node.kind === "directory") {
      return [{ ...node, children: visibleArtifactNodes(node.children, "") }];
    }
    return [node];
  });
  if (!isManagedWorkspaceRoot(rootPath)) return userVisibleNodes;
  const normalizedRequested = normalizeArtifactPath(requestedPath, rootPath);
  return normalizedRequested
    ? retainRequestedArtifact(userVisibleNodes, normalizedRequested)
    : [];
}

export function compactArtifactWorkspacePath(rootPath: string): string {
  const normalized = rootPath.replace(/\/+$/, "");
  if (!normalized) return "等待当前会话创建工作区";
  if (isManagedWorkspaceRoot(normalized)) return "当前会话工作区";
  const homeMatch = normalized.match(/^\/Users\/[^/]+(\/.*)?$/);
  const homePath = homeMatch ? `~${homeMatch[1] || ""}` : normalized;
  const parts = homePath.split("/").filter(Boolean);
  if (parts.length <= 3) return homePath;
  return `…/${parts.slice(-2).join("/")}`;
}

function isTextualWork(work: WorkItem): boolean {
  return (
    work.kind === "markdown" ||
    work.kind === "html" ||
    Boolean(work.mime_type?.startsWith("text/")) ||
    TEXT_WORK_EXTENSIONS.test(work.path)
  );
}

export function isolatedHtmlPreviewSource(source: string): string {
  const policy =
    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:\">";
  return /<head(?:\s[^>]*)?>/i.test(source)
    ? source.replace(/<head(?:\s[^>]*)?>/i, (head) => `${head}${policy}`)
    : `${policy}${source}`;
}

function outcomeStatusLabel(value: string, kind: "execution" | "delivery") {
  if (kind === "execution") {
    return (
      {
        idle: "任务尚未运行",
        running: "任务执行中",
        stopping: "任务正在停止",
        completed: "任务已结束",
        failed: "任务执行失败",
        interrupted: "任务已中断",
        stalled: "任务执行停滞",
        cancelled: "任务已停止",
        stopped: "任务已停止",
        unknown: "任务状态未知",
      }[value] || "任务状态未知"
    );
  }
  return (
    {
      verified: "已正式交付",
      partial: "部分交付",
      not_delivered: "尚未交付",
      candidate: "候选待验真",
      delivered: "已正式交付",
      ready: "可供审阅，未正式发送",
      pending: "尚未交付",
      blocked: "交付受阻",
      unknown: "交付状态未知",
    }[value] || "交付状态未知"
  );
}

function validationStatusLabel(validation: WorkItem["validation"]): string {
  if (!validation) return "未提供独立检查记录";
  return (
    {
      passed: "必需检查范围通过",
      failed: "所列检查有问题",
      partial: "仅完成部分检查",
      not_run: "尚未执行检查",
      error: "检查执行出错",
    }[validation.status] || "检查状态未知"
  );
}

function validationCheckOutcomeLabel(
  outcome: NonNullable<WorkItem["validation"]>["checks"][number]["outcome"],
): string {
  return {
    passed: "通过",
    failed: "有问题",
    not_run: "未检查",
    error: "执行错误",
    waived: "已豁免",
  }[outcome];
}

function workVersionSource(work: WorkItem): string {
  if (work.derivation) {
    return `派生转换·来自 V${work.derivation.source_version}`;
  }
  if (work.source_run_id) return "任务运行";
  if (work.parent_work_id) return "手动保存";
  return "手动登记";
}

function workRelationshipLabel(work: WorkItem): string {
  if (work.derivation) {
    return `派生 PDF（来自 V${work.derivation.source_version}）`;
  }
  if (work.artifact_role === "delivery") return "正式交付";
  if (work.artifact_role === "primary") return "主成果";
  if (work.artifact_role === "candidate") {
    return "候选成果（角色尚未确认为主交付）";
  }
  return "未分类";
}

function availabilityLabel(work: WorkItem): string {
  if (work.availability_status === "available") {
    return work.integrity_status === "intact" ? "字节可用且完整" : "字节可用";
  }
  if (work.availability_status === "missing") return "当前字节缺失";
  if (work.availability_status === "permission_denied") return "无权读取";
  return "可用性未知";
}

function collectExpandedDirectories(nodes: CodeFileNode[]): Set<string> {
  const expanded = new Set<string>();
  const visit = (items: CodeFileNode[], depth: number) => {
    for (const item of items) {
      if (item.kind !== "directory" || item.children.length === 0) continue;
      if (depth < 2) expanded.add(item.path);
      visit(item.children, depth + 1);
    }
  };
  visit(nodes, 0);
  return expanded;
}

function attachmentRoleForNode(
  node: CodeFileNode,
  attachmentRoles: Map<string, CanonicalAttachmentProjection>,
): CanonicalAttachmentProjection | undefined {
  if (node.kind !== "file") return undefined;
  const normalizedPath = node.path.replace(/^\.\//, "");
  if (normalizedPath !== `attachments/${node.name}`) return undefined;
  return attachmentRoles.get(node.name);
}

function FileTreeRow({
  node,
  depth,
  activePath,
  expanded,
  onToggle,
  onOpen,
  onActivate,
  actionItems,
  attachmentRoles,
}: FileTreeRowProps) {
  const directory = node.kind === "directory";
  const isExpanded = expanded.has(node.path);
  const attachmentProjection = attachmentRoleForNode(node, attachmentRoles);
  const row = (
    <button
      type="button"
      className={`${styles.treeRow} ${
        activePath === node.path ? styles.treeRowActive : ""
      }`}
      style={{ paddingLeft: 10 + depth * 14 }}
      title={`${node.path} · 右键查看更多操作`}
      onClick={() => (directory ? onToggle(node.path) : onOpen(node.path))}
      onDoubleClick={() => {
        if (!directory) onActivate(node.path);
      }}
    >
      <span className={styles.treeChevron} aria-hidden="true">
        {directory ? (
          isExpanded ? (
            <ChevronDown size={13} />
          ) : (
            <ChevronRight size={13} />
          )
        ) : null}
      </span>
      <span className={styles.treeIcon} aria-hidden="true">
        {directory ? <Folder size={14} /> : <File size={14} />}
      </span>
      <span className={styles.treeName}>
        {attachmentProjection?.displayName || node.name}
      </span>
      {attachmentProjection ? (
        <span className={styles.treeRole}>
          {getAttachmentRoleLabel(attachmentProjection.role)}
        </span>
      ) : null}
    </button>
  );
  return (
    <li>
      <Dropdown
        menu={{ items: actionItems(node) }}
        trigger={["contextMenu"]}
        destroyOnHidden
      >
        {row}
      </Dropdown>
      {directory && isExpanded && node.children.length > 0 ? (
        <ul className={styles.treeList}>
          {node.children.map((child) => (
            <FileTreeRow
              key={child.path}
              node={child}
              depth={depth + 1}
              activePath={activePath}
              expanded={expanded}
              onToggle={onToggle}
              onOpen={onOpen}
              onActivate={onActivate}
              actionItems={actionItems}
              attachmentRoles={attachmentRoles}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function filename(path: string): string {
  return path.split("/").filter(Boolean).pop() || path;
}

export default function ChatArtifactPanel({
  open,
  chatId,
  requestedPath,
  onClose,
  onPrepareArtifactEdit,
}: ChatArtifactPanelProps) {
  const { message } = useAppMessage();
  const initialWorkbenchRef = useRef<ArtifactWorkbenchState | null>(null);
  if (initialWorkbenchRef.current === null) {
    initialWorkbenchRef.current = loadArtifactWorkbenchState(chatId);
  }
  const initialWorkbench = initialWorkbenchRef.current;
  const [workbench, setWorkbenchState] =
    useState<ArtifactWorkbenchState>(initialWorkbench);
  const workbenchRef = useRef(workbench);
  const workbenchChatIdRef = useRef(chatId);
  const [view, setViewState] = useState<ChatArtifactView>(
    initialWorkbench.view,
  );
  const [surface, setSurface] = useState<ArtifactPanelSurface>(
    requestedPath ? "files" : "outcome",
  );
  const [works, setWorks] = useState<WorkItem[]>([]);
  const [runtimeState, setRuntimeState] = useState<ChatRuntimeState | null>(
    null,
  );
  const [attachmentRoles, setAttachmentRoles] = useState<
    Map<string, CanonicalAttachmentProjection>
  >(() => new Map());
  const [outcomeLoading, setOutcomeLoading] = useState(false);
  const [outcomeError, setOutcomeError] = useState<string | null>(null);
  const [outcomePreview, setOutcomePreview] = useState<WorkPreview | null>(
    null,
  );
  const [outcomePreviewStatus, setOutcomePreviewStatus] =
    useState<OutcomePreviewStatus>("idle");
  const [outcomePreviewError, setOutcomePreviewError] = useState<string | null>(
    null,
  );
  const [selectedPage, setSelectedPage] = useState(1);
  const [previewImageUrl, setPreviewImageUrl] = useState<string | null>(null);
  const [outcomeText, setOutcomeText] = useState<string | null>(null);
  const [outcomeTextPreview, setOutcomeTextPreview] =
    useState<WorkTextPreview | null>(null);
  const [outcomeAssetUrl, setOutcomeAssetUrl] = useState<string | null>(null);
  const [preparingEdit, setPreparingEdit] = useState(false);
  const [pendingManualEdit, setPendingManualEdit] =
    useState<WorkEditCopy | null>(null);
  const [finalizingManualEdit, setFinalizingManualEdit] = useState(false);
  const [derivingPdf, setDerivingPdf] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [tree, setTree] = useState<CodeFileTreeResponse | null>(null);
  const [workspaceDir, setWorkspaceDir] = useState("");
  const [, setWorkspaceName] = useState("当前任务");
  const [activePath, setActivePath] = useState<string | null>(
    initialWorkbench.activePath,
  );
  const [activeFile, setActiveFile] = useState<CodeFileContentResponse | null>(
    null,
  );
  const [expanded, setExpandedState] = useState<Set<string>>(
    new Set(initialWorkbench.expanded),
  );
  const [loading, setLoading] = useState(false);
  const [fileLoading, setFileLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [treeError, setTreeError] = useState<string | null>(null);
  const [panelWidth, setPanelWidthState] = useState<number | null>(
    storedPanelWidth,
  );
  const [renderedPanelWidth, setRenderedPanelWidth] = useState<number | null>(
    null,
  );
  const [hostWidth, setHostWidth] = useState(0);
  const [resizing, setResizing] = useState(false);
  const requestIdRef = useRef(0);
  const treeGenerationRef = useRef(0);
  const outcomeGenerationRef = useRef(0);
  const previewGenerationRef = useRef(0);
  const pageGenerationRef = useRef(0);
  const editActionGenerationRef = useRef(0);
  const activePathRef = useRef<string | null>(initialWorkbench.activePath);
  const panelRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const panelWidthRef = useRef<number | null>(panelWidth);
  const resizeDragRef = useRef<{
    pointerId: number;
    panelRight: number;
  } | null>(null);
  const documentResizeStyleRef = useRef<{
    cursor: string;
    userSelect: string;
  } | null>(null);
  const effectiveHostWidth =
    hostWidth || (typeof window !== "undefined" ? window.innerWidth : 1200);
  const shellLayout =
    effectiveHostWidth <= 680
      ? "full"
      : effectiveHostWidth <= 1100
        ? "overlay"
        : "split";

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const updateWorkbench = useCallback(
    (
      update:
        | ArtifactWorkbenchState
        | ((current: ArtifactWorkbenchState) => ArtifactWorkbenchState),
    ) => {
      // Async tree/outcome refreshes can finish in either order. Advance the
      // shared ref synchronously so neither path can reconcile from an older
      // snapshot and erase the other's selected Work or open-file state.
      const current = workbenchRef.current;
      const next = typeof update === "function" ? update(current) : update;
      workbenchRef.current = next;
      setWorkbenchState(next);
    },
    [],
  );

  const changeView = useCallback(
    (nextView: ArtifactWorkbenchView) => {
      setViewState(nextView);
      updateWorkbench((current) => ({ ...current, view: nextView }));
    },
    [updateWorkbench],
  );

  const clampPanelWidth = useCallback((requestedWidth: number) => {
    const parentWidth =
      panelRef.current?.parentElement?.getBoundingClientRect().width ||
      window.innerWidth;
    const availableWidth =
      parentWidth <= 1100
        ? parentWidth - 32
        : parentWidth - MIN_CHAT_STAGE_WIDTH;
    const maximum = Math.max(
      MIN_PANEL_WIDTH,
      Math.min(MAX_PANEL_WIDTH, availableWidth),
    );
    return Math.round(
      Math.min(maximum, Math.max(MIN_PANEL_WIDTH, requestedWidth)),
    );
  }, []);

  const updatePanelWidth = useCallback(
    (requestedWidth: number) => {
      const nextWidth = clampPanelWidth(requestedWidth);
      panelWidthRef.current = nextWidth;
      setPanelWidthState(nextWidth);
      return nextWidth;
    },
    [clampPanelWidth],
  );

  const restoreDocumentResizeStyle = useCallback(() => {
    const previous = documentResizeStyleRef.current;
    if (!previous) return;
    document.body.style.cursor = previous.cursor;
    document.body.style.userSelect = previous.userSelect;
    documentResizeStyleRef.current = null;
  }, []);

  const finishResize = useCallback(
    (pointerId?: number) => {
      const activeDrag = resizeDragRef.current;
      if (
        !activeDrag ||
        (pointerId !== undefined && activeDrag.pointerId !== pointerId)
      ) {
        return;
      }
      resizeDragRef.current = null;
      setResizing(false);
      restoreDocumentResizeStyle();
      const width = panelWidthRef.current;
      if (width === null) return;
      try {
        window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(width));
      } catch {
        // Resizing still works when persistent storage is unavailable.
      }
    },
    [restoreDocumentResizeStyle],
  );

  useEffect(
    () => () => {
      restoreDocumentResizeStyle();
    },
    [restoreDocumentResizeStyle],
  );

  const handleResizePointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0 || !panelRef.current) return;
      event.preventDefault();
      const panelRect = panelRef.current.getBoundingClientRect();
      resizeDragRef.current = {
        pointerId: event.pointerId,
        panelRight: panelRect.right,
      };
      panelWidthRef.current = panelRect.width;
      setResizing(true);
      event.currentTarget.setPointerCapture?.(event.pointerId);
      if (!documentResizeStyleRef.current) {
        documentResizeStyleRef.current = {
          cursor: document.body.style.cursor,
          userSelect: document.body.style.userSelect,
        };
      }
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [],
  );

  const handleResizePointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const activeDrag = resizeDragRef.current;
      if (!activeDrag || activeDrag.pointerId !== event.pointerId) return;
      event.preventDefault();
      updatePanelWidth(activeDrag.panelRight - event.clientX);
    },
    [updatePanelWidth],
  );

  const handleResizeKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const currentWidth =
        panelWidthRef.current ||
        panelRef.current?.getBoundingClientRect().width ||
        MIN_PANEL_WIDTH;
      const direction = event.key === "ArrowLeft" ? 1 : -1;
      const width = updatePanelWidth(currentWidth + direction * 24);
      try {
        window.localStorage.setItem(PANEL_WIDTH_STORAGE_KEY, String(width));
      } catch {
        // Keyboard resizing remains available without persistent storage.
      }
    },
    [updatePanelWidth],
  );

  const resetPanelWidth = useCallback(() => {
    resizeDragRef.current = null;
    setResizing(false);
    restoreDocumentResizeStyle();
    panelWidthRef.current = null;
    setPanelWidthState(null);
    try {
      window.localStorage.removeItem(PANEL_WIDTH_STORAGE_KEY);
    } catch {
      // The CSS default still restores even when storage is unavailable.
    }
  }, [restoreDocumentResizeStyle]);

  useEffect(() => {
    if (workbenchChatIdRef.current === chatId) return;
    workbenchChatIdRef.current = chatId;
    requestIdRef.current += 1;
    treeGenerationRef.current += 1;
    outcomeGenerationRef.current += 1;
    previewGenerationRef.current += 1;
    pageGenerationRef.current += 1;
    editActionGenerationRef.current += 1;
    const restored = loadArtifactWorkbenchState(chatId);
    workbenchRef.current = restored;
    setWorkbenchState(restored);
    setViewState(restored.view);
    activePathRef.current = restored.activePath;
    setActivePath(restored.activePath);
    setActiveFile(null);
    setExpandedState(new Set(restored.expanded));
    setError(null);
    setTreeError(null);
    setSurface("outcome");
    setWorks([]);
    setRuntimeState(null);
    setAttachmentRoles(new Map());
    setOutcomeError(null);
    setOutcomePreview(null);
    setOutcomePreviewStatus("idle");
    setOutcomePreviewError(null);
    setSelectedPage(1);
    setOutcomeText(null);
    setOutcomeTextPreview(null);
    setOutcomeAssetUrl(null);
    setPendingManualEdit(null);
    setPreparingEdit(false);
    setFinalizingManualEdit(false);
    setTree(null);
    setWorkspaceDir("");
  }, [chatId]);

  useEffect(() => {
    if (requestedPath) setSurface("files");
  }, [requestedPath]);

  useEffect(() => {
    if (workbenchChatIdRef.current !== chatId) return;
    saveArtifactWorkbenchState(chatId, workbench);
  }, [chatId, workbench]);

  useEffect(() => {
    activePathRef.current = activePath;
  }, [activePath]);

  useEffect(() => {
    if (!open || !panelRef.current) return;
    const panel = panelRef.current;
    const host = panel.parentElement;
    const updateDimensions = (entries: ResizeObserverEntry[] = []) => {
      const panelEntry = entries.find((entry) => entry.target === panel);
      const hostEntry = entries.find((entry) => entry.target === host);
      setRenderedPanelWidth(
        Math.round(
          panelEntry?.contentRect.width || panel.getBoundingClientRect().width,
        ),
      );
      setHostWidth(
        Math.round(
          hostEntry?.contentRect.width ||
            host?.getBoundingClientRect().width ||
            window.innerWidth,
        ),
      );
    };
    updateDimensions();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => updateDimensions(entries));
    observer.observe(panel);
    if (host) observer.observe(host);
    return () => observer.disconnect();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const active = globalThis.document.activeElement;
    if (active instanceof HTMLElement && !panelRef.current?.contains(active)) {
      previousFocusRef.current = active;
    }
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onCloseRef.current();
    };
    globalThis.document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      globalThis.document.removeEventListener("keydown", handleKeyDown, true);
      previousFocusRef.current?.focus();
      previousFocusRef.current = null;
    };
  }, [open]);

  useEffect(() => {
    if (!open || shellLayout === "split") return;
    const panel = panelRef.current;
    if (!panel) return;
    const focusCloseButton = () => closeButtonRef.current?.focus();
    const frame = window.requestAnimationFrame(focusCloseButton);
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not([disabled]),a[href],input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.getClientRects().length > 0);
      if (!focusable.length) {
        event.preventDefault();
        focusCloseButton();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const current = globalThis.document.activeElement;
      if (event.shiftKey && current === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && current === last) {
        event.preventDefault();
        first.focus();
      }
    };
    globalThis.document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      window.cancelAnimationFrame(frame);
      globalThis.document.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [open, shellLayout]);

  const panelMode = artifactPanelModeForWidth(
    renderedPanelWidth || panelWidth || DEFAULT_PANEL_WIDTH,
  );

  const outcomeSelection = useMemo(
    () =>
      selectPrimaryOutcome({
        works,
        chatId: chatId || "",
        currentRunId: runtimeState?.run_id,
        pinnedWorkId: workbench.selectedWorkId,
      }),
    [chatId, runtimeState?.run_id, workbench.selectedWorkId, works],
  );
  const primaryWork = outcomeSelection.primary;
  const lineageVersions = useMemo(() => {
    if (!primaryWork) return [];
    const lineage = primaryWork.lineage_root_id || primaryWork.id;
    return works
      .filter(
        (work) =>
          work.source_chat_id === chatId &&
          (work.lineage_root_id || work.id) === lineage,
      )
      .sort((left, right) => right.version - left.version);
  }, [chatId, primaryWork, works]);
  const latestLineageVersion = lineageVersions[0] || primaryWork;
  const viewingHistoricalRevision = Boolean(
    primaryWork &&
    latestLineageVersion &&
    primaryWork.id !== latestLineageVersion.id,
  );
  useEffect(() => {
    if (!primaryWork || workbench.selectedWorkId) return;
    updateWorkbench((current) =>
      current.selectedWorkId
        ? current
        : { ...current, selectedWorkId: primaryWork.id },
    );
  }, [primaryWork, updateWorkbench, workbench.selectedWorkId]);
  const outcomeProjection = primaryWork
    ? projectArtifactOutcome({
        work: primaryWork,
        runtime: runtimeState,
        previewStatus: outcomePreviewStatus,
      })
    : null;

  const refreshOutcomes = useCallback(async () => {
    if (!open || !chatId) {
      setWorks([]);
      setRuntimeState(null);
      setAttachmentRoles(new Map());
      setOutcomeLoading(false);
      return;
    }
    const generation = ++outcomeGenerationRef.current;
    setOutcomeLoading(true);
    setOutcomeError(null);
    try {
      const [nextWorks, chat] = await Promise.all([
        worksApi.list(chatId),
        chatApi.getChat(chatId),
      ]);
      if (generation !== outcomeGenerationRef.current) return;
      const cachedWorkId = workbenchRef.current.selectedWorkId;
      if (
        cachedWorkId &&
        !nextWorks.some(
          (work) => work.id === cachedWorkId && work.source_chat_id === chatId,
        )
      ) {
        updateWorkbench((current) => {
          if (current.selectedWorkId !== cachedWorkId) return current;
          const selectedPageByWorkId = { ...current.selectedPageByWorkId };
          delete selectedPageByWorkId[cachedWorkId];
          return {
            ...current,
            selectedWorkId: null,
            selectedPageByWorkId,
          };
        });
      }
      setWorks(nextWorks);
      setRuntimeState(chat.runtime_state || null);
      const trustedAttachmentPaths = Array.isArray(
        chat.meta?.external_attachment_paths,
      )
        ? chat.meta.external_attachment_paths
        : [];
      setAttachmentRoles(
        attachmentRolesFromMessages(
          chat.messages || [],
          trustedAttachmentPaths,
        ),
      );
    } catch (reason) {
      if (generation !== outcomeGenerationRef.current) return;
      setOutcomeError(
        reason instanceof Error ? reason.message : "成果状态读取失败",
      );
    } finally {
      if (generation === outcomeGenerationRef.current) {
        setOutcomeLoading(false);
      }
    }
  }, [chatId, open, updateWorkbench]);

  useEffect(() => {
    if (!open) return;
    void refreshOutcomes();
  }, [open, refreshOutcomes]);

  useEffect(() => {
    const generation = ++previewGenerationRef.current;
    let objectUrl: string | null = null;
    setOutcomePreview(null);
    setOutcomePreviewError(null);
    setOutcomeText(null);
    setOutcomeAssetUrl(null);
    setSelectedPage(
      primaryWork
        ? workbenchRef.current.selectedPageByWorkId[primaryWork.id] || 1
        : 1,
    );
    if (!open || !primaryWork) {
      setOutcomePreviewStatus("idle");
      return;
    }
    if (
      primaryWork.availability_status !== "available" ||
      primaryWork.integrity_status !== "intact"
    ) {
      setOutcomePreviewStatus("error");
      setOutcomePreviewError("当前版本不可用或完整性未确认");
      return;
    }
    setOutcomePreviewStatus("loading");
    const loadPreview = async () => {
      try {
        if (
          (["presentation", "pdf"] as WorkItem["kind"][]).includes(
            primaryWork.kind,
          )
        ) {
          const preview = await worksApi.preview(primaryWork.id, chatId);
          if (generation !== previewGenerationRef.current) return;
          setOutcomePreview(preview);
          setOutcomePreviewStatus("ready");
          return;
        }
        if (
          primaryWork.kind === "document" ||
          primaryWork.kind === "spreadsheet"
        ) {
          const preview = await worksApi.textPreview(primaryWork.id, chatId);
          if (generation !== previewGenerationRef.current) return;
          if (!preview.content) {
            setOutcomePreviewStatus("unsupported");
            setOutcomePreviewError(preview.message || "未能从当前版本提取正文");
            return;
          }
          setOutcomeText(preview.content);
          setOutcomeTextPreview(preview);
          setOutcomePreviewStatus("ready");
          return;
        }
        if (primaryWork.kind === "image" || isTextualWork(primaryWork)) {
          const blob = await worksApi.contentBlob(primaryWork.id, chatId);
          if (generation !== previewGenerationRef.current) return;
          if (primaryWork.kind === "image") {
            objectUrl = URL.createObjectURL(blob);
            setOutcomeAssetUrl(objectUrl);
          } else {
            if (blob.size > MAX_INLINE_TEXT_BYTES) {
              setOutcomePreviewStatus("unsupported");
              setOutcomePreviewError("文本超过 2 MB，请下载或用编辑器打开");
              return;
            }
            setOutcomeText(await blob.text());
          }
          if (generation === previewGenerationRef.current) {
            setOutcomePreviewStatus("ready");
          }
          return;
        }
        setOutcomePreviewStatus("unsupported");
      } catch (reason) {
        if (generation !== previewGenerationRef.current) return;
        setOutcomePreviewStatus("error");
        setOutcomePreviewError(
          reason instanceof Error ? reason.message : "无法生成内容预览",
        );
      }
    };
    void loadPreview();
    return () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [chatId, open, primaryWork]);

  useEffect(() => {
    if (!primaryWork || !outcomePreview?.pages.length) return;
    const lastPage =
      outcomePreview.pages[outcomePreview.pages.length - 1].number;
    if (outcomePreview.pages.some((page) => page.number === selectedPage)) {
      return;
    }
    const nextPage = Math.min(Math.max(selectedPage, 1), lastPage);
    setSelectedPage(nextPage);
    updateWorkbench((current) => ({
      ...current,
      selectedPageByWorkId: {
        ...current.selectedPageByWorkId,
        [primaryWork.id]: nextPage,
      },
    }));
  }, [outcomePreview, primaryWork, selectedPage, updateWorkbench]);

  const selectOutcomePage = useCallback(
    (page: number) => {
      setSelectedPage(page);
      if (!primaryWork) return;
      updateWorkbench((current) => ({
        ...current,
        selectedPageByWorkId: {
          ...current.selectedPageByWorkId,
          [primaryWork.id]: page,
        },
      }));
    },
    [primaryWork, updateWorkbench],
  );

  useEffect(() => {
    let objectUrl: string | null = null;
    const previewGeneration = previewGenerationRef.current;
    const pageGeneration = ++pageGenerationRef.current;
    setPreviewImageUrl(null);
    if (!primaryWork || !outcomePreview || outcomePreviewStatus !== "ready") {
      return;
    }
    void worksApi
      .previewPageBlob(
        primaryWork.id,
        selectedPage,
        outcomePreview.source_digest,
        chatId,
      )
      .then((blob) => {
        if (
          previewGeneration !== previewGenerationRef.current ||
          pageGeneration !== pageGenerationRef.current
        ) {
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        setPreviewImageUrl(objectUrl);
      })
      .catch((reason) => {
        if (
          previewGeneration !== previewGenerationRef.current ||
          pageGeneration !== pageGenerationRef.current
        ) {
          return;
        }
        setOutcomePreviewStatus("error");
        setOutcomePreviewError(
          reason instanceof Error ? reason.message : "无法读取该页预览",
        );
      });
    return () => {
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [chatId, outcomePreview, outcomePreviewStatus, primaryWork, selectedPage]);

  const runNativeAction = useCallback(
    async (action: "open" | "openWith" | "reveal", targetPath: string) => {
      if (!targetPath) {
        message.error("文件路径无效，无法执行此操作");
        return;
      }
      const api = window.electronAPI;
      const handler =
        action === "open"
          ? api?.openPath
          : action === "openWith"
            ? api?.openWith
            : api?.revealPath;
      if (!api?.isDesktop || !handler) {
        message.info("请在澜台桌面版中使用系统打开功能");
        return;
      }
      try {
        const result = (await handler(targetPath)) as NativeOpenResult;
        if (result?.success || result?.canceled) return;
        if (result?.revealed) {
          message.info("已在 Finder 中显示，请右键选择“打开方式”");
          return;
        }
        message.error(result?.error || "系统无法打开此项目");
      } catch (reason) {
        message.error(
          reason instanceof Error ? reason.message : "系统打开失败",
        );
      }
    },
    [message],
  );

  const saveOriginalFile = useCallback(
    async (relativePath: string) => {
      if (!relativePath || !workspaceDir) {
        message.error("文件路径无效，无法下载");
        return;
      }
      let url: string | null = null;
      try {
        const blob = await codingApi.downloadFile(relativePath, {
          workspaceDir,
        });
        url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename(relativePath);
        anchor.click();
      } catch (reason) {
        message.error(
          reason instanceof Error ? reason.message : "原文件下载失败",
        );
      } finally {
        if (url) URL.revokeObjectURL(url);
      }
    },
    [message, workspaceDir],
  );

  const runHtmlWork = useCallback(
    async (targetPath: string) => {
      const api = window.electronAPI;
      if (!api?.isDesktop || !api.openHtmlWork) {
        message.info("请在澜台桌面版中运行网页作品");
        return;
      }
      const result = await api.openHtmlWork(targetPath, filename(targetPath));
      if (!result.success) {
        message.error(result.error || "无法运行网页作品");
      }
    },
    [message],
  );

  const openFile = useCallback(
    async (
      path: string,
      rootOverride?: string,
      openBinaryWithSystem = false,
    ): Promise<CodeFileContentResponse | null> => {
      const root = rootOverride || workspaceDir;
      if (!root || !path) return null;
      const requestId = ++requestIdRef.current;
      setActivePath(path);
      setActiveFile(null);
      setError(null);
      if (
        prefersNativeFileOpen(path) ||
        (openBinaryWithSystem &&
          EDITABLE_OFFICE.test(path) &&
          !PREVIEWABLE_MARKDOWN.test(path))
      ) {
        setFileLoading(false);
        if (openBinaryWithSystem) {
          const systemPath = artifactSystemPath(root, path);
          if (EDITABLE_OFFICE.test(path)) {
            const openEditor =
              window.electronAPI?.openOfficeEditor ||
              window.electronAPI?.openDocxEditor;
            const result = await openEditor?.(systemPath, {
              source_chat_id: chatId || "",
              agent_id: useAgentStore.getState().selectedAgent,
            });
            if (result && !result.success) {
              message.error(result.error || "无法在澜台本地编辑器中打开");
            }
          } else {
            await runNativeAction("open", systemPath);
          }
        }
        return null;
      }
      setFileLoading(true);
      try {
        const file = await codingApi.readFile(path, { workspaceDir: root });
        if (requestId !== requestIdRef.current) return null;
        if (file.binary) {
          setActiveFile(null);
          if (openBinaryWithSystem) {
            await runNativeAction("open", artifactSystemPath(root, path));
          }
          return null;
        }
        setActiveFile(file);
        return file;
      } catch (reason) {
        if (requestId !== requestIdRef.current) return null;
        setActiveFile(null);
        setError(reason instanceof Error ? reason.message : "文件读取失败");
        return null;
      } finally {
        if (requestId === requestIdRef.current) setFileLoading(false);
      }
    },
    [chatId, message, runNativeAction, workspaceDir],
  );

  const rememberOpenFile = useCallback(
    (path: string, nextView: ArtifactWorkbenchView) => {
      activePathRef.current = path;
      setActivePath(path);
      setViewState(nextView);
      updateWorkbench((current) => openArtifactTab(current, path, nextView));
    },
    [updateWorkbench],
  );

  const openArtifactInPanel = useCallback(
    async (
      path: string,
      nextView: ArtifactWorkbenchView = "source",
      rootOverride?: string,
      openBinaryWithSystem = false,
    ) => {
      const openedFile = await openFile(
        path,
        rootOverride,
        openBinaryWithSystem,
      );
      if (!openedFile) return null;
      const resolvedView =
        nextView === "preview" &&
        !PREVIEWABLE_HTML.test(path) &&
        !PREVIEWABLE_MARKDOWN.test(path)
          ? "source"
          : nextView;
      rememberOpenFile(path, resolvedView);
      return openedFile;
    },
    [openFile, rememberOpenFile],
  );

  const refresh = useCallback(async () => {
    if (!open) return;
    const generation = ++treeGenerationRef.current;
    const sourceChatId = chatId;
    const isCurrentRefresh = () =>
      generation === treeGenerationRef.current &&
      workbenchChatIdRef.current === sourceChatId;
    const activeWorkspace = getActiveTopicWorkspace();
    const matchingWorkspace =
      activeWorkspace &&
      (!chatId || !activeWorkspace.chatId || activeWorkspace.chatId === chatId)
        ? activeWorkspace
        : null;
    const root = matchingWorkspace?.effectiveWorkspaceDir || "";

    if (!root) {
      if (!isCurrentRefresh()) return;
      setTree(null);
      setWorkspaceDir("");
      setWorkspaceName("当前任务");
      setActiveFile(null);
      setActivePath(null);
      setLoading(false);
      setTreeError(null);
      return;
    }

    setLoading(true);
    setTreeError(null);
    try {
      const state = await codingApi.getWorkbenchState({
        workspaceDir: root,
        chatId,
        treeDepth: DEFAULT_TREE_DEPTH,
        treeLimit: DEFAULT_TREE_LIMIT,
      });
      if (!isCurrentRefresh()) return;
      const rawTree = state.file_tree || null;
      const resolvedRoot = rawTree?.root_path || state.workspace_dir || root;
      const nextTree = rawTree
        ? {
            ...rawTree,
            children: visibleArtifactNodes(
              rawTree.children,
              resolvedRoot,
              requestedPath,
            ),
          }
        : null;
      if (nextTree) {
        nextTree.total_entries = flattenArtifactFiles(nextTree.children).length;
      }
      setTree(nextTree);
      setWorkspaceDir(state.workspace_dir || root);
      setWorkspaceName(
        isManagedWorkspaceRoot(resolvedRoot)
          ? "当前会话"
          : state.workspace_name || "当前任务",
      );
      if (!nextTree) {
        setActivePath(null);
        setActiveFile(null);
        return;
      }

      const files = flattenArtifactFiles(nextTree.children);
      const directories = flattenArtifactDirectories(nextTree.children);
      const currentWorkbench = workbenchRef.current;
      const stateWithInitialDirectories =
        currentWorkbench.openPaths.length === 0 &&
        currentWorkbench.expanded.length === 0
          ? {
              ...currentWorkbench,
              expanded: [...collectExpandedDirectories(nextTree.children)],
            }
          : currentWorkbench;
      const partialTree = Boolean(nextTree.truncated || nextTree.has_more);
      const reconciled = partialTree
        ? stateWithInitialDirectories
        : reconcileArtifactWorkbenchState(
            stateWithInitialDirectories,
            new Set(files.map((node) => node.path)),
            new Set(directories.map((node) => node.path)),
          );
      updateWorkbench(reconciled);
      setExpandedState(new Set(reconciled.expanded));

      const requested = normalizeArtifactPath(
        requestedPath,
        nextTree.root_path || state.workspace_dir || root,
      );
      const next =
        files.find((node) => node.path === requested) ||
        files.find((node) => node.path === reconciled.activePath);

      if (next) {
        const requestedView = requested
          ? PREVIEWABLE_HTML.test(next.path) ||
            PREVIEWABLE_MARKDOWN.test(next.path)
            ? "preview"
            : "source"
          : reconciled.view;
        const openedFile = await openArtifactInPanel(
          next.path,
          requestedView,
          state.workspace_dir || root,
          Boolean(requested),
        );
        if (!isCurrentRefresh()) return;
        if (!openedFile) {
          changeView("files");
        }
      } else {
        activePathRef.current = null;
        setActivePath(null);
        setActiveFile(null);
        changeView("files");
      }
    } catch (reason) {
      if (!isCurrentRefresh()) return;
      setTreeError(reason instanceof Error ? reason.message : "工作区读取失败");
    } finally {
      if (isCurrentRefresh()) setLoading(false);
    }
  }, [
    changeView,
    chatId,
    open,
    openArtifactInPanel,
    requestedPath,
    updateWorkbench,
  ]);

  useEffect(() => {
    if (!open) return;
    void refresh();
    const handleWorkspaceChange = () => {
      void refreshOutcomes();
      void refresh();
    };
    window.addEventListener(
      TOPIC_WORKSPACE_CHANGED_EVENT,
      handleWorkspaceChange,
    );
    return () => {
      window.removeEventListener(
        TOPIC_WORKSPACE_CHANGED_EVENT,
        handleWorkspaceChange,
      );
    };
  }, [open, refresh, refreshOutcomes]);

  useEffect(() => {
    if (!open || !workspaceDir) return;
    const controller = new AbortController();
    let refreshTimer: number | undefined;
    void codingApi
      .watchFiles({
        workspaceDir,
        signal: controller.signal,
        onChange: () => {
          window.clearTimeout(refreshTimer);
          refreshTimer = window.setTimeout(() => {
            void refreshOutcomes();
            void refresh();
          }, 220);
        },
      })
      .catch(() => {
        // The panel still supports explicit refresh when file watch is absent.
      });
    return () => {
      controller.abort();
      window.clearTimeout(refreshTimer);
    };
  }, [open, refresh, refreshOutcomes, workspaceDir]);

  const htmlPreviewAvailable = Boolean(
    activeFile && !activeFile.binary && PREVIEWABLE_HTML.test(activeFile.path),
  );
  const markdownPreviewAvailable = Boolean(
    activeFile &&
    !activeFile.binary &&
    PREVIEWABLE_MARKDOWN.test(activeFile.path),
  );
  const previewAvailable = htmlPreviewAvailable || markdownPreviewAvailable;
  const lineCount = useMemo(
    () => (activeFile?.content ? activeFile.content.split("\n").length : 0),
    [activeFile?.content],
  );
  const sourceLines = useMemo(
    () => activeFile?.content?.split("\n") ?? [],
    [activeFile?.content],
  );
  const artifactFileCount = useMemo(
    () => (tree ? flattenArtifactFiles(tree.children).length : 0),
    [tree],
  );
  const activeObjectLabel = activePath
    ? coCreationObjectLabelForFile(activePath)
    : "任务产物";
  const activeObjectPhase =
    loading || fileLoading
      ? CO_CREATION_PHASE_LABELS.observing
      : error || treeError
        ? "读取遇到问题"
        : activePath
          ? CO_CREATION_PHASE_LABELS.reviewing
          : "等待当前对象";
  const selectOutcome = useCallback(
    (work: WorkItem) => {
      updateWorkbench((current) => ({
        ...current,
        selectedWorkId: work.id,
      }));
      setPendingManualEdit(null);
    },
    [updateWorkbench],
  );

  const prepareOutcomeEdit = useCallback(async () => {
    if (!primaryWork || !chatId || !onPrepareArtifactEdit) return;
    const generation = ++editActionGenerationRef.current;
    const sourceChatId = chatId;
    const sourceWork = primaryWork;
    setPreparingEdit(true);
    try {
      const edit = await worksApi.createEditCopy(sourceWork.id, sourceChatId);
      if (
        generation !== editActionGenerationRef.current ||
        workbenchChatIdRef.current !== sourceChatId
      ) {
        return;
      }
      const target = buildArtifactEditTarget({
        chatId: sourceChatId,
        work: sourceWork,
        editPath: edit.edit_path,
        ...(sourceWork.kind === "presentation" || sourceWork.kind === "pdf"
          ? { page: selectedPage }
          : {}),
      });
      onPrepareArtifactEdit({ target, title: sourceWork.title });
    } catch (reason) {
      if (
        generation === editActionGenerationRef.current &&
        workbenchChatIdRef.current === sourceChatId
      ) {
        message.error(
          reason instanceof Error ? reason.message : "无法准备成果修改副本",
        );
      }
    } finally {
      if (
        generation === editActionGenerationRef.current &&
        workbenchChatIdRef.current === sourceChatId
      ) {
        setPreparingEdit(false);
      }
    }
  }, [chatId, message, onPrepareArtifactEdit, primaryWork, selectedPage]);

  const openOutcomeInEditor = useCallback(async () => {
    if (!primaryWork || !chatId) return;
    const openEditor =
      window.electronAPI?.openOfficeEditor ||
      window.electronAPI?.openDocxEditor;
    if (!window.electronAPI?.isDesktop || !openEditor) {
      message.info("请在澜台桌面版中使用本地编辑器");
      return;
    }
    const generation = ++editActionGenerationRef.current;
    const sourceChatId = chatId;
    const sourceWork = primaryWork;
    setPreparingEdit(true);
    try {
      const edit = await worksApi.createEditCopy(sourceWork.id, sourceChatId);
      if (
        generation !== editActionGenerationRef.current ||
        workbenchChatIdRef.current !== sourceChatId
      ) {
        return;
      }
      const result = await openEditor(edit.edit_path, {
        source_chat_id: sourceChatId,
        agent_id: useAgentStore.getState().selectedAgent,
      });
      if (
        generation !== editActionGenerationRef.current ||
        workbenchChatIdRef.current !== sourceChatId
      ) {
        return;
      }
      if (result && !result.success) {
        message.error(result.error || "无法在澜台本地编辑器中打开");
        return;
      }
      setPendingManualEdit(edit);
    } catch (reason) {
      if (
        generation === editActionGenerationRef.current &&
        workbenchChatIdRef.current === sourceChatId
      ) {
        message.error(
          reason instanceof Error ? reason.message : "无法创建手动编辑副本",
        );
      }
    } finally {
      if (
        generation === editActionGenerationRef.current &&
        workbenchChatIdRef.current === sourceChatId
      ) {
        setPreparingEdit(false);
      }
    }
  }, [chatId, message, primaryWork]);

  const finalizeManualEdit = useCallback(async () => {
    if (!pendingManualEdit || !primaryWork || !chatId) return;
    const generation = ++editActionGenerationRef.current;
    const sourceChatId = chatId;
    const sourceWork = primaryWork;
    const edit = pendingManualEdit;
    setFinalizingManualEdit(true);
    try {
      const saved = await worksApi.add({
        path: edit.edit_path,
        title: sourceWork.title,
        source_chat_id: sourceChatId,
        source_run_id: null,
      });
      if (
        generation !== editActionGenerationRef.current ||
        workbenchChatIdRef.current !== sourceChatId
      ) {
        return;
      }
      updateWorkbench((current) => ({
        ...current,
        selectedWorkId: saved.id,
      }));
      setPendingManualEdit(null);
      await refreshOutcomes();
      if (
        generation !== editActionGenerationRef.current ||
        workbenchChatIdRef.current !== sourceChatId
      ) {
        return;
      }
      message.success(
        saved.id === sourceWork.id
          ? "内容没有变化，仍保留当前版本"
          : `已保存为 V${saved.version}`,
      );
    } catch (reason) {
      if (
        generation === editActionGenerationRef.current &&
        workbenchChatIdRef.current === sourceChatId
      ) {
        message.error(
          reason instanceof Error
            ? reason.message
            : "保存失败；若已有新版本，请基于最新版本重新编辑",
        );
      }
    } finally {
      if (
        generation === editActionGenerationRef.current &&
        workbenchChatIdRef.current === sourceChatId
      ) {
        setFinalizingManualEdit(false);
      }
    }
  }, [
    chatId,
    message,
    pendingManualEdit,
    primaryWork,
    refreshOutcomes,
    updateWorkbench,
  ]);

  const downloadOutcome = useCallback(async () => {
    if (!primaryWork) return;
    setDownloading(true);
    let url: string | null = null;
    try {
      const blob = await worksApi.contentBlob(primaryWork.id, chatId);
      url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename(primaryWork.path) || primaryWork.title;
      anchor.click();
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "下载失败");
    } finally {
      if (url) URL.revokeObjectURL(url);
      setDownloading(false);
    }
  }, [chatId, message, primaryWork]);

  const deriveOutcomePdf = useCallback(async () => {
    if (!primaryWork || primaryWork.kind !== "presentation" || !chatId) return;
    const generation = ++editActionGenerationRef.current;
    const sourceChatId = chatId;
    const sourceWork = primaryWork;
    setDerivingPdf(true);
    try {
      const derived = await worksApi.derive(sourceWork.id, {
        target_format: "pdf",
        chat_id: sourceChatId,
      });
      if (
        generation !== editActionGenerationRef.current ||
        workbenchChatIdRef.current !== sourceChatId
      ) {
        return;
      }
      await refreshOutcomes();
      if (
        generation !== editActionGenerationRef.current ||
        workbenchChatIdRef.current !== sourceChatId
      ) {
        return;
      }
      message.success(
        `${filename(derived.path) || derived.title} 已作为 V${sourceWork.version} 的派生 PDF 关联`,
      );
    } catch (reason) {
      if (
        generation === editActionGenerationRef.current &&
        workbenchChatIdRef.current === sourceChatId
      ) {
        message.error(
          reason instanceof Error ? reason.message : "无法导出关联 PDF",
        );
      }
    } finally {
      if (
        generation === editActionGenerationRef.current &&
        workbenchChatIdRef.current === sourceChatId
      ) {
        setDerivingPdf(false);
      }
    }
  }, [chatId, message, primaryWork, refreshOutcomes]);

  const actionItems = useCallback(
    (node: CodeFileNode): MenuProps["items"] => {
      const systemPath = artifactSystemPath(workspaceDir, node.path);
      if (node.kind === "directory") {
        return [
          {
            key: "open-directory",
            label: "用 Finder 打开文件夹",
            onClick: () => void runNativeAction("open", systemPath),
          },
          {
            key: "reveal-directory",
            label: "在 Finder 中显示",
            onClick: () => void runNativeAction("reveal", systemPath),
          },
        ];
      }
      const previewItem: MenuProps["items"] =
        PREVIEWABLE_HTML.test(node.path) || PREVIEWABLE_MARKDOWN.test(node.path)
          ? [
              {
                key: "preview",
                label: "在澜台中预览",
                onClick: () => void openArtifactInPanel(node.path, "preview"),
              },
              { type: "divider" },
            ]
          : [];
      const officeItem: MenuProps["items"] = EDITABLE_OFFICE.test(node.path)
        ? [
            {
              key: "open-lantai-office",
              label: "在澜台中编辑",
              onClick: () => void openFile(node.path, undefined, true),
            },
            { type: "divider" },
          ]
        : [];
      return [
        ...previewItem,
        ...officeItem,
        {
          key: "open-default",
          label: EDITABLE_OFFICE.test(node.path)
            ? "用系统默认应用打开"
            : "用默认应用打开（推荐）",
          onClick: () => void runNativeAction("open", systemPath),
        },
        {
          key: "open-with",
          label: "选择应用打开…",
          onClick: () => void runNativeAction("openWith", systemPath),
        },
        {
          key: "download-original",
          label: "下载原文件",
          onClick: () => void saveOriginalFile(node.path),
        },
        { type: "divider" },
        {
          key: "reveal-file",
          label: "在 Finder 中显示",
          onClick: () => void runNativeAction("reveal", systemPath),
        },
      ];
    },
    [
      openArtifactInPanel,
      openFile,
      runNativeAction,
      saveOriginalFile,
      workspaceDir,
    ],
  );
  const activeFileActions = useMemo<MenuProps["items"]>(
    () =>
      activePath
        ? actionItems({
            name: filename(activePath),
            path: activePath,
            kind: "file",
            size: activeFile?.size || 0,
            children: [],
          })
        : [],
    [actionItems, activeFile?.size, activePath],
  );

  const handleSelectFile = useCallback(
    async (path: string) => {
      const openedFile = await openArtifactInPanel(
        path,
        "source",
        undefined,
        !EDITABLE_OFFICE.test(path) ||
          (prefersNativeFileOpen(path) && !PREVIEWABLE_MARKDOWN.test(path)),
      );
      if (!openedFile) changeView("files");
    },
    [changeView, openArtifactInPanel],
  );

  const handleActivateOpenTab = useCallback(
    async (path: string) => {
      const current = activateArtifactTab(workbenchRef.current, path);
      updateWorkbench(current);
      const nextView =
        current.view === "preview" &&
        (PREVIEWABLE_HTML.test(path) || PREVIEWABLE_MARKDOWN.test(path))
          ? "preview"
          : "source";
      await openArtifactInPanel(path, nextView);
    },
    [openArtifactInPanel, updateWorkbench],
  );

  const handleCloseOpenTab = useCallback(
    async (path: string) => {
      const current = workbenchRef.current;
      const wasActive = current.activePath === path;
      const next = closeArtifactTab(current, path);
      updateWorkbench(next);
      if (!wasActive) return;
      if (!next.activePath) {
        requestIdRef.current += 1;
        activePathRef.current = null;
        setActivePath(null);
        setActiveFile(null);
        setViewState("files");
        return;
      }
      const nextView =
        next.view === "preview" &&
        (PREVIEWABLE_HTML.test(next.activePath) ||
          PREVIEWABLE_MARKDOWN.test(next.activePath))
          ? "preview"
          : "source";
      await openArtifactInPanel(next.activePath, nextView);
    },
    [openArtifactInPanel, updateWorkbench],
  );

  const toggleExpandedDirectory = useCallback(
    (path: string) => {
      const next = new Set(expanded);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      setExpandedState(next);
      updateWorkbench((state) => ({ ...state, expanded: [...next] }));
    },
    [expanded, updateWorkbench],
  );

  useEffect(() => {
    if (panelMode !== "compact" && view === "files" && activeFile) {
      changeView("source");
    }
  }, [activeFile, changeView, panelMode, view]);

  if (!open) return null;

  const panelStyle = panelWidth
    ? ({
        "--chat-artifact-panel-width": `${panelWidth}px`,
      } as CSSProperties)
    : undefined;

  // prettier-ignore
  return (
    <>
      {shellLayout !== "split" ? (
        <div
          className={styles.scrim}
          aria-hidden="true"
          onPointerDown={() => onCloseRef.current()}
        />
      ) : null}
    <aside
      ref={panelRef}
      className={styles.panel}
      style={panelStyle}
      aria-label="任务产物"
      data-co-creation-surface={CO_CREATION_SURFACES.artifact.id}
      data-testid="chat-artifact-panel"
      data-resizing={resizing ? "true" : "false"}
      data-layout={panelMode}
      data-shell-layout={shellLayout}
      role={shellLayout === "split" ? undefined : "dialog"}
      aria-modal={shellLayout === "split" ? undefined : "true"}
    >
      <div
        className={styles.resizer}
        role="separator"
        aria-label="调整产物面板宽度"
        aria-orientation="vertical"
        aria-valuemin={MIN_PANEL_WIDTH}
        aria-valuemax={MAX_PANEL_WIDTH}
        aria-valuenow={
          panelWidth ||
          Math.round(
            panelRef.current?.getBoundingClientRect().width ||
              DEFAULT_PANEL_WIDTH,
          )
        }
        tabIndex={0}
        title="拖动调整宽度，双击恢复默认"
        data-testid="chat-artifact-resizer"
        onPointerDown={handleResizePointerDown}
        onPointerMove={handleResizePointerMove}
        onPointerUp={(event) => {
          event.currentTarget.releasePointerCapture?.(event.pointerId);
          finishResize(event.pointerId);
        }}
        onPointerCancel={(event) => finishResize(event.pointerId)}
        onDoubleClick={resetPanelWidth}
        onKeyDown={handleResizeKeyDown}
      />
      <header className={styles.header}>
        <nav className={styles.tabs} aria-label="产物视图">
          <button
            type="button"
            className={surface === "outcome" ? styles.tabActive : ""}
            aria-current={surface === "outcome" ? "page" : undefined}
            onClick={() => setSurface("outcome")}
          >
            <MonitorPlay size={14} />
            成果
          </button>
          <button
            type="button"
            className={surface === "files" ? styles.tabActive : ""}
            aria-current={surface === "files" ? "page" : undefined}
            onClick={() => setSurface("files")}
          >
            <FileCode2 size={14} />
            其他文件
          </button>
        </nav>
        <div className={styles.headerMeta}>
          <Tooltip title="刷新成果与文件">
            <button
              type="button"
              className={styles.iconButton}
              aria-label="刷新成果与文件"
              onClick={() => {
                void refreshOutcomes();
                void refresh();
              }}
            >
              <RefreshCw size={15} />
            </button>
          </Tooltip>
          <Tooltip title="收起产物面板">
            <button
              ref={closeButtonRef}
              type="button"
              className={styles.iconButton}
              aria-label="收起产物面板"
              onClick={() => onCloseRef.current()}
            >
              <PanelRightClose size={16} />
            </button>
          </Tooltip>
        </div>
      </header>

      {surface === "outcome" ? (
        <main className={styles.outcomeSurface}>
          {outcomeLoading && works.length === 0 ? (
            <div className={styles.centerState}>
              <Spin size="small" />
              <span>正在核对当前任务的成果关系…</span>
            </div>
          ) : outcomeError && works.length === 0 ? (
            <div className={styles.centerState}>
              <FileCode2 size={30} strokeWidth={1.35} />
              <strong>暂时无法核对成果</strong>
              <span>{outcomeError}</span>
              <button type="button" onClick={() => void refreshOutcomes()}>
                重试
              </button>
            </div>
          ) : primaryWork && outcomeProjection ? (
            <>
              <section className={styles.outcomeHeading}>
                <div>
                  <span className={styles.outcomeEyebrow}>当前成果</span>
                  <h2>{primaryWork.title}</h2>
                  <p>
                    <span>V{primaryWork.version}</span>
                    <span>{primaryWork.kind}</span>
                    <span title={primaryWork.path}>
                      {filename(primaryWork.path)}
                    </span>
                  </p>
                </div>
                <button
                  type="button"
                  className={styles.outcomeDownloadButton}
                  disabled={downloading}
                  onClick={() => void downloadOutcome()}
                >
                  <Download size={14} />
                  {downloading
                    ? "准备中"
                    : outcomeProjection.delivery === "verified" ||
                        outcomeProjection.delivery === "delivered"
                      ? "下载已交付版本"
                      : "下载候选草稿"}
                </button>
              </section>

              <section
                className={styles.outcomeStatusStrip}
                aria-label="成果状态"
              >
                <span>
                  <small>内容预览</small>
                  <strong>
                    {outcomeProjection.preview === "ready"
                      ? "可用"
                      : outcomeProjection.preview === "loading"
                        ? "准备中"
                        : outcomeProjection.preview === "unsupported"
                          ? "此格式暂不支持"
                          : outcomeProjection.preview === "error"
                            ? "失败"
                            : "未准备"}
                  </strong>
                </span>
                <span>
                  <small>检查</small>
                  <strong>
                    {validationStatusLabel(outcomeProjection.validation)}
                  </strong>
                </span>
                <span>
                  <small>交付</small>
                  <strong>
                    {outcomeStatusLabel(outcomeProjection.delivery, "delivery")}
                  </strong>
                </span>
                <span>
                  <small>任务</small>
                  <strong>
                    {outcomeStatusLabel(
                      outcomeProjection.execution,
                      "execution",
                    )}
                  </strong>
                </span>
              </section>

              {outcomeProjection.validation ? (
                <section
                  className={styles.outcomeValidation}
                  aria-label="检查范围与结果"
                >
                  <header>
                    <div>
                      <strong>检查范围与结果</strong>
                      <span>
                        {outcomeProjection.validation.validator_id} v
                        {outcomeProjection.validation.validator_version}
                        {" · "}
                        {outcomeProjection.validation.ruleset_id} v
                        {outcomeProjection.validation.ruleset_version}
                      </span>
                    </div>
                    <small>
                      总体：
                      {validationStatusLabel(outcomeProjection.validation)}
                    </small>
                  </header>
                  <dl>
                    <div>
                      <dt>已覆盖</dt>
                      <dd>
                        {outcomeProjection.validation.observed_scope.length > 0
                          ? outcomeProjection.validation.observed_scope.join(
                              "、",
                            )
                          : "无"}
                      </dd>
                    </div>
                    <div>
                      <dt>待检查</dt>
                      <dd>
                        {outcomeProjection.validation.required_scope.filter(
                          (checkId) =>
                            !outcomeProjection.validation?.observed_scope.includes(
                              checkId,
                            ),
                        ).length > 0
                          ? outcomeProjection.validation.required_scope
                              .filter(
                                (checkId) =>
                                  !outcomeProjection.validation?.observed_scope.includes(
                                    checkId,
                                  ),
                              )
                              .join("、")
                          : "无"}
                      </dd>
                    </div>
                  </dl>
                  <ul>
                    {outcomeProjection.validation.checks.map((check) => (
                      <li key={check.id} data-outcome={check.outcome}>
                        <div>
                          <code>{check.id}</code>
                          <span>
                            <strong>
                              {validationCheckOutcomeLabel(check.outcome)}
                            </strong>
                            <small>{check.required ? "必需" : "可选"}</small>
                          </span>
                        </div>
                        <p>{check.detail}</p>
                        {check.evidence_refs.length > 0 ? (
                          <small>
                            证据：{check.evidence_refs.join("、")}
                          </small>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}

              <section
                className={styles.outcomePreview}
                aria-label="成果内容预览"
              >
                {outcomePreviewStatus === "loading" ? (
                  <div className={styles.centerState}>
                    <Spin size="small" />
                    <span>正在准备真实内容预览…</span>
                  </div>
                ) : outcomePreviewStatus === "error" ? (
                  <div className={styles.centerState}>
                    <strong>预览不可用</strong>
                    <span>{outcomePreviewError || "当前版本无法预览"}</span>
                  </div>
                ) : outcomePreviewStatus === "unsupported" ? (
                  <div className={styles.centerState}>
                    <File size={30} strokeWidth={1.35} />
                    <strong>此格式暂不支持内嵌预览</strong>
                    <span>
                      {outcomePreviewError ||
                        "成果仍可下载；支持的 Office 格式可用澜台编辑器打开。"}
                    </span>
                  </div>
                ) : primaryWork.kind === "presentation" ||
                  primaryWork.kind === "pdf" ? (
                  <div className={styles.pagedPreview}>
                    <div className={styles.pageStage}>
                      {previewImageUrl ? (
                        <img
                          src={previewImageUrl}
                          alt={`${primaryWork.title} 第 ${selectedPage} 页`}
                        />
                      ) : (
                        <Spin size="small" />
                      )}
                    </div>
                    {outcomePreview ? (
                      <nav className={styles.pageRail} aria-label="选择页面">
                        {outcomePreview.pages.map((page) => (
                          <button
                            key={page.number}
                            type="button"
                            aria-current={
                              selectedPage === page.number ? "page" : undefined
                            }
                            className={
                              selectedPage === page.number
                                ? styles.pageButtonActive
                                : ""
                            }
                            onClick={() => selectOutcomePage(page.number)}
                          >
                            第 {page.number} 页
                          </button>
                        ))}
                      </nav>
                    ) : null}
                  </div>
                ) : primaryWork.kind === "image" && outcomeAssetUrl ? (
                  <div className={styles.imagePreview}>
                    <img src={outcomeAssetUrl} alt={primaryWork.title} />
                  </div>
                ) : primaryWork.kind === "markdown" && outcomeText != null ? (
                  <article className={styles.outcomeMarkdown}>
                    <Markdown raw content={outcomeText} />
                  </article>
                ) : (primaryWork.kind === "document" ||
                    primaryWork.kind === "spreadsheet") &&
                  outcomeText != null ? (
                  <article
                    className={styles.outcomeDocument}
                    aria-label={`${primaryWork.title} 正文预览`}
                  >
                    <header>
                      <span>正文与结构预览</span>
                      <small>
                        {outcomeTextPreview?.method || "Office Open XML"}
                        {outcomeTextPreview?.truncated
                          ? " · 已截断至 2 MB"
                          : ""}
                      </small>
                    </header>
                    <pre>{outcomeText}</pre>
                  </article>
                ) : outcomeText != null ? (
                  <pre className={styles.outcomeCode}>{outcomeText}</pre>
                ) : null}
              </section>

              <section className={styles.outcomeActions} aria-label="成果操作">
                <button
                  type="button"
                  className={styles.outcomePrimaryAction}
                  disabled={
                    preparingEdit ||
                    viewingHistoricalRevision ||
                    !chatId ||
                    !onPrepareArtifactEdit
                  }
                  onClick={() => void prepareOutcomeEdit()}
                >
                  <PencilLine size={14} />
                  {preparingEdit ? "正在准备" : "让沈行知修改"}
                </button>
                {EDITABLE_OFFICE.test(primaryWork.path) ? (
                  <button
                    type="button"
                    disabled={preparingEdit || viewingHistoricalRevision}
                    onClick={() => void openOutcomeInEditor()}
                  >
                    用澜台编辑
                  </button>
                ) : null}
                {primaryWork.kind === "presentation" &&
                /\.pptx?$/i.test(primaryWork.path) &&
                !primaryWork.derivation ? (
                  <button
                    type="button"
                    disabled={derivingPdf || viewingHistoricalRevision}
                    onClick={() => void deriveOutcomePdf()}
                  >
                    {derivingPdf ? "正在导出 PDF" : "导出并关联 PDF"}
                  </button>
                ) : null}
                {pendingManualEdit ? (
                  <button
                    type="button"
                    disabled={finalizingManualEdit}
                    onClick={() => void finalizeManualEdit()}
                  >
                    {finalizingManualEdit
                      ? "正在保存"
                      : "编辑保存后，存为新版本"}
                  </button>
                ) : null}
              </section>

              {viewingHistoricalRevision ? (
                <div className={styles.revisionNotice} role="status">
                  <span>
                    你正在查看历史版本。预览与下载保持 V{primaryWork.version}{" "}
                    的精确字节；修改需先切到最新 V{latestLineageVersion.version}
                    ，避免覆盖后续改动。
                  </span>
                  <button
                    type="button"
                    onClick={() => selectOutcome(latestLineageVersion)}
                  >
                    切到最新版本修改
                  </button>
                </div>
              ) : null}

              <details className={styles.outcomeDetails}>
                <summary>版本与来源详情</summary>
                <dl>
                  <div>
                    <dt>版本</dt>
                    <dd>V{primaryWork.version}</dd>
                  </div>
                  <div>
                    <dt>内容摘要</dt>
                    <dd title={primaryWork.source_digest || "未提供"}>
                      {primaryWork.source_digest
                        ? primaryWork.source_digest.slice(0, 12)
                        : "未提供"}
                    </dd>
                  </div>
                  <div>
                    <dt>可用性</dt>
                    <dd>{availabilityLabel(primaryWork)}</dd>
                  </div>
                  <div>
                    <dt>来源</dt>
                    <dd title={primaryWork.source_run_id || undefined}>
                      {workVersionSource(primaryWork)}
                      {primaryWork.source_run_id
                        ? ` · ${primaryWork.source_run_id.slice(0, 12)}`
                        : ""}
                    </dd>
                  </div>
                  <div>
                    <dt>父版本内容</dt>
                    <dd title={primaryWork.parent_digest || undefined}>
                      {primaryWork.parent_digest
                        ? primaryWork.parent_digest.slice(0, 12)
                        : "初始版本"}
                    </dd>
                  </div>
                  <div>
                    <dt>变更摘要</dt>
                    <dd>未提供</dd>
                  </div>
                </dl>
                {lineageVersions.length > 1 ? (
                  <div className={styles.versionHistory}>
                    <h3>版本历史</h3>
                    <ul aria-label="成果版本历史">
                      {lineageVersions.map((work) => (
                        <li key={work.id}>
                          <button
                            type="button"
                            aria-current={
                              work.id === primaryWork.id ? "true" : undefined
                            }
                            onClick={() => selectOutcome(work)}
                          >
                            <span>V{work.version}</span>
                            <small>
                              {new Date(work.created_at).toLocaleString()} ·{" "}
                              {workVersionSource(work)} ·{" "}
                              {work.id === latestLineageVersion.id
                                ? "最新"
                                : "历史"}
                            </small>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </details>

              {outcomeSelection.alternatives.length > 0 ? (
                <section className={styles.outcomeAlternatives}>
                  <h3>相关成果与文件</h3>
                  {outcomeSelection.alternatives.map((work) => (
                    <button
                      type="button"
                      key={work.id}
                      onClick={() => selectOutcome(work)}
                    >
                      <span>{work.title}</span>
                      <small>
                        {workRelationshipLabel(work)} · V{work.version} ·{" "}
                        {work.kind}
                      </small>
                    </button>
                  ))}
                </section>
              ) : null}
            </>
          ) : outcomeSelection.reason === "ambiguous" ? (
            <section className={styles.outcomeChooser}>
              <span className={styles.outcomeEyebrow}>需要你的选择</span>
              <h2>本任务有多个候选成果</h2>
              <p>
                现有关系不足以替你决定主成果。选择后，后续新版本会留在同一成果线上。
              </p>
              <div>
                {outcomeSelection.alternatives.map((work) => (
                  <button
                    type="button"
                    key={work.id}
                    onClick={() => selectOutcome(work)}
                  >
                    <strong>{work.title}</strong>
                    <span>
                      {workRelationshipLabel(work)} · V{work.version} ·{" "}
                      {work.kind}
                    </span>
                  </button>
                ))}
              </div>
            </section>
          ) : outcomeSelection.alternatives.length > 0 ? (
            <section className={styles.outcomeChooser}>
              <span className={styles.outcomeEyebrow}>尚未确定</span>
              <h2>暂无可确认的主成果</h2>
              <p>
                下列文件已有成果关系，但还没有主交付证据。你可以明确选择要查看的成果；选择本身不会把它标成已验证或已交付。
              </p>
              <div>
                {outcomeSelection.alternatives.map((work) => (
                  <button
                    type="button"
                    key={work.id}
                    onClick={() => selectOutcome(work)}
                  >
                    <strong>{work.title}</strong>
                    <span>
                      {workRelationshipLabel(work)} · V{work.version} ·{" "}
                      {work.kind}
                    </span>
                  </button>
                ))}
              </div>
              <button type="button" onClick={() => setSurface("files")}>
                查看其他文件
              </button>
            </section>
          ) : (
            <div className={styles.centerState}>
              <File size={30} strokeWidth={1.35} />
              <strong>暂无可确认的主成果</strong>
              <span>
                工作区文件不等于已完成成果。生成脚本和参考资料仍可在“其他文件”中查看。
              </span>
              <button type="button" onClick={() => setSurface("files")}>
                查看其他文件
              </button>
            </div>
          )}
        </main>
      ) : (
        <>
          <nav className={styles.fileViewTabs} aria-label="文件查看方式">
            <button
              type="button"
              aria-current={view === "files" ? "page" : undefined}
              onClick={() => changeView("files")}
            >
              文件
            </button>
            <button
              type="button"
              aria-current={view === "source" ? "page" : undefined}
              disabled={!activeFile}
              onClick={() => changeView("source")}
            >
              内容
            </button>
            <button
              type="button"
              aria-current={view === "preview" ? "page" : undefined}
              disabled={!previewAvailable}
              onClick={() => changeView("preview")}
            >
              预览
            </button>
          </nav>

          <div
            className={styles.workspacePath}
            title={compactArtifactWorkspacePath(workspaceDir)}
          >
            <span>{compactArtifactWorkspacePath(workspaceDir)}</span>
            {workspaceDir ? (
              <Tooltip title="用 Finder 打开工作区">
                <button
                  type="button"
                  className={styles.compactActionButton}
                  aria-label="打开工作区文件夹"
                  onClick={() => void runNativeAction("open", workspaceDir)}
                >
                  <FolderOpen size={14} />
                </button>
              </Tooltip>
            ) : null}
          </div>

          {workbench.openPaths.length > 0 ? (
            <nav className={styles.openTabs} aria-label="已打开的任务产物">
              {workbench.openPaths.map((path) => {
                const active = path === workbench.activePath;
                const name = filename(path);
                return (
                  <span
                    key={path}
                    className={`${styles.openFileTab} ${
                      active ? styles.openFileTabActive : ""
                    }`}
                    data-active={active ? "true" : "false"}
                    title={path}
                  >
                    <button
                      type="button"
                      className={styles.openFileTabButton}
                      aria-label={`切换到 ${name}`}
                      aria-current={active ? "page" : undefined}
                      onClick={() => void handleActivateOpenTab(path)}
                    >
                      <File size={12} aria-hidden="true" />
                      <span>{name}</span>
                    </button>
                    <button
                      type="button"
                      className={styles.openFileTabClose}
                      aria-label={`关闭 ${name}`}
                      onClick={() => void handleCloseOpenTab(path)}
                    >
                      <X size={12} aria-hidden="true" />
                    </button>
                  </span>
                );
              })}
            </nav>
          ) : null}

          <section
            className={styles.objectContext}
            aria-label="当前共同创作对象"
          >
            <span className={styles.objectKind}>{activeObjectLabel}</span>
            <span
              className={styles.objectName}
              title={activePath || CO_CREATION_SURFACES.artifact.objectLabel}
            >
              {activePath
                ? filename(activePath)
                : CO_CREATION_SURFACES.artifact.objectLabel}
            </span>
            <span className={styles.objectPhase}>{activeObjectPhase}</span>
          </section>

          {loading && !tree ? (
            <div className={styles.centerState}>
              <Spin size="small" />
              <span>正在读取任务产物…</span>
            </div>
          ) : !workspaceDir ? (
            <div className={styles.centerState}>
              <Folder size={30} strokeWidth={1.35} />
              <strong>产物会在这里出现</strong>
              <span>
                继续在聊天中描述目标，Agent 创建文件后可随时回来查看。
              </span>
            </div>
          ) : treeError && !tree ? (
            <div className={styles.centerState}>
              <FileCode2 size={30} strokeWidth={1.35} />
              <strong>暂时无法读取产物</strong>
              <span>{treeError}</span>
              <button type="button" onClick={() => void refresh()}>
                重试
              </button>
            </div>
          ) : tree && artifactFileCount === 0 ? (
            <div className={styles.centerState}>
              <Folder size={30} strokeWidth={1.35} />
              <strong>此会话还没有可查看的产物</strong>
              <span>
                文件创建后会自动出现在这里，聊天过程不会被工作台打断。
              </span>
            </div>
          ) : (
            <div className={styles.workspaceLayout} data-view={view}>
              <main className={styles.contentStage}>
                {view === "preview" && previewAvailable ? (
                  <div className={styles.previewShell}>
                    <div className={styles.previewToolbar}>
                      <span>{filename(activeFile?.path || "")}</span>
                      <div className={styles.toolbarActions}>
                        <em>
                          {htmlPreviewAvailable ? "临时预览" : "内容预览"}
                        </em>
                        <Dropdown
                          menu={{ items: activeFileActions }}
                          trigger={["click"]}
                          placement="bottomRight"
                          destroyOnHidden
                        >
                          <button
                            type="button"
                            className={styles.compactActionButton}
                            aria-label="文件操作"
                            title="打开方式与 Finder 操作"
                          >
                            <MoreHorizontal size={15} />
                          </button>
                        </Dropdown>
                      </div>
                    </div>
                    {htmlPreviewAvailable ? (
                      <>
                        <div
                          className={styles.previewBoundaryNotice}
                          role="note"
                        >
                          <span>
                            此处只显示不可信 HTML，不运行脚本、表单或网络请求。
                          </span>
                          <button
                            type="button"
                            aria-label={`在独立窗口运行 ${filename(activeFile?.path || "")}`}
                            onClick={() =>
                              void runHtmlWork(
                                artifactSystemPath(
                                  workspaceDir,
                                  activeFile?.path || "",
                                ),
                              )
                            }
                          >
                            独立运行
                          </button>
                        </div>
                        <iframe
                          key={`${activeFile?.path}:${activeFile?.sha256}`}
                          className={styles.previewFrame}
                          title={`${filename(activeFile?.path || "")} 预览`}
                          sandbox=""
                          srcDoc={isolatedHtmlPreviewSource(
                            activeFile?.content || "",
                          )}
                        />
                      </>
                    ) : markdownPreviewAvailable ? (
                      <article
                        key={`${activeFile?.path}:${activeFile?.sha256}`}
                        className={styles.markdownPreview}
                        aria-label={`${filename(activeFile?.path || "")} 预览`}
                      >
                        <Markdown raw content={activeFile?.content || ""} />
                      </article>
                    ) : null}
                  </div>
                ) : (
                  <section className={styles.fileViewer} aria-label="文件内容">
                    <div className={styles.fileViewerHeader}>
                      <button
                        type="button"
                        className={styles.backToFiles}
                        aria-label="返回工作区文件"
                        onClick={() => changeView("files")}
                      >
                        <ArrowLeft size={14} />
                      </button>
                      <span title={activePath || ""}>
                        {activePath ? filename(activePath) : "打开文件"}
                      </span>
                      <div className={styles.toolbarActions}>
                        {activeFile ? (
                          <em>
                            {lineCount} 行
                            {activeFile.truncated ? " · 已截断" : ""}
                          </em>
                        ) : null}
                        {activePath ? (
                          <Dropdown
                            menu={{ items: activeFileActions }}
                            trigger={["click"]}
                            placement="bottomRight"
                            destroyOnHidden
                          >
                            <button
                              type="button"
                              className={styles.compactActionButton}
                              aria-label="文件操作"
                              title="打开方式与 Finder 操作"
                            >
                              <MoreHorizontal size={15} />
                            </button>
                          </Dropdown>
                        ) : null}
                      </div>
                    </div>
                    <div className={styles.fileViewerBody}>
                      {fileLoading ? (
                        <div className={styles.centerState}>
                          <Spin size="small" />
                        </div>
                      ) : error ? (
                        <div className={styles.centerState}>{error}</div>
                      ) : activeFile ? (
                        <div className={styles.sourceCode} role="table">
                          {sourceLines.map((line, index) => (
                            <div
                              // The line position is stable inside the current file.
                              key={`${activeFile.sha256}:${index}`}
                              className={styles.sourceLine}
                              role="row"
                            >
                              <span
                                className={styles.lineNumber}
                                role="rowheader"
                                aria-label={`第 ${index + 1} 行`}
                              >
                                {index + 1}
                              </span>
                              <code role="cell">{line || " "}</code>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <div className={styles.centerState}>
                          <File size={28} strokeWidth={1.35} />
                          <span>从右侧选择一个文件</span>
                        </div>
                      )}
                    </div>
                  </section>
                )}
              </main>

              <aside className={styles.fileRail} aria-label="工作区文件">
                <div className={styles.fileRailTitle}>
                  <span>工作区文件</span>
                  <em>{tree?.total_entries || 0}</em>
                </div>
                {tree && tree.children.length > 0 ? (
                  <ul className={styles.treeList}>
                    {tree.children.map((node) => (
                      <FileTreeRow
                        key={node.path}
                        node={node}
                        depth={0}
                        activePath={activePath}
                        expanded={expanded}
                        onToggle={toggleExpandedDirectory}
                        onOpen={handleSelectFile}
                        onActivate={(path) =>
                          void openFile(path, undefined, true)
                        }
                        actionItems={actionItems}
                        attachmentRoles={attachmentRoles}
                      />
                    ))}
                  </ul>
                ) : (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="暂无文件"
                  />
                )}
              </aside>
            </div>
          )}

          {treeError && tree ? (
            <div className={styles.partialTreeNotice} role="status">
              本次刷新失败，继续显示上一次内容：{treeError}
            </div>
          ) : null}

          {tree?.truncated || tree?.has_more ? (
            <div className={styles.partialTreeNotice} role="status">
              文件较多，当前仅显示已加载部分；刷新不会移除未加载的已打开条目。
            </div>
          ) : null}
        </>
      )}

      <footer className={styles.footer}>
        <span>
          {panelMode === "compact"
            ? "继续在对话中修改；双击文件可手动接管"
            : panelMode === "workbench"
              ? "沈行知的执行过程保留在聊天时间线中，你可随时手动接管"
              : "源码、预览与文件导航保持在右侧；修改要求继续从对话发出"}
        </span>
      </footer>
    </aside>
    </>
  );
}
