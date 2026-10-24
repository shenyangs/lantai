import {
  ArrowLeft,
  BookOpenText,
  Check,
  ExternalLink,
  FilePlus2,
  GripVertical,
  Loader2,
  RefreshCw,
  RotateCcw,
  X,
} from "lucide-react";
import {
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import KdocsReaderBody, { type ReadingPosition } from "./KdocsReaderBody";
import { kdocsApi } from "../../../../api/modules/kdocs";
import type { KdocsDocumentItem } from "../../../../api/types/kdocs";
import { openExternalUrl } from "../../../../utils/openExternalUrl";
import {
  kdocsErrorMessage,
  kdocsPartialReadMessage,
  readKdocsDocumentSafely,
} from "../../kdocsRead";
import {
  documentToKdocsReference,
  excerptToKdocsReference,
  type KdocsReference,
} from "../../kdocsReferences";
import KdocsDocumentBrowser from "./KdocsDocumentBrowser";
import styles from "./Kdocs.module.less";

interface KdocsWorkspacePanelProps {
  open: boolean;
  references: KdocsReference[];
  requestedDocument: KdocsDocumentItem | null;
  requestedReference?: KdocsReference | null;
  onRequestedDocumentHandled: () => void;
  onClose: () => void;
  onAddDocument: (
    document: KdocsDocumentItem,
    prepared?: KdocsReference,
  ) => void;
  onAddExcerpt: (reference: KdocsReference) => void;
  onRemoveDocument: (driveId: string, fileId: string) => void;
  onMarkDocumentUpdated: (document: KdocsDocumentItem) => void;
}

const PANEL_WIDTH_KEY = "lantai:kdocs-panel-width";
const MIN_PANEL_WIDTH = 420;
const MAX_PANEL_WIDTH = 760;
const MIN_CHAT_WIDTH = 390;

function initialPanelWidth(): number {
  if (typeof window === "undefined") return 520;
  const stored = Number(window.localStorage.getItem(PANEL_WIDTH_KEY) || 520);
  return Number.isFinite(stored)
    ? Math.max(MIN_PANEL_WIDTH, Math.min(MAX_PANEL_WIDTH, stored))
    : 520;
}

export default function KdocsWorkspacePanel({
  open,
  references,
  requestedDocument,
  requestedReference,
  onRequestedDocumentHandled,
  onClose,
  onAddDocument,
  onAddExcerpt,
  onRemoveDocument,
  onMarkDocumentUpdated,
}: KdocsWorkspacePanelProps) {
  const [panelWidth, setPanelWidth] = useState(initialPanelWidth);
  const [document, setDocument] = useState<KdocsDocumentItem | null>(null);
  const [reader, setReader] = useState<KdocsReference | null>(null);
  const [selection, setSelection] = useState<{
    text: string;
    location: string;
    start: number;
    end: number;
  } | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [freshness, setFreshness] = useState("");
  const [recent, setRecent] = useState<
    Array<{ document: KdocsDocumentItem; reader: KdocsReference }>
  >([]);
  const positions = useRef(new Map<string, ReadingPosition>());
  const returnedFromOriginal = useRef(false);
  const [target, setTarget] = useState<KdocsReference["excerpt"]>();
  const checkRef = useRef<() => void>(() => {});
  const [updated, setUpdated] = useState(false);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [openingOriginal, setOpeningOriginal] = useState(false);
  const [actionError, setActionError] = useState("");
  const [hostWidth, setHostWidth] = useState(0);
  const isOverlay = hostWidth > 0 && hostWidth <= 1100;
  const contentRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const expandedRef = useRef(expanded);
  const readSequence = useRef(0);
  const resizeStart = useRef<{ x: number; width: number } | null>(null);

  const selectedKeys = useMemo(
    () => new Set(references.map((item) => `${item.drive_id}:${item.file_id}`)),
    [references],
  );
  const wholeDocumentReferenced = document
    ? references.some(
        (item) =>
          item.kind === "file" &&
          item.drive_id === document.drive_id &&
          item.file_id === document.file_id,
      )
    : false;

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    expandedRef.current = expanded;
  }, [expanded]);

  useEffect(() => {
    if (!open) return;
    const host = panelRef.current?.parentElement;
    if (!host) return;
    const sync = (width?: number) => {
      setHostWidth(
        Math.round(
          width || host.getBoundingClientRect().width || window.innerWidth,
        ),
      );
    };
    sync();
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver((entries) =>
        sync(entries[0]?.contentRect.width),
      );
      observer.observe(host);
      return () => observer.disconnect();
    }
    const handleWindowResize = () => sync();
    window.addEventListener("resize", handleWindowResize);
    return () => window.removeEventListener("resize", handleWindowResize);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const active = globalThis.document.activeElement;
    if (
      active instanceof HTMLElement &&
      !panelRef.current?.contains(active)
    ) {
      previousFocusRef.current = active;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      if (expandedRef.current) setExpanded(false);
      else onCloseRef.current();
    };
    globalThis.document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      globalThis.document.removeEventListener("keydown", handleKeyDown, true);
      previousFocusRef.current?.focus();
      previousFocusRef.current = null;
    };
  }, [open]);

  useEffect(() => {
    if (!open || !(isOverlay || expanded)) return;
    const panel = panelRef.current;
    if (!panel) return;
    const focusableSelector = [
      "button:not([disabled])",
      "input:not([disabled])",
      "textarea:not([disabled])",
      "select:not([disabled])",
      "a[href]",
      '[tabindex]:not([tabindex="-1"])',
    ].join(",");
    const focusCloseButton = () => closeButtonRef.current?.focus();
    const frame = window.requestAnimationFrame(focusCloseButton);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(focusableSelector),
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
    const handleFocusIn = (event: FocusEvent) => {
      if (!panel.contains(event.target as Node)) focusCloseButton();
    };
    globalThis.document.addEventListener("keydown", handleKeyDown, true);
    globalThis.document.addEventListener("focusin", handleFocusIn, true);
    return () => {
      window.cancelAnimationFrame(frame);
      globalThis.document.removeEventListener("keydown", handleKeyDown, true);
      globalThis.document.removeEventListener("focusin", handleFocusIn, true);
    };
  }, [isOverlay, expanded, open]);

  const readDocument = async (
    nextDocument: KdocsDocumentItem,
    syncReference = false,
  ) => {
    const sequence = ++readSequence.current;
    setCheckingUpdate(false);
    setDocument(nextDocument);
    setReader({
      ...documentToKdocsReference(nextDocument),
      read_status: "reading",
    });
    setSelection(null);
    setTarget(undefined);
    setFreshness("");
    setUpdated(false);
    setActionError("");
    const result = await readKdocsDocumentSafely(nextDocument);
    if (sequence !== readSequence.current) return;
    if (
      result.read_status === "failed" &&
      reader?.content_snapshot &&
      document?.key === nextDocument.key
    ) {
      setReader(reader);
      setActionError(result.error_message || "读取失败，已保留原有正文");
      return;
    }
    setReader(result);
    if (syncReference) onAddDocument(nextDocument, result);
  };

  useEffect(() => {
    if (!open || !requestedDocument) return;
    const storedReference =
      requestedReference ||
      references.find(
        (item) =>
          item.kind === "file" &&
          item.drive_id === requestedDocument.drive_id &&
          item.file_id === requestedDocument.file_id,
      );
    const hasStoredResult = Boolean(
      storedReference &&
      (storedReference.content_snapshot ||
        ["read", "partial", "original_updated", "failed"].includes(
          storedReference.read_status,
        )),
    );
    if (storedReference && hasStoredResult) {
      readSequence.current += 1;
      setCheckingUpdate(false);
      setFreshness("");
      setDocument(requestedDocument);
      setReader(storedReference);
      setSelection(null);
      setTarget(requestedReference?.excerpt);
      setUpdated(storedReference.read_status === "original_updated");
      setActionError("");
    } else {
      void readDocument(requestedDocument);
    }
    onRequestedDocumentHandled();
    // This effect handles a one-shot request object. Parent callbacks and the
    // live reference array must not reopen the reader after that hand-off.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, requestedDocument]);

  useEffect(() => {
    if (!document || !reader?.content_snapshot) return;
    setRecent((items) =>
      [
        { document, reader },
        ...items.filter((item) => item.document.key !== document.key),
      ].slice(0, 12),
    );
  }, [document, reader]);

  useEffect(() => {
    const check = () => {
      if (
        !returnedFromOriginal.current ||
        globalThis.document.visibilityState === "hidden"
      )
        return;
      returnedFromOriginal.current = false;
      checkRef.current();
    };
    window.addEventListener("focus", check);
    globalThis.document.addEventListener("visibilitychange", check);
    return () => {
      window.removeEventListener("focus", check);
      globalThis.document.removeEventListener("visibilitychange", check);
    };
  }, []);

  useEffect(() => {
    if (!document || !reader?.content_snapshot?.text) return;
    const handleSelection = () => {
      const root = contentRef.current;
      const selected = window.getSelection();
      if (
        !root ||
        !selected ||
        selected.rangeCount === 0 ||
        selected.isCollapsed
      ) {
        setSelection(null);
        return;
      }
      const range = selected.getRangeAt(0);
      if (!root.contains(range.commonAncestorContainer)) {
        setSelection(null);
        return;
      }
      const text = selected.toString().trim();
      if (!text) {
        setSelection(null);
        return;
      }
      const before = range.cloneRange();
      before.selectNodeContents(root);
      before.setEnd(range.startContainer, range.startOffset);
      const start = before.toString().length;
      const headings = Array.from(root.querySelectorAll("h1,h2,h3,h4,h5,h6"));
      const heading = headings
        .filter(
          (item) =>
            Boolean(
              item.compareDocumentPosition(range.startContainer) &
              Node.DOCUMENT_POSITION_FOLLOWING,
            ) || item.contains(range.startContainer),
        )
        .slice(-1)[0];
      setSelection({
        text,
        location: heading?.textContent || "正文选区",
        start,
        end: start + selected.toString().length,
      });
    };
    globalThis.document.addEventListener("selectionchange", handleSelection);
    return () =>
      globalThis.document.removeEventListener(
        "selectionchange",
        handleSelection,
      );
  }, [document, reader?.content_snapshot?.text]);

  const handleResizeStart = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (isOverlay) return;
    resizeStart.current = { x: event.clientX, width: panelWidth };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const handleResizeMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!resizeStart.current) return;
    const max = Math.min(
      MAX_PANEL_WIDTH,
      (hostWidth || window.innerWidth) - MIN_CHAT_WIDTH,
    );
    const next = Math.max(
      MIN_PANEL_WIDTH,
      Math.min(
        max,
        resizeStart.current.width + resizeStart.current.x - event.clientX,
      ),
    );
    setPanelWidth(next);
  };
  const handleResizeEnd = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (!resizeStart.current) return;
    resizeStart.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    window.localStorage.setItem(
      PANEL_WIDTH_KEY,
      String(Math.round(panelWidth)),
    );
  };

  const checkForUpdates = async () => {
    if (!document || checkingUpdate) return;
    const sequence = readSequence.current;
    setCheckingUpdate(true);
    setActionError("");
    try {
      const latest = await kdocsApi.getDocumentInfo(document.file_id);
      if (sequence !== readSequence.current) return;
      const baselineVersion =
        reader?.read_snapshot?.version || document.version;
      const baselineHash =
        reader?.read_snapshot?.content_hash || document.content_hash;
      const changed = Boolean(
        (baselineVersion &&
          latest.version &&
          baselineVersion !== latest.version) ||
        (baselineHash &&
          latest.content_hash &&
          baselineHash !== latest.content_hash),
      );
      const mergedDocument = {
        ...document,
        ...latest,
        drive_id: latest.drive_id || document.drive_id,
      };
      setDocument(mergedDocument);
      setUpdated(changed);
      setFreshness(
        changed
          ? "在线原文有更新"
          : (baselineVersion && latest.version) ||
              (baselineHash && latest.content_hash)
            ? "已核对，原文未变化"
            : "来源未提供版本信息，暂时无法确认是否更新",
      );
      if (changed) onMarkDocumentUpdated(mergedDocument);
    } catch (caught) {
      if (sequence === readSequence.current)
        setActionError(kdocsErrorMessage(caught));
    } finally {
      if (sequence === readSequence.current) setCheckingUpdate(false);
    }
  };

  checkRef.current = () => {
    if (open) void checkForUpdates();
  };

  const openOriginal = async () => {
    if (!document || openingOriginal) return;
    setOpeningOriginal(true);
    setActionError("");
    try {
      const url =
        document.original_url ||
        (await kdocsApi.getDocumentLink(document.file_id)).url;
      await openExternalUrl(url);
      returnedFromOriginal.current = true;
    } catch (caught) {
      setActionError(kdocsErrorMessage(caught));
    } finally {
      setOpeningOriginal(false);
    }
  };

  const panelStyle = {
    "--kdocs-panel-width": `${panelWidth}px`,
  } as CSSProperties;

  if (!open) return null;
  const positionKey = `${document?.key}:${reader?.snapshot_id || reader?.read_snapshot?.read_at || ""}`;

  return (
    <>
      {isOverlay || expanded ? (
        <div
          className={expanded ? styles.expandedScrim : styles.workspaceScrim}
          aria-hidden="true"
          onPointerDown={() => onCloseRef.current()}
        />
      ) : null}
      <aside
        ref={panelRef}
        className={`${styles.workspacePanel} ${expanded ? styles.expandedReader : ""}`}
        style={panelStyle}
        aria-label="金山文档"
        role={isOverlay || expanded ? "dialog" : undefined}
        aria-modal={isOverlay || expanded ? "true" : undefined}
        data-layout={isOverlay ? "overlay" : "split"}
      >
        <button
          type="button"
          className={styles.resizeHandle}
          aria-label="调整金山文档面板宽度"
          onPointerDown={handleResizeStart}
          onPointerMove={handleResizeMove}
          onPointerUp={handleResizeEnd}
          onPointerCancel={handleResizeEnd}
        >
          <GripVertical size={13} />
        </button>
        <header className={styles.panelHeader}>
          <span>
            <BookOpenText size={18} strokeWidth={1.8} aria-hidden="true" />
            <strong>金山文档</strong>
            <small>{document ? "正文阅读" : "云文档空间"}</small>
          </span>
          <button
            ref={closeButtonRef}
            type="button"
            aria-label="关闭金山文档"
            onClick={() => onCloseRef.current()}
          >
            <X size={18} strokeWidth={1.8} />
          </button>
        </header>

        <div className={styles.panelBody}>
          <div className={styles.browserLayer} data-hidden={Boolean(document)}>
            <KdocsDocumentBrowser
              layout="compact"
              selectedKeys={selectedKeys}
              onToggle={(item) => {
                if (selectedKeys.has(item.key)) {
                  onRemoveDocument(item.drive_id, item.file_id);
                } else {
                  onAddDocument(item);
                }
              }}
              onOpen={(item) => void readDocument(item)}
            />
          </div>

          {document ? (
            <article className={styles.reader}>
              <div className={styles.readerToolbar}>
                <button
                  type="button"
                  onClick={() => {
                    readSequence.current += 1;
                    setCheckingUpdate(false);
                    setDocument(null);
                  }}
                >
                  <ArrowLeft size={15} />
                  文档列表
                </button>
                <span />
                <button
                  type="button"
                  onClick={() => setExpanded((value) => !value)}
                >
                  {expanded ? "收起阅读" : "展开阅读"}
                </button>
                <button
                  type="button"
                  disabled={checkingUpdate}
                  onClick={() => void checkForUpdates()}
                >
                  {checkingUpdate ? (
                    <Loader2 size={14} className={styles.spinIcon} />
                  ) : (
                    <RefreshCw size={14} />
                  )}
                  检查更新
                </button>
                <button
                  type="button"
                  disabled={openingOriginal}
                  onClick={() => void openOriginal()}
                  title="将在浏览器中打开；浏览器登录状态与澜台连接状态相互独立"
                >
                  <ExternalLink size={14} />
                  {document.permissions.can_edit ? "在线编辑" : "查看原文"}
                </button>
              </div>
              <header className={styles.readerHeader}>
                <div>
                  <strong>{document.name}</strong>
                  <small>
                    {document.location || document.owner?.name || "金山云文档"}
                  </small>
                </div>
                <button
                  type="button"
                  className={
                    wholeDocumentReferenced
                      ? styles.referencedButton
                      : styles.primaryButton
                  }
                  disabled={reader?.read_status === "reading"}
                  onClick={() => reader && onAddDocument(document, reader)}
                >
                  {wholeDocumentReferenced ? (
                    <Check size={14} />
                  ) : (
                    <FilePlus2 size={14} />
                  )}
                  {wholeDocumentReferenced ? "更新待发送引用" : "引用整篇"}
                </button>
              </header>

              {updated ? (
                <div className={styles.updateNotice}>
                  <span>
                    <strong>在线原文有新版本</strong>
                    <small>阅读内容与聊天引用各自保留快照。</small>
                  </span>
                  <button
                    type="button"
                    onClick={() => void readDocument(document)}
                  >
                    <RotateCcw size={14} />
                    重新读取
                  </button>
                </div>
              ) : null}
              {actionError ? (
                <div className={styles.inlineError}>{actionError}</div>
              ) : null}

              <div className={styles.readingMeta}>
                <span>
                  {reader?.read_snapshot?.read_at
                    ? `读取于 ${new Date(reader.read_snapshot.read_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
                    : ""}{" "}
                  {freshness}
                </span>
                {recent.length > 1 ? (
                  <select
                    aria-label="最近阅读"
                    value={document.key}
                    onChange={(event) => {
                      const item = recent.find(
                        (entry) => entry.document.key === event.target.value,
                      );
                      if (item) {
                        readSequence.current += 1;
                        setCheckingUpdate(false);
                        setDocument(item.document);
                        setReader(item.reader);
                        setSelection(null);
                        setTarget(undefined);
                        setUpdated(false);
                        setFreshness("");
                      }
                    }}
                  >
                    {recent.map((item) => (
                      <option key={item.document.key} value={item.document.key}>
                        {item.document.name}
                      </option>
                    ))}
                  </select>
                ) : null}
              </div>
              {reader?.content_snapshot?.text ? (
                <KdocsReaderBody
                  key={positionKey}
                  text={reader.content_snapshot.text}
                  contentRef={contentRef}
                  position={
                    positions.current.get(positionKey) || {
                      scroll: 0,
                      query: "",
                      match: 0,
                    }
                  }
                  onPosition={(position) => {
                    positions.current.set(positionKey, position);
                    if (positions.current.size > 24)
                      positions.current.delete(
                        positions.current.keys().next().value!,
                      );
                  }}
                  onError={setActionError}
                  target={target}
                />
              ) : (
                <div className={styles.readerState}>
                  <strong>
                    {reader?.read_status === "reading"
                      ? "正在读取正文"
                      : reader?.read_status === "failed"
                        ? "正文读取失败"
                        : "文档没有可显示的文本"}
                  </strong>
                  <small>{reader?.error_message}</small>
                  {reader?.read_status === "failed" ? (
                    <button
                      type="button"
                      onClick={() => void readDocument(document)}
                    >
                      重试
                    </button>
                  ) : null}
                </div>
              )}
              {reader?.continuation ? (
                <button
                  type="button"
                  className={styles.loadMore}
                  onClick={async () => {
                    const sequence = ++readSequence.current;
                    const next = await readKdocsDocumentSafely(document, {
                      previous: reader,
                    });
                    if (sequence !== readSequence.current) return;
                    if (next.read_status === "failed")
                      setActionError(next.error_message || "继续读取失败");
                    else setReader(next);
                  }}
                >
                  继续读取正文
                </button>
              ) : null}

              {reader?.read_status === "partial" ? (
                <div className={styles.partialNotice}>
                  {kdocsPartialReadMessage({
                    truncated: reader.content_snapshot?.truncated,
                    warning: reader.error_message,
                    coverage: reader.coverage,
                  })}
                </div>
              ) : null}

              {selection ? (
                <div className={styles.selectionBar}>
                  <span>
                    已选择 {selection.text.length} 个字 · {selection.location}
                  </span>
                  <button
                    type="button"
                    className={styles.primaryButton}
                    disabled={selection.text.length > 8000}
                    onClick={() => {
                      const excerpt = excerptToKdocsReference(
                        document,
                        selection.text,
                        selection.location,
                      );
                      onAddExcerpt({
                        ...excerpt,
                        read_snapshot: reader?.read_snapshot,
                        snapshot_id: reader?.snapshot_id,
                        content_snapshot: reader?.content_snapshot,
                        excerpt: {
                          text: selection.text,
                          location: selection.location,
                          start: selection.start,
                          end: selection.end,
                        },
                      });
                      window.getSelection()?.removeAllRanges();
                      setSelection(null);
                    }}
                  >
                    {selection.text.length > 8000
                      ? "请缩小选区至 8000 字内"
                      : "引用选中内容"}
                  </button>
                </div>
              ) : null}
            </article>
          ) : null}
        </div>
      </aside>
    </>
  );
}
