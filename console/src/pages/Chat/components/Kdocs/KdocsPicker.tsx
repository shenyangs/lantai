import { Check, FileText, X } from "lucide-react";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { KdocsDocumentItem } from "../../../../api/types/kdocs";
import KdocsDocumentBrowser from "./KdocsDocumentBrowser";
import styles from "./Kdocs.module.less";

interface KdocsPickerProps {
  open: boolean;
  referencedKeys: ReadonlySet<string>;
  onClose: () => void;
  onConfirm: (documents: KdocsDocumentItem[]) => void;
  onPreview: (document: KdocsDocumentItem) => void;
}

export default function KdocsPicker({
  open,
  referencedKeys,
  onClose,
  onConfirm,
  onPreview,
}: KdocsPickerProps) {
  const [selected, setSelected] = useState<Map<string, KdocsDocumentItem>>(
    () => new Map(),
  );
  const [visibleItems, setVisibleItems] = useState<KdocsDocumentItem[]>([]);
  const dialogRef = useRef<HTMLElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setSelected(new Map());
  }, [open]);

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const frame = window.requestAnimationFrame(() =>
      searchInputRef.current?.focus(),
    );
    return () => {
      window.cancelAnimationFrame(frame);
      const previous = previousFocusRef.current;
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);

  const selectedKeys = useMemo(
    () => new Set([...referencedKeys, ...selected.keys()]),
    [referencedKeys, selected],
  );
  const selectableVisible = visibleItems.filter(
    (item) => !item.is_folder && !referencedKeys.has(item.key),
  );
  const allVisibleSelected =
    selectableVisible.length > 0 &&
    selectableVisible.every((item) => selectedKeys.has(item.key));

  if (!open) return null;

  const toggle = (document: KdocsDocumentItem) => {
    if (referencedKeys.has(document.key)) return;
    setSelected((current) => {
      const next = new Map(current);
      if (next.has(document.key)) next.delete(document.key);
      else next.set(document.key, document);
      return next;
    });
  };

  const toggleVisible = () => {
    setSelected((current) => {
      const next = new Map(current);
      if (allVisibleSelected) {
        selectableVisible.forEach((item) => next.delete(item.key));
      } else {
        selectableVisible.forEach((item) => next.set(item.key, item));
      }
      return next;
    });
  };

  const handleDialogKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [
      ...event.currentTarget.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ),
    ].filter((element) => element.getClientRects().length > 0);
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      className={styles.pickerBackdrop}
      role="presentation"
      onMouseDown={onClose}
    >
      <section
        ref={dialogRef}
        className={styles.picker}
        role="dialog"
        aria-modal="true"
        aria-labelledby="kdocs-picker-title"
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={handleDialogKeyDown}
      >
        <header className={styles.pickerHeader}>
          <span>
            <FileText size={18} strokeWidth={1.8} aria-hidden="true" />
            <strong id="kdocs-picker-title">添加金山云文档</strong>
          </span>
          <button type="button" aria-label="关闭" onClick={onClose}>
            <X size={18} strokeWidth={1.8} />
          </button>
        </header>

        <KdocsDocumentBrowser
          layout="rail"
          selectedKeys={selectedKeys}
          lockedKeys={referencedKeys}
          searchAutoFocus
          searchInputRef={searchInputRef}
          onToggle={toggle}
          onOpen={onPreview}
          onItemsChange={setVisibleItems}
        />

        <footer className={styles.pickerFooter}>
          <button
            type="button"
            className={styles.selectVisible}
            disabled={selectableVisible.length === 0}
            onClick={toggleVisible}
          >
            <span data-checked={allVisibleSelected}>
              {allVisibleSelected ? (
                <Check size={13} strokeWidth={2.3} />
              ) : null}
            </span>
            {allVisibleSelected ? "取消当前列表" : "选择当前列表"}
          </button>
          <div className={styles.selectionSummary}>
            <strong>已选 {selected.size} 项</strong>
            {selected.size ? (
              <span
                title={[...selected.values()]
                  .map((item) => item.name)
                  .join("、")}
              >
                {[...selected.values()]
                  .slice(0, 2)
                  .map((item) => item.name)
                  .join("、")}
                {selected.size > 2 ? ` 等 ${selected.size} 项` : ""}
              </span>
            ) : (
              <span>选择后会在发送前读取正文</span>
            )}
          </div>
          <div className={styles.pickerActions}>
            <button type="button" onClick={onClose}>
              取消
            </button>
            <button
              type="button"
              className={styles.primaryButton}
              disabled={selected.size === 0}
              onClick={() => {
                const fresh = [...selected.values()].filter(
                  (item) => !referencedKeys.has(item.key),
                );
                onConfirm(fresh);
                onClose();
              }}
            >
              添加 {selected.size ? `(${selected.size})` : ""}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
