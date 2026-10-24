import {
  ChevronLeft,
  ChevronRight,
  File,
  FileSpreadsheet,
  FileText,
  Folder,
  FolderClock,
  RefreshCw,
  Search,
  Star,
  Users,
} from "lucide-react";
import {
  type Ref,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { kdocsApi } from "../../../../api/modules/kdocs";
import type {
  KdocsDocumentItem,
  KdocsDocumentView,
} from "../../../../api/types/kdocs";
import { kdocsErrorMessage } from "../../kdocsRead";
import styles from "./Kdocs.module.less";

interface FolderCrumb {
  driveId: string;
  fileId: string;
  name: string;
}

interface KdocsDocumentBrowserProps {
  selectedKeys?: ReadonlySet<string>;
  onToggle?: (document: KdocsDocumentItem) => void;
  onOpen: (document: KdocsDocumentItem) => void;
  onItemsChange?: (documents: KdocsDocumentItem[]) => void;
  layout?: "rail" | "compact";
  initialView?: Exclude<KdocsDocumentView, "folder" | "search">;
  lockedKeys?: ReadonlySet<string>;
  searchAutoFocus?: boolean;
  searchInputRef?: Ref<HTMLInputElement>;
}

const VIEWS: Array<{
  key: Exclude<KdocsDocumentView, "folder" | "search">;
  label: string;
  icon: typeof FolderClock;
}> = [
  { key: "recent", label: "最近", icon: FolderClock },
  { key: "starred", label: "星标", icon: Star },
  { key: "my", label: "我的文档", icon: Folder },
  { key: "shared", label: "与我共享", icon: Users },
];

function documentIcon(document: KdocsDocumentItem) {
  if (document.is_folder) return <Folder size={18} strokeWidth={1.8} />;
  if (["xls", "xlsx", "et", "csv"].includes(document.suffix)) {
    return <FileSpreadsheet size={18} strokeWidth={1.8} />;
  }
  if (["doc", "docx", "wps", "txt", "md", "pdf"].includes(document.suffix)) {
    return <FileText size={18} strokeWidth={1.8} />;
  }
  return <File size={18} strokeWidth={1.8} />;
}

function formatModified(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "";
  const numeric = typeof value === "number" ? value : Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric)
    : new Date(String(value));
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const sameYear = now.getFullYear() === date.getFullYear();
  return new Intl.DateTimeFormat("zh-CN", {
    ...(sameYear ? {} : { year: "numeric" }),
    month: "numeric",
    day: "numeric",
  }).format(date);
}

