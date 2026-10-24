import React, { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  useChatAnywhereSessions,
  useChatAnywhereSessionsState,
} from "@/vendor/lantaiChat";
import {
  CREATE_NEW_CHAT_CONTEXT_KEY,
  CREATE_NEW_CHAT_PENDING_KEY,
  CREATE_NEW_CHAT_REQUEST_EVENT,
  CREATE_NEW_CHAT_SETTLED_EVENT,
  type CreateNewChatSettledDetail,
  type ChatKnowledgeSourceReference,
  type ChatKnowledgeScopeMode,
} from "../../chatSessionEvents";

const readPendingKnowledgeSources = (): {
  sources: ChatKnowledgeSourceReference[];
  scopeMode: ChatKnowledgeScopeMode;
} | null => {
  const raw = window.sessionStorage.getItem(CREATE_NEW_CHAT_CONTEXT_KEY);
  window.sessionStorage.removeItem(CREATE_NEW_CHAT_CONTEXT_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as {
      knowledgeSource?: Partial<ChatKnowledgeSourceReference>;
      knowledgeSources?: Partial<ChatKnowledgeSourceReference>[];
      knowledgeScopeMode?: ChatKnowledgeScopeMode;
    };
    const rawSources = Array.isArray(parsed.knowledgeSources)
      ? parsed.knowledgeSources
      : parsed.knowledgeSource
        ? [parsed.knowledgeSource]
        : [];
    const sources = rawSources
      .slice(0, 20)
      .map((source) => ({
        path: String(source?.path || "").trim(),
        filename: String(source?.filename || "").trim(),
        kind: String(source?.kind || "").trim() || undefined,
      }))
      .filter((source) => source.path && source.filename);
    if (!sources.length) return null;
    return {
      sources,
      scopeMode:
        parsed.knowledgeScopeMode === "expanded" ? "expanded" : "exact",
    };
  } catch {
    return null;
  }
};

export function resolveRouteSession<
  T extends { id?: string; realId?: string; sessionId?: string },
>(sessions: T[], chatId: string): T | undefined {
  const exact = sessions.find((session) => session.id === chatId);
  if (exact) return exact;
  const authoritative = sessions.filter((session) => session.realId === chatId);
  if (authoritative.length === 1) return authoritative[0];
  const aliases = sessions.filter((session) => session.sessionId === chatId);
  return aliases.length === 1 ? aliases[0] : undefined;
}

/**
 * URL chatId → context currentSessionId (one direction of bidirectional sync).
 *
 * Only reacts to URL or session list changes. currentSessionId is read via ref
 * to avoid triggering the effect when the context changes from the other direction
 * (context → URL via onSessionSelected), which would cause circular re-loads.
 */
const ChatSessionInitializer: React.FC = () => {
  const location = useLocation();
  const navigate = useNavigate();
  const chatId = useMemo(() => {
    const match = location.pathname.match(/^\/chat\/(.+)$/);
    return match?.[1];
  }, [location.pathname]);

  const { sessions, currentSessionId, setCurrentSessionId } =
    useChatAnywhereSessionsState();
  const { createSession } = useChatAnywhereSessions();

  const currentSessionIdRef = useRef(currentSessionId);
  currentSessionIdRef.current = currentSessionId;
  const appliedUrlSelectionRef = useRef<string | null>(null);
  const createPendingRef = useRef(false);

  useLayoutEffect(() => {
    if (!chatId) {
      appliedUrlSelectionRef.current = null;
      return;
    }
    const matching = resolveRouteSession(sessions, chatId);
    const targetSessionId = matching?.id || chatId;
    const urlSelectionKey = `${chatId}\u0000${targetSessionId}`;
    if (appliedUrlSelectionRef.current === urlSelectionKey) return;
    appliedUrlSelectionRef.current = urlSelectionKey;
    if (currentSessionIdRef.current !== targetSessionId) {
      setCurrentSessionId(targetSessionId);
    }
    // currentSessionId is read via ref to avoid circular triggers.
  }, [chatId, sessions, setCurrentSessionId]);

  useEffect(() => {
    const createFromExternalRequest = () => {
      if (window.sessionStorage.getItem(CREATE_NEW_CHAT_PENDING_KEY) !== "1") {
        return;
      }
      window.sessionStorage.removeItem(CREATE_NEW_CHAT_PENDING_KEY);
      if (createPendingRef.current) return;

      createPendingRef.current = true;
      // A blank chat is a real conversation boundary. Clear the selected
      // session synchronously, before workspace creation resolves, so the old
      // conversation can never remain painted under the new-chat route.
      if (currentSessionIdRef.current) {
        currentSessionIdRef.current = undefined;
        setCurrentSessionId(undefined);
      }
      const knowledgeSelection = readPendingKnowledgeSources();
      void createSession(
        knowledgeSelection
          ? {
              name:
                knowledgeSelection.sources.length === 1
                  ? `关于 ${knowledgeSelection.sources[0].filename}`
                  : `基于 ${knowledgeSelection.sources.length} 份资料`,
              meta: {
                knowledge_sources: knowledgeSelection.sources,
                knowledge_scope_mode: knowledgeSelection.scopeMode,
                ...(knowledgeSelection.sources.length === 1
                  ? { knowledge_source: knowledgeSelection.sources[0] }
                  : {}),
              },
            }
          : undefined,
      )
        .then(() => {
          window.dispatchEvent(
            new CustomEvent<CreateNewChatSettledDetail>(
              CREATE_NEW_CHAT_SETTLED_EVENT,
              { detail: { ok: true } },
            ),
          );
        })
        .catch(() => {
          window.dispatchEvent(
            new CustomEvent<CreateNewChatSettledDetail>(
              CREATE_NEW_CHAT_SETTLED_EVENT,
              { detail: { ok: false } },
            ),
          );
        })
        .finally(() => {
          createPendingRef.current = false;
        });
    };

    const consumePendingRequest = () => {
      createFromExternalRequest();
    };

    window.addEventListener(
      CREATE_NEW_CHAT_REQUEST_EVENT,
      createFromExternalRequest,
    );

    const searchParams = new URLSearchParams(location.search);
    if (searchParams.get("lantaiColdStart") === "1") {
      searchParams.delete("lantaiColdStart");
      window.sessionStorage.setItem(CREATE_NEW_CHAT_PENDING_KEY, "1");
      const remainingSearch = searchParams.toString();
      navigate(
        {
          pathname: location.pathname,
          search: remainingSearch ? `?${remainingSearch}` : "",
          hash: location.hash,
        },
        { replace: true },
      );
    }
    consumePendingRequest();

    return () => {
      window.removeEventListener(
        CREATE_NEW_CHAT_REQUEST_EVENT,
        createFromExternalRequest,
      );
    };
  }, [
    createSession,
    location.hash,
    location.pathname,
    location.search,
    navigate,
  ]);

  return null;
};

export default ChatSessionInitializer;
