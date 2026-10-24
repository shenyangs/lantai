import { useState, useEffect, useRef } from "react";
import { useAppMessage } from "../../../hooks/useAppMessage";
import api from "../../../api";
import type { ChatUpdateRequest } from "../../../api/types";
import type { Session } from "./components/constants";
import { useAgentStore } from "../../../stores/agentStore";
import { useTranslation } from "react-i18next";
import {
  createAsyncStateScope,
  createLatestRequestGate,
} from "../../../utils/latestRequest";

export function useSessions() {
  const { t } = useTranslation();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const { selectedAgent } = useAgentStore();
  const { message } = useAppMessage();
  const sessionsLoadGateRef = useRef(createLatestRequestGate());
  const mutationScopeRef = useRef(createAsyncStateScope());

  const fetchSessions = async () => {
    const ownsState = sessionsLoadGateRef.current.begin();
    setLoading(true);
    try {
      const data = await api.listSessions();
      if (ownsState() && data) {
        setSessions(data as Session[]);
      }
    } catch (error) {
      if (!ownsState()) return;
      console.error("❌ Failed to load sessions:", error);
    } finally {
      if (ownsState()) setLoading(false);
    }
  };

  useEffect(() => {
    setSessions([]);
    void fetchSessions();
    return () => {
      sessionsLoadGateRef.current.invalidate();
      mutationScopeRef.current.invalidate();
    };
  }, [selectedAgent]);

  const updateSession = async (
    sessionId: string,
    values: ChatUpdateRequest,
  ) => {
    const ownsState = mutationScopeRef.current.capture();
    try {
      const result = await api.updateSession(sessionId, values);
      if (!ownsState()) return true;
      setSessions((current) =>
        current.map((session) =>
          session.id === sessionId ? result : session,
        ),
      );
      message.success(t("sessions.saveSuccess"));
      return true;
    } catch (error) {
      if (!ownsState()) return false;
      console.error("❌ Failed to save session:", error);
      message.error(t("sessions.saveFailed"));
      return false;
    }
  };

  const deleteSession = async (sessionId: string) => {
    const ownsState = mutationScopeRef.current.capture();
    try {
      await api.deleteSession(sessionId);
      if (!ownsState()) return true;
      setSessions((current) =>
        current.filter((session) => session.id !== sessionId),
      );
      message.success(t("sessions.deleteSuccess"));
      return true;
    } catch (error) {
      if (!ownsState()) return false;
      console.error("❌ Failed to delete session:", error);
      message.error(t("sessions.deleteFailed"));
      return false;
    }
  };

  const batchDeleteSessions = async (sessionIds: string[]) => {
    const ownsState = mutationScopeRef.current.capture();
    try {
      await api.batchDeleteSessions(sessionIds);
      if (!ownsState()) return true;
      setSessions((current) =>
        current.filter((session) => !sessionIds.includes(session.id)),
      );
      message.success(
        t("sessions.batchDeleteSuccess", { count: sessionIds.length }),
      );
      return true;
    } catch (error) {
      if (!ownsState()) return false;
      console.error("❌ Failed to batch delete sessions:", error);
      message.error(t("sessions.batchDeleteFailed"));
      return false;
    }
  };

  return {
    sessions,
    loading,
    updateSession,
    deleteSession,
    batchDeleteSessions,
  };
}