export default function KdocsDocumentBrowser({
  selectedKeys = new Set<string>(),
  onToggle,
  onOpen,
  onItemsChange,
  layout = "compact",
  initialView = "recent",
  lockedKeys = new Set<string>(),
  searchAutoFocus = false,
  searchInputRef,
}: KdocsDocumentBrowserProps) {
  const [baseView, setBaseView] = useState(initialView);
  const [folderTrail, setFolderTrail] = useState<FolderCrumb[]>([]);
  const [query, setQuery] = useState("");
  const [settledQuery, setSettledQuery] = useState("");
  const [items, setItems] = useState<KdocsDocumentItem[]>([]);
  const [nextPageToken, setNextPageToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [refreshToken, setRefreshToken] = useState(0);
  const requestSequence = useRef(0);

  useEffect(() => {
    const timer = window.setTimeout(() => setSettledQuery(query.trim()), 260);
    return () => window.clearTimeout(timer);
  }, [query]);

  const activeFolder = folderTrail[folderTrail.length - 1];
  const activeView: KdocsDocumentView = settledQuery
    ? "search"
    : activeFolder
      ? "folder"
      : baseView;

  const requestParams = useMemo(
    () => ({
      view: activeView,
      ...(activeView === "folder" && activeFolder
        ? {
            drive_id: activeFolder.driveId,
            parent_id: activeFolder.fileId,
          }
        : {}),
      ...(activeView === "search" ? { query: settledQuery } : {}),
      page_size: 50,
    }),
    [activeFolder, activeView, settledQuery],
  );

  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setLoading(true);
    setLoadingMore(false);
    setError("");
    try {
      const response = await kdocsApi.listDocuments(requestParams);
      if (sequence !== requestSequence.current) return;
      setItems(response.items);
      setNextPageToken(response.next_page_token || "");
    } catch (caught) {
      if (sequence !== requestSequence.current) return;
      setItems([]);
      setNextPageToken("");
      setError(kdocsErrorMessage(caught));
    } finally {
      if (sequence === requestSequence.current) setLoading(false);
    }
  }, [requestParams]);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  useEffect(() => {
    onItemsChange?.(items);
  }, [items, onItemsChange]);

  const loadMore = async () => {
    if (!nextPageToken || loadingMore) return;
    const sequence = requestSequence.current;
    const pageToken = nextPageToken;
    setLoadingMore(true);
    setError("");
    try {
      const response = await kdocsApi.listDocuments({
        ...requestParams,
        page_token: pageToken,
      });
      if (sequence !== requestSequence.current) return;
      setItems((current) => {
        const known = new Set(current.map((item) => item.key));
        return [
          ...current,
          ...response.items.filter((item) => !known.has(item.key)),
        ];
      });
      setNextPageToken(response.next_page_token || "");
    } catch (caught) {
      if (sequence !== requestSequence.current) return;
      setError(kdocsErrorMessage(caught));
    } finally {
      if (sequence === requestSequence.current) setLoadingMore(false);
    }
  };

  const openItem = (document: KdocsDocumentItem) => {
    if (!document.is_folder) {
      onOpen(document);
      return;
    }
    setQuery("");
    setSettledQuery("");
    setFolderTrail((current) => [
      ...current,
      {
        driveId: document.drive_id,
        fileId: document.file_id,
        name: document.name,
      },
    ]);
  };

  const navigation = (
    <nav className={styles.documentNavigation} aria-label="金山文档分类">
      {VIEWS.map(({ key, label, icon: ViewIcon }) => (
        <button
          key={key}
          type="button"
          data-active={!settledQuery && !activeFolder && baseView === key}
          onClick={() => {
            setBaseView(key);
            setFolderTrail([]);
            setQuery("");
            setSettledQuery("");
          }}
        >
          <ViewIcon size={16} strokeWidth={1.8} aria-hidden="true" />
          <span>{label}</span>
        </button>
      ))}
    </nav>
  );

  return (
    <section
      className={styles.documentBrowser}
      data-layout={layout}
      aria-label="浏览金山文档"
    >
      {layout === "rail" ? navigation : null}
      <div className={styles.documentBrowserMain}>
        {layout === "compact" ? navigation : null}
        <div className={styles.documentSearchRow}>
          <label>
            <Search size={15} strokeWidth={1.8} aria-hidden="true" />
            <input
              ref={searchInputRef}
              autoFocus={searchAutoFocus}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索文件名"
              aria-label="搜索金山文档"
            />
          </label>
          <button
            type="button"
            className={styles.iconButton}
            aria-label="刷新文档列表"
            title="刷新"
            onClick={() => setRefreshToken((value) => value + 1)}
          >
            <RefreshCw size={15} strokeWidth={1.8} />
          </button>
        </div>

        {folderTrail.length > 0 && !settledQuery ? (
          <div className={styles.breadcrumbs} aria-label="文件夹路径">
            <button
              type="button"
              onClick={() => setFolderTrail((current) => current.slice(0, -1))}
            >
              <ChevronLeft size={14} aria-hidden="true" />
              返回
            </button>
            <span title={folderTrail.map((crumb) => crumb.name).join(" / ")}>
              {folderTrail.map((crumb) => crumb.name).join(" / ")}
            </span>
          </div>
        ) : null}

        <div className={styles.documentList} aria-live="polite">
          {loading ? (
            <div className={styles.listState} data-state="loading">
              <span className={styles.spinner} aria-hidden="true" />
              <strong>正在读取云文档</strong>
              <small>列表会保留在当前对话中</small>
            </div>
          ) : error ? (
            <div className={styles.listState} data-state="error">
              <strong>暂时无法读取文档</strong>
              <small>{error}</small>
              <button type="button" onClick={() => void load()}>
                重试
              </button>
            </div>
          ) : items.length === 0 ? (
            <div className={styles.listState}>
              <strong>
                {settledQuery ? "没有找到匹配文档" : "这里还没有文档"}
              </strong>
              <small>
                {settledQuery
                  ? "换一个文件名或关键词试试"
                  : "可切换分类，或在上方搜索"}
              </small>
            </div>
          ) : (
            items.map((document) => {
              const selected = selectedKeys.has(document.key);
              const locked = lockedKeys.has(document.key);
              const owner =
                document.owner?.name || document.modifier?.name || "";
              const modified = formatModified(document.modified_at);
              return (
                <div
                  key={document.key}
                  className={styles.documentRow}
                  data-folder={document.is_folder}
                  data-selected={selected}
                >
                  {onToggle && !document.is_folder ? (
                    <input
                      type="checkbox"
                      checked={selected}
                      disabled={locked}
                      aria-label={
                        locked
                          ? `已引用${document.name}`
                          : `${selected ? "取消选择" : "选择"}${document.name}`
                      }
                      onChange={() => onToggle(document)}
                    />
                  ) : (
                    <span className={styles.rowIndent} aria-hidden="true" />
                  )}
                  <button
                    type="button"
                    className={styles.documentOpenButton}
                    aria-label={`${document.is_folder ? "打开文件夹" : "打开"}${
                      document.name || "未命名文档"
                    }`}
                    onClick={() => openItem(document)}
                  >
                    <span
                      className={styles.documentIcon}
                      data-suffix={document.suffix}
                    >
                      {documentIcon(document)}
                    </span>
                    <span className={styles.documentIdentity}>
                      <strong title={document.name}>
                        {document.name || "未命名文档"}
                      </strong>
                      <small>
                        {[document.location, owner]
                          .filter(Boolean)
                          .join(" · ") ||
                          (document.is_folder ? "文件夹" : "金山云文档")}
                      </small>
                    </span>
                    <span className={styles.documentMeta}>{modified}</span>
                    <ChevronRight
                      size={15}
                      strokeWidth={1.7}
                      aria-hidden="true"
                    />
                  </button>
                </div>
              );
            })
          )}
        </div>

        {!loading && items.length > 0 && nextPageToken ? (
          <button
            type="button"
            className={styles.loadMore}
            disabled={loadingMore}
            onClick={() => void loadMore()}
          >
            {loadingMore ? "正在加载…" : "加载更多"}
          </button>
        ) : null}
      </div>
    </section>
  );
}
