import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { openExternalUrl } from "../../../../utils/openExternalUrl";
import { findTextMatches, textRange } from "./kdocsReaderNavigation";
import styles from "./Kdocs.module.less";

export interface ReadingPosition {
  scroll: number;
  query: string;
  match: number;
}
interface Props {
  text: string;
  contentRef: RefObject<HTMLDivElement>;
  position: ReadingPosition;
  onPosition: (position: ReadingPosition) => void;
  onError: (message: string) => void;
  target?: { text: string; start?: number; end?: number };
}

// Highlight ranges leave React's DOM and the document's selectable text intact.
function paint(name: string, ranges: Range[]) {
  const api = globalThis as unknown as {
    CSS?: { highlights?: Map<string, unknown> };
    Highlight?: new (...ranges: Range[]) => unknown;
  };
  if (api.CSS?.highlights && api.Highlight) {
    if (ranges.length)
      api.CSS.highlights.set(name, new api.Highlight(...ranges));
    else api.CSS.highlights.delete(name);
  }
}
function reveal(range: Range | null) {
  const node = range?.startContainer;
  (node instanceof Element ? node : node?.parentElement)?.scrollIntoView?.({
    block: "center",
  });
}

interface MarkdownNode {
  type: string;
  value?: string;
  children?: MarkdownNode[];
}
function safeLineBreaks() {
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (node.type === "html" && /^<br\s*\/?\s*>$/i.test(node.value || "")) {
        node.type = "break";
        delete node.value;
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}

export default function KdocsReaderBody({
  text,
  contentRef,
  position,
  onPosition,
  onError,
  target,
}: Props) {
  const [query, setQuery] = useState(position.query);
  const [match, setMatch] = useState(position.match);
  const [rendered, setRendered] = useState("");
  const [headings, setHeadings] = useState<
    Array<{ id: string; title: string; level: number }>
  >([]);
  const interacted = useRef(false);
  const searchRef = useRef<HTMLInputElement>(null);
  const stateRef = useRef({ query, match });
  stateRef.current = { query, match };
  const matches = useMemo(
    () => findTextMatches(rendered, query),
    [rendered, query],
  );

  useEffect(() => {
    const root = contentRef.current;
    if (!root) return;
    setRendered(root.textContent || "");
    setHeadings(
      Array.from(root.querySelectorAll("h1,h2,h3,h4,h5,h6")).map(
        (heading, index) => {
          heading.id = `kdocs-heading-${index}`;
          return {
            id: heading.id,
            title: heading.textContent || "",
            level: Number(heading.tagName[1]),
          };
        },
      ),
    );
    root.scrollTop = position.scroll;
    return () => {
      paint("kdocs-search", []);
      paint("kdocs-active", []);
    };
    // A snapshot owns its initial position; subsequent scrolling is saved by the parent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  useEffect(() => {
    const root = contentRef.current;
    if (!root) return;
    const ranges = matches
      .map(([start, end]) => textRange(root, start, end))
      .filter((range): range is Range => Boolean(range));
    paint("kdocs-search", ranges);
    paint("kdocs-active", ranges.length ? [ranges[match % ranges.length]] : []);
    if (ranges.length && interacted.current)
      reveal(ranges[match % ranges.length]);
    onPosition({ scroll: root.scrollTop, query, match });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matches, match]);

  useEffect(() => {
    if (!target || !contentRef.current) return;
    const root = contentRef.current;
    const value = root.textContent || "";
    let range: Range | null = null;
    if (
      target.start !== undefined &&
      target.end !== undefined &&
      value.slice(target.start, target.end).trim() === target.text.trim()
    ) {
      range = textRange(root, target.start, target.end);
    } else {
      const candidates = findTextMatches(value, target.text);
      if (candidates.length === 1) range = textRange(root, ...candidates[0]);
    }
    if (!range) {
      onError("无法唯一定位此摘录；请通过文内搜索核对原文位置。");
      return;
    }
    paint("kdocs-active", [range]);
    reveal(range);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, text, rendered]);

  return (
    <>
      <div
        className={styles.readingTools}
        onKeyDown={(event) => {
          if (event.key === "Enter" && matches.length) {
            event.preventDefault();
            interacted.current = true;
            setMatch(
              (value) =>
                (value + (event.shiftKey ? matches.length - 1 : 1)) %
                matches.length,
            );
          }
          if (event.key === "Escape") {
            event.stopPropagation();
            setQuery("");
          }
        }}
      >
        <input
          ref={searchRef}
          aria-label="文内搜索"
          placeholder="搜索已读取正文"
          value={query}
          onChange={(event) => {
            interacted.current = true;
            setQuery(event.target.value);
            setMatch(0);
          }}
        />
        <span role="status">
          {query
            ? `${matches.length ? (match % matches.length) + 1 : 0} / ${matches.length}`
            : ""}
        </span>
        <button
          aria-label="上一处"
          disabled={!matches.length}
          onClick={() => {
            interacted.current = true;
            setMatch((value) => (value + matches.length - 1) % matches.length);
          }}
        >
          ↑
        </button>
        <button
          aria-label="下一处"
          disabled={!matches.length}
          onClick={() => {
            interacted.current = true;
            setMatch((value) => (value + 1) % matches.length);
          }}
        >
          ↓
        </button>
        {headings.length ? (
          <select
            aria-label="章节目录"
            value=""
            onChange={(event) => {
              const heading = contentRef.current?.querySelector(
                `#${event.target.value}`,
              );
              heading?.scrollIntoView?.({ block: "start" });
            }}
          >
            <option value="">章节目录</option>
            {headings.map((heading) => (
              <option key={heading.id} value={heading.id}>
                {"　".repeat(Math.min(heading.level - 1, 3))}
                {heading.title}
              </option>
            ))}
          </select>
        ) : null}
      </div>
      <div
        className={styles.readerContent}
        ref={contentRef}
        tabIndex={0}
        onScroll={(event) =>
          onPosition({
            scroll: event.currentTarget.scrollTop,
            ...stateRef.current,
          })
        }
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "f") {
            event.preventDefault();
            searchRef.current?.focus();
          }
        }}
      >
        <ReactMarkdown
          remarkPlugins={[remarkGfm, safeLineBreaks]}
          components={{
            table: ({ children }) => (
              <div
                className={styles.tableViewport}
                tabIndex={0}
                aria-label="表格，可横向滚动"
              >
                <table>{children}</table>
              </div>
            ),
            a: ({ href, children }) => (
              <a
                href={href}
                onClick={(event) => {
                  event.preventDefault();
                  if (href)
                    void openExternalUrl(href).catch((error) =>
                      onError(String(error)),
                    );
                }}
              >
                {children}
              </a>
            ),
          }}
        >
          {text}
        </ReactMarkdown>
      </div>
    </>
  );
}
