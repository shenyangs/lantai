import {
  useCallback,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type RefObject,
} from "react";

export interface ConversationNavigationMessage {
  id: string;
  role?: string;
  cards?: Array<{ code?: string; data?: any }>;
  createdAt?: string | number | null;
  created_at?: string | number | null;
  content?: unknown;
}

export interface ConversationTurn {
  id: string;
  anchorMessageId: string;
  messageIds: string[];
  preview: string;
  timestamp?: string | number | null;
}

export interface ConversationTurnMetric {
  messageId: string;
  top: number;
}

const MIN_NAVIGATOR_TURNS = 6;
const MIN_NAVIGATOR_OVERFLOW_PX = 320;
const PREVIEW_MAX_LENGTH = 88;
const NAVIGATOR_MARKER_GAP_PX = 12;
const NAVIGATOR_RAIL_END_PADDING_PX = 16;

function flattenTextContent(content: unknown): string[] {
  if (typeof content === "string") return [content];
  if (!Array.isArray(content)) return [];
  return content.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    if (typeof record.text === "string" && record.text.trim()) {
      return [record.text];
    }
    const attachmentName =
      record.file_name || record.fileName || record.filename || record.name;
    return typeof attachmentName === "string" && attachmentName.trim()
      ? [attachmentName]
      : [];
  });
}

