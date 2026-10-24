import type { RuntimeRunSummary } from "../../../api/types/runtime";

export function selectRequestedRuntimeRun(params: {
  requestedRunId: string;
  runtimeRuns: RuntimeRunSummary[];
  runtimeRunsAvailable: boolean;
  loading: boolean;
  runtimeDrawerOpen: boolean;
  selectedRunId?: string | null;
}): RuntimeRunSummary | null {
  const {
    requestedRunId,
    runtimeRuns,
    runtimeRunsAvailable,
    loading,
    runtimeDrawerOpen,
    selectedRunId,
  } = params;

  if (
    !requestedRunId ||
    loading ||
    !runtimeRunsAvailable ||
    runtimeRuns.length === 0
  ) {
    return null;
  }
  if (runtimeDrawerOpen && selectedRunId === requestedRunId) {
    return null;
  }
  return runtimeRuns.find((run) => run.id === requestedRunId) || null;
}
