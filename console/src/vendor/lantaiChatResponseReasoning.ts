import React from "react";

function textFromContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((item) => {
      if (!item || typeof item !== "object") return "";
      const record = item as Record<string, unknown>;
      return String(record.text || record.thinking || "");
    })
    .join("");
}

export default function LantaiResponseReasoning({
  data,
}: {
  data: Record<string, unknown>;
}) {
  const status = typeof data?.status === "string" ? data.status : "";
  const isLive = ["created", "in_progress", "running"].includes(status);
  const [expanded, setExpanded] = React.useState(false);
  const previousIsLiveRef = React.useRef(isLive);
  React.useLayoutEffect(() => {
    if (previousIsLiveRef.current !== isLive) {
      setExpanded(false);
      previousIsLiveRef.current = isLive;
    }
  }, [isLive]);
  // The Thinking disclosure is an evidence surface, not a summary. Preserve
  // every reasoning character supplied by the runtime; collapsing affects
  // presentation only and never rewrites, redacts, or truncates the content.
  const text = textFromContent(data?.content);
  const open = expanded;
  const viewState = expanded
    ? "expanded"
    : isLive
      ? "live-collapsed"
      : "completed-collapsed";
  const characterCount = text.length.toLocaleString("zh-CN");
  return React.createElement(
    "details",
    {
      className: "lantai-reasoning lantai-thinking lantai-process-summary",
      open,
      onToggle: (event: React.SyntheticEvent<HTMLDetailsElement>) => {
        // Chromium owns the native `open` property and can mutate it outside
        // React's state (for example during a synthetic or accessibility
        // activation). Keep the disclosure truly controlled so the DOM,
        // styling, and action label cannot disagree about whether private
        // reasoning is expanded.
        if (event.currentTarget.open !== expanded) {
          event.currentTarget.open = expanded;
        }
      },
      "data-chat-thinking": "1",
      "data-status": status || undefined,
      "data-view-state": viewState,
    },
    React.createElement(
      "summary",
      {
        onClick: (event: React.MouseEvent<HTMLElement>) => {
          event.preventDefault();
          setExpanded((value) => !value);
        },
      },
      React.createElement("span", null, "思考"),
      React.createElement(
        "span",
        { className: "lantai-reasoning-meta" },
        isLive ? `正在思考 · ${characterCount} 字` : `${characterCount} 字`,
      ),
      React.createElement(
        "span",
        { className: "lantai-reasoning-action" },
        expanded ? "收起" : "展开",
      ),
    ),
    React.createElement(
      "div",
      {
        className: "lantai-reasoning-content",
        "data-chat-scroll-chain": "1",
      },
      text,
    ),
  );
}
