import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useChatAnywhereSessionsState } from "@/vendor/lantaiChat";
import {
  planApi,
  subscribePlanUpdates,
  type PlanStateResponse,
} from "../../api/modules/plan";

export interface UsePlanStateOptions {
  active?: boolean;
  pollIntervalMs?: number;
}

export interface UsePlanStateResult {
  plan: PlanStateResponse | null;
  loading: boolean;
  doneCount: number;
  totalCount: number;
  percent: number;
  refresh: () => Promise<void>;
}

export function getBackendSessionId(): string {
  return (window as any).currentSessionId || "";
}

export function usePlanState({
  active = true,
  pollIntervalMs = 30000,
}: UsePlanStateOptions = {}): UsePlanStateResult {
  const { currentSessionId } = useChatAnywhereSessionsState();
  const [plan, setPlan] = useState<PlanStateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const ssePlanRef = useRef<PlanStateResponse | null>(null);
  const activeRef = useRef(active);
  const sessionIdRef = useRef(currentSessionId);
  const refreshInFlightRef = useRef<{
    sessionId: string;
    token: symbol;
    promise: Promise<void>;
  } | null>(null);

  useEffect(() => {
    activeRef.current = active;
  }, [active]);

  const refresh = useCallback(async () => {
    if (!activeRef.current) return;
    const sid = getBackendSessionId();
    if (refreshInFlightRef.current?.sessionId === sid) {
      return refreshInFlightRef.current.promise;
    }

    const requestToken = Symbol(sid || "draft");
    const request = (async () => {
      setLoading(true);
      try {
        const data = await planApi.getCurrentPlan(sid || undefined);
        if (getBackendSessionId() !== sid) return;
        if (ssePlanRef.current !== null && data === null) return;
        setPlan(data);
      } catch {
        // Keep the previous plan snapshot; this panel is supportive UI.
      } finally {
        if (refreshInFlightRef.current?.token === requestToken) {
          setLoading(false);
        }
      }
    })();
    refreshInFlightRef.current = {
      sessionId: sid,
      token: requestToken,
      promise: request,
    };
    try {
      await request;
    } finally {
      if (refreshInFlightRef.current?.token === requestToken) {
        refreshInFlightRef.current = null;
      }
    }
  }, [currentSessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!active) {
      ssePlanRef.current = null;
      setLoading(false);
      return;
    }

    const sessionChanged = sessionIdRef.current !== currentSessionId;
    sessionIdRef.current = currentSessionId;
    ssePlanRef.current = null;
    if (sessionChanged) setPlan(null);
    void refresh();
  }, [active, currentSessionId, refresh]);

  useEffect(() => {
    if (!active) return;

    const unsubscribe = subscribePlanUpdates((updatedPlan, eventSessionId) => {
      const mySid = getBackendSessionId();
      if (eventSessionId && eventSessionId !== mySid) return;
      ssePlanRef.current = updatedPlan;
      setPlan(updatedPlan);
    });

    return unsubscribe;
  }, [active]);

  useEffect(() => {
    if (!active || pollIntervalMs <= 0) return;
    let disposed = false;
    let timer: number | null = null;
    const poll = async () => {
      await refresh();
      if (!disposed) {
        timer = window.setTimeout(() => void poll(), pollIntervalMs);
      }
    };
    timer = window.setTimeout(() => void poll(), pollIntervalMs);
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [active, pollIntervalMs, refresh]);

  const counts = useMemo(() => {
    const done =
      plan?.subtasks.filter((task) => task.state === "done").length ?? 0;
    const total = plan?.subtasks.length ?? 0;
    const percent = total > 0 ? Math.round((done / total) * 100) : 0;
    return { done, total, percent };
  }, [plan?.subtasks]);

  return {
    plan,
    loading,
    doneCount: counts.done,
    totalCount: counts.total,
    percent: counts.percent,
    refresh,
  };
}