function messagePreview(message: ConversationNavigationMessage): string {
  const cardText = (message.cards || []).flatMap((card) => {
    const input = Array.isArray(card?.data?.input) ? card.data.input : [];
    return input.flatMap((item: unknown) => {
      if (!item || typeof item !== "object") return [];
      return flattenTextContent((item as Record<string, unknown>).content);
    });
  });
  const text = [...cardText, ...flattenTextContent(message.content)]
    .join(" ")
    .replace(/\u200b/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return "未命名对话";
  return text.length > PREVIEW_MAX_LENGTH
    ? `${text.slice(0, PREVIEW_MAX_LENGTH - 1).trimEnd()}…`
    : text;
}

export function buildConversationTurns(
  messages: ConversationNavigationMessage[],
): ConversationTurn[] {
  const turns: ConversationTurn[] = [];

  for (const message of messages) {
    const messageId = String(message.id || "");
    if (!messageId) continue;
    if (message.role === "user") {
      turns.push({
        id: messageId,
        anchorMessageId: messageId,
        messageIds: [messageId],
        preview: messagePreview(message),
        timestamp: message.createdAt ?? message.created_at,
      });
      continue;
    }
    turns[turns.length - 1]?.messageIds.push(messageId);
  }

  return turns;
}

export function shouldShowConversationNavigator(
  turnCount: number,
  scrollHeight: number,
  clientHeight: number,
): boolean {
  return (
    turnCount >= MIN_NAVIGATOR_TURNS &&
    scrollHeight - clientHeight >= MIN_NAVIGATOR_OVERFLOW_PX
  );
}

export function resolveActiveConversationTurn(
  turns: ConversationTurn[],
  metrics: ConversationTurnMetric[],
  readingLine: number,
): string | null {
  if (turns.length === 0) return null;
  const topByMessageId = new Map(
    metrics.map((metric) => [metric.messageId, metric.top]),
  );
  let active = turns[0]?.id || null;
  for (const turn of turns) {
    const top = topByMessageId.get(turn.anchorMessageId);
    if (top == null) continue;
    if (top > readingLine) break;
    active = turn.id;
  }
  return active;
}

export function resolveConversationMarkerProximity(
  index: number,
  interactionIndex: number | null,
): "0" | "1" | "2" | "3" | "far" {
  if (interactionIndex == null) return "far";

  const distance = Math.abs(index - interactionIndex);
  return distance > 3 ? "far" : (String(distance) as "0" | "1" | "2" | "3");
}

function findMessageNode(
  list: HTMLDivElement,
  messageId: string,
): HTMLElement | null {
  return (
    Array.from(
      list.querySelectorAll<HTMLElement>("[data-lantai-message-id]"),
    ).find((node) => node.dataset.lantaiMessageId === messageId) || null
  );
}

export function scrollConversationToMessage(
  list: HTMLDivElement,
  messageId: string,
  options: { behavior?: ScrollBehavior } = {},
): { found: boolean; nearEnd: boolean; top?: number } {
  const message = findMessageNode(list, messageId);
  if (!message) return { found: false, nearEnd: false };

  const listRect = list.getBoundingClientRect();
  const messageRect = message.getBoundingClientRect();
  const readingInset = Math.min(120, Math.max(56, list.clientHeight * 0.2));
  const maxScrollTop = Math.max(0, list.scrollHeight - list.clientHeight);
  const top = Math.min(
    maxScrollTop,
    Math.max(0, list.scrollTop + messageRect.top - listRect.top - readingInset),
  );
  const nearEnd = maxScrollTop - top <= 24;
  if (typeof list.scrollTo === "function") {
    list.scrollTo({
      top,
      behavior: options.behavior || "smooth",
    });
  } else {
    list.scrollTop = top;
  }
  return { found: true, nearEnd, top };
}

function formatTurnTime(value: string | number | null | undefined): string {
  if (value == null || value === "") return "";
  const numeric =
    typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value;
  const date = new Date(numeric);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

interface ConversationNavigatorProps {
  messages: ConversationNavigationMessage[];
  scrollContainerRef: RefObject<HTMLDivElement | null>;
  onNavigate: (messageId: string) => void;
}

export function ConversationNavigator({
  messages,
  scrollContainerRef,
  onNavigate,
}: ConversationNavigatorProps) {
  const turns = useMemo(() => buildConversationTurns(messages), [messages]);
  const [visible, setVisible] = useState(false);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(
    turns[0]?.id || null,
  );
  const [interactionIndex, setInteractionIndex] = useState<number | null>(null);
  const markerRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const updateFromScroll = useCallback(() => {
    const list = scrollContainerRef.current;
    if (!list) return;
    if (turns.length < MIN_NAVIGATOR_TURNS) {
      setVisible(false);
      return;
    }
    const listRect = list.getBoundingClientRect();
    const metrics = turns.flatMap<ConversationTurnMetric>((turn) => {
      const node = findMessageNode(list, turn.anchorMessageId);
      const nodeRect = node?.getBoundingClientRect();
      return node
        ? [
            {
              messageId: turn.anchorMessageId,
              top: list.scrollTop + (nodeRect?.top || 0) - listRect.top,
            },
          ]
        : [];
    });
    setVisible(
      shouldShowConversationNavigator(
        turns.length,
        list.scrollHeight,
        list.clientHeight,
      ),
    );
    setActiveTurnId(
      resolveActiveConversationTurn(
        turns,
        metrics,
        list.scrollTop + list.clientHeight * 0.35,
      ),
    );
  }, [scrollContainerRef, turns]);

  useLayoutEffect(() => {
    const list = scrollContainerRef.current;
    if (!list) return;
    if (turns.length < MIN_NAVIGATOR_TURNS) {
      setVisible((current) => (current ? false : current));
      const nextActiveTurnId = turns[0]?.id || null;
      setActiveTurnId((current) =>
        current === nextActiveTurnId ? current : nextActiveTurnId,
      );
      return;
    }
    let frame = 0;
    const scheduleUpdate = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(updateFromScroll);
    };
    updateFromScroll();
    list.addEventListener("scroll", scheduleUpdate, { passive: true });
    const content = list.querySelector<HTMLElement>(".lantai-bubble-list");
    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(scheduleUpdate);
    resizeObserver?.observe(list);
    if (content) resizeObserver?.observe(content);
    return () => {
      window.cancelAnimationFrame(frame);
      list.removeEventListener("scroll", scheduleUpdate);
      resizeObserver?.disconnect();
    };
  }, [scrollContainerRef, turns, updateFromScroll]);

  const handleMarkerKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let nextIndex: number | null = null;
    if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      nextIndex = Math.max(0, index - 1);
    } else if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      nextIndex = Math.min(turns.length - 1, index + 1);
    } else if (event.key === "Home") {
      nextIndex = 0;
    } else if (event.key === "End") {
      nextIndex = turns.length - 1;
    }
    if (nextIndex == null) return;
    event.preventDefault();
    markerRefs.current[nextIndex]?.focus();
  };

  const railHeight =
    Math.max(0, turns.length - 1) * NAVIGATOR_MARKER_GAP_PX +
    NAVIGATOR_RAIL_END_PADDING_PX;

  return (
    <nav
      className="lantai-conversation-navigator"
      aria-label="会话定位"
      aria-hidden={visible ? undefined : "true"}
      data-visible={visible ? "true" : "false"}
    >
      {visible ? (
        <div
          className="lantai-conversation-navigator-markers"
          style={
            {
              "--lt-conversation-rail-height": `${railHeight}px`,
            } as CSSProperties
          }
        >
          {turns.map((turn, index) => {
            const active = turn.id === activeTurnId;
            const time = formatTurnTime(turn.timestamp);
            const style = {
              "--lt-conversation-marker-position": `${
                (index / Math.max(1, turns.length - 1)) * 100
              }%`,
            } as CSSProperties;
            return (
              <button
                key={turn.id}
                ref={(node) => {
                  markerRefs.current[index] = node;
                }}
                type="button"
                className="lantai-conversation-navigator-marker"
                style={style}
                data-active={active ? "true" : "false"}
                data-proximity={resolveConversationMarkerProximity(
                  index,
                  interactionIndex,
                )}
                data-edge={
                  index === 0
                    ? "start"
                    : index === turns.length - 1
                      ? "end"
                      : undefined
                }
                aria-current={active ? "location" : undefined}
                aria-label={`第 ${index + 1} 轮：${turn.preview}`}
                tabIndex={active ? 0 : -1}
                onClick={() => onNavigate(turn.anchorMessageId)}
                onKeyDown={(event) => handleMarkerKeyDown(event, index)}
                onMouseEnter={() => setInteractionIndex(index)}
                onMouseLeave={() => setInteractionIndex(null)}
                onFocus={() => setInteractionIndex(index)}
                onBlur={() => setInteractionIndex(null)}
              >
                <span className="lantai-conversation-navigator-tick" />
                <span
                  className="lantai-conversation-navigator-preview"
                  role="tooltip"
                >
                  {time ? (
                    <time className="lantai-conversation-navigator-time">
                      {time}
                    </time>
                  ) : null}
                  <span className="lantai-conversation-navigator-summary">
                    {turn.preview}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      ) : null}
    </nav>
  );
}
