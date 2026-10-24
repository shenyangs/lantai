import {
  AlertCircle,
  CheckCircle2,
  FileText,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { kdocsApi } from "../../../../api/modules/kdocs";
import type { KdocsDocumentItem } from "../../../../api/types/kdocs";
import {
  matchKdocsMention,
  replaceKdocsMention,
  type KdocsMentionMatch,
} from "../../kdocsMention";
import { kdocsErrorMessage } from "../../kdocsRead";
import {
  kdocsReferenceBudgets,
  type KdocsReference,
} from "../../kdocsReferences";
import styles from "./Kdocs.module.less";

interface KdocsComposerContextProps {
  references: KdocsReference[];
  getTextarea: () => HTMLTextAreaElement | null;
  setTextareaText: (
    textarea: HTMLTextAreaElement,
    value: string,
    cursor?: number,
  ) => void;
  isComposingRef: RefObject<boolean>;
  onAddDocument: (document: KdocsDocumentItem) => void;
  onOpenReference: (reference: KdocsReference) => void;
  onRemoveReference: (key: string) => void;
  onRetryReference: (reference: KdocsReference) => void;
}

const STATUS_LABELS: Record<KdocsReference["read_status"], string> = {
  referenced: "已引用",
  reading: "读取中",
  read: "已读取",
  partial: "正文已读取 · 格式未完全展开",
  original_updated: "原文已更新",
  failed: "读取失败",
};

export default function KdocsComposerContext({
  references,
  getTextarea,
  setTextareaText,
  isComposingRef,
  onAddDocument,
  onOpenReference,
  onRemoveReference,
  onRetryReference,
}: KdocsComposerContextProps) {
  const [mention, setMention] = useState<KdocsMentionMatch | null>(null);
  const [results, setResults] = useState<KdocsDocumentItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const requestSequence = useRef(0);
  const mentionTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const mentionListId = `kdocs-mention-${useId().replace(/:/g, "")}`;

  const referenceDocumentKeys = useMemo(
    () => new Set(references.map((item) => `${item.drive_id}:${item.file_id}`)),
    [references],
  );

  useEffect(() => {
    const updateMention = (target: EventTarget | null) => {
      const textarea = getTextarea();
      if (!textarea || target !== textarea || isComposingRef.current) return;
      mentionTextareaRef.current = textarea;
      const next = matchKdocsMention(
        textarea.value,
        textarea.selectionStart ?? textarea.value.length,
      );
      setMention(next);
      setActiveIndex(0);
    };
    const handleInput = (event: Event) => updateMention(event.target);
    const handleClick = (event: Event) => updateMention(event.target);
    document.addEventListener("input", handleInput, true);
    document.addEventListener("click", handleClick, true);
    return () => {
      document.removeEventListener("input", handleInput, true);
      document.removeEventListener("click", handleClick, true);
    };
  }, [getTextarea, isComposingRef]);

  useEffect(() => {
    if (!mention) return;
    const textarea = mentionTextareaRef.current ?? getTextarea();
    if (!textarea) return;
    mentionTextareaRef.current = textarea;
    const attributes = [
      "role",
      "aria-autocomplete",
      "aria-expanded",
      "aria-controls",
      "aria-haspopup",
    ] as const;
    const original = new Map(
      attributes.map((attribute) => [
        attribute,
        textarea.getAttribute(attribute),
      ]),
    );
    const openShells = [
      textarea.closest<HTMLElement>(".lantai-sender"),
      textarea.closest<HTMLElement>(".lantai-chat-anywhere-input"),
    ].filter((element): element is HTMLElement => Boolean(element));
    const originalShellMarkers = new Map(
      openShells.map((element) => [
        element,
        element.getAttribute("data-kdocs-mention-open"),
      ]),
    );
    textarea.setAttribute("role", "combobox");
    textarea.setAttribute("aria-autocomplete", "list");
    textarea.setAttribute("aria-expanded", "true");
    textarea.setAttribute("aria-controls", mentionListId);
    textarea.setAttribute("aria-haspopup", "listbox");
    openShells.forEach((element) =>
      element.setAttribute("data-kdocs-mention-open", "true"),
    );
    return () => {
      attributes.forEach((attribute) => {
        const value = original.get(attribute);
        if (value === null || value === undefined) {
          textarea.removeAttribute(attribute);
        } else {
          textarea.setAttribute(attribute, value);
        }
      });
      originalShellMarkers.forEach((value, element) => {
        if (value === null) {
          element.removeAttribute("data-kdocs-mention-open");
        } else {
          element.setAttribute("data-kdocs-mention-open", value);
        }
      });
      textarea.removeAttribute("aria-activedescendant");
      mentionTextareaRef.current = null;
    };
  }, [getTextarea, mention, mentionListId]);

  useEffect(() => {
    if (!mention) return;
    const textarea = mentionTextareaRef.current;
    const activeOption = results[activeIndex];
    if (!textarea || !activeOption) {
      textarea?.removeAttribute("aria-activedescendant");
      return;
    }
    const activeId = `${mentionListId}-option-${activeIndex}`;
    textarea.setAttribute("aria-activedescendant", activeId);
    return () => {
      if (textarea.getAttribute("aria-activedescendant") === activeId) {
        textarea.removeAttribute("aria-activedescendant");
      }
    };
  }, [activeIndex, mention, mentionListId, results]);

  useEffect(() => {
    if (!mention) {
      setResults([]);
      setError("");
      setLoading(false);
      return;
    }
    const sequence = ++requestSequence.current;
    const timer = window.setTimeout(
      async () => {
        setLoading(true);
        setError("");
        try {
          const query = mention.query.trim();
          const response = await kdocsApi.listDocuments({
            view: query ? "search" : "recent",
            ...(query ? { query } : {}),
            page_size: 8,
          });
          if (sequence !== requestSequence.current) return;
          setResults(
            response.items.filter((item) => !item.is_folder).slice(0, 8),
          );
        } catch (caught) {
          if (sequence !== requestSequence.current) return;
          setResults([]);
          setError(kdocsErrorMessage(caught));
        } finally {
          if (sequence === requestSequence.current) setLoading(false);
        }
      },
      mention.query.trim() ? 220 : 80,
    );
    return () => window.clearTimeout(timer);
  }, [mention]);

  const selectDocument = useCallback(
    (document: KdocsDocumentItem) => {
      const textarea = mentionTextareaRef.current ?? getTextarea();
      if (textarea && mention) {
        const replacement = replaceKdocsMention(
          textarea.value,
          mention,
          document.name,
        );
        setTextareaText(textarea, replacement.value, replacement.cursor);
      }
      onAddDocument(document);
      setMention(null);
      window.setTimeout(() => getTextarea()?.focus(), 0);
    },
    [getTextarea, mention, onAddDocument, setTextareaText],
  );

  useEffect(() => {
    if (!mention) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isComposingRef.current || event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        setMention(null);
        return;
      }
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!results.length) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        setActiveIndex((current) =>
          event.key === "ArrowDown"
            ? (current + 1) % results.length
            : (current - 1 + results.length) % results.length,
        );
        return;
      }
      if (event.key === "Enter" && results[activeIndex]) {
        event.preventDefault();
        event.stopImmediatePropagation();
        selectDocument(results[activeIndex]);
      }
    };
    document.addEventListener("keydown", handleKeyDown, true);
    return () => document.removeEventListener("keydown", handleKeyDown, true);
  }, [activeIndex, isComposingRef, mention, results, selectDocument]);

  const budgets = kdocsReferenceBudgets(references);
  return (
    <div className={styles.composerContext}>
      {mention ? (
        <div
          id={mentionListId}
          className={styles.mentionMenu}
          role="listbox"
          aria-label="引用金山文档"
          aria-busy={loading}
        >
          <header>
            <span>引用金山文档</span>
            <kbd>↑↓</kbd>
            <kbd>Enter</kbd>
          </header>
          {loading ? (
            <div className={styles.mentionState}>
              <Loader2 size={15} className={styles.spinIcon} />
              正在查找文档
            </div>
          ) : error ? (
            <div className={styles.mentionState} data-state="error">
              <AlertCircle size={15} />
              <span>{error}</span>
            </div>
          ) : results.length ? (
            results.map((document, index) => (
              <button
                key={document.key}
                id={`${mentionListId}-option-${index}`}
                type="button"
                role="option"
                aria-selected={index === activeIndex}
                data-active={index === activeIndex}
                onMouseEnter={() => setActiveIndex(index)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => selectDocument(document)}
              >
                <FileText size={16} strokeWidth={1.8} />
                <span>
                  <strong>{document.name}</strong>
                  <small>
                    {document.location || document.owner?.name || "金山云文档"}
                  </small>
                </span>
                {referenceDocumentKeys.has(document.key) ? (
                  <em>已引用</em>
                ) : null}
              </button>
            ))
          ) : (
            <div className={styles.mentionState}>
              {mention.query.trim() ? "没有匹配的文档" : "最近没有可引用的文档"}
            </div>
          )}
        </div>
      ) : null}

      {references.length ? (
        <div className={styles.referenceStrip} aria-label="已引用的金山文档">
          {references.map((reference, index) => (
            <div
              key={reference.key}
              className={styles.referenceChip}
              data-status={reference.read_status}
            >
              <button
                type="button"
                className={styles.referenceMain}
                title={reference.path_snapshot || reference.name_snapshot}
                onClick={() => onOpenReference(reference)}
              >
                {reference.read_status === "reading" ? (
                  <Loader2 size={14} className={styles.spinIcon} />
                ) : reference.read_status === "failed" ? (
                  <AlertCircle size={14} />
                ) : (
                  <CheckCircle2 size={14} />
                )}
                <span>
                  <strong>{reference.name_snapshot}</strong>
                  <small>
                    {reference.kind === "excerpt" ? "选中片段 · " : ""}
                    {reference.read_status === "partial"
                      ? reference.content_snapshot?.truncated
                        ? "正文仅载入部分"
                        : reference.coverage?.limitations.includes("images")
                          ? "图片尚未读取"
                          : reference.coverage?.body === "unknown"
                            ? "读取范围待确认"
                            : STATUS_LABELS.partial
                      : STATUS_LABELS[reference.read_status]}
                    {reference.excerpt?.location
                      ? ` · ${reference.excerpt.location}`
                      : ""}
                    {reference.kind === "file" &&
                    budgets[index].source > budgets[index].included
                      ? ` · 本次发送 ${budgets[index].included.toLocaleString()} / ${budgets[index].source.toLocaleString()} 字`
                      : ""}
                  </small>
                </span>
              </button>
              {reference.read_status === "failed" ||
              reference.read_status === "original_updated" ? (
                <button
                  type="button"
                  className={styles.chipAction}
                  aria-label={`重新读取${reference.name_snapshot}`}
                  title="重新读取"
                  onClick={() => onRetryReference(reference)}
                >
                  <RefreshCw size={13} />
                </button>
              ) : null}
              <button
                type="button"
                className={styles.chipAction}
                aria-label={`移除${reference.name_snapshot}`}
                title="移除引用"
                onClick={() => onRemoveReference(reference.key)}
              >
                <X size={13} />
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
