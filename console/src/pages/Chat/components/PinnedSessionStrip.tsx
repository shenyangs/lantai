import { useMemo } from "react";
import {
  useChatAnywhereSessionsState,
  type ILantaiChatSession,
} from "@/vendor/lantaiChat";
import sessionApi from "../sessionApi";
import {
  getSessionStatus,
  getSessionTitle,
  type SessionPresentationSource,
} from "./ChatSessionDrawer/presentation";
import styles from "./PinnedSessionStrip.module.less";

type PinnedSession = ILantaiChatSession & SessionPresentationSource;

interface PinnedSessionStripProps {
  onOpenAll: () => void;
}

export default function PinnedSessionStrip({
  onOpenAll,
}: PinnedSessionStripProps) {
  const { sessions, currentSessionId, setCurrentSessionId } =
    useChatAnywhereSessionsState();
  const pinnedSessions = useMemo(
    () =>
      (sessions as PinnedSession[])
        .filter((session) => session.pinned && !session.archived)
        .slice(0, 4),
    [sessions],
  );
  if (pinnedSessions.length === 0) return null;

  return (
    <nav className={styles.strip} aria-label="置顶会话快速切换">
      {pinnedSessions.map((session) => {
        const title = getSessionTitle(session, "新会话");
        const status = getSessionStatus(session);
        return (
          <button
            key={session.id}
            type="button"
            className={styles.session}
            aria-current={session.id === currentSessionId ? "page" : undefined}
            aria-label={`切换到 ${title}，${status.label}`}
            title={`${title} · ${status.label}`}
            onClick={() => {
              sessionApi.selectSession(session.id);
              setCurrentSessionId(session.id);
            }}
          >
            <span
              className={styles.status}
              data-tone={status.tone}
              aria-hidden="true"
            />
            <span>{title}</span>
          </button>
        );
      })}
      {(sessions as PinnedSession[]).filter(
        (session) => session.pinned && !session.archived,
      ).length > 4 ? (
        <button type="button" className={styles.more} onClick={onOpenAll}>
          查看全部
        </button>
      ) : null}
    </nav>
  );
}
