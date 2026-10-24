import { getApiToken } from "./config";

function isHttpHeaderValueSafe(value: string): boolean {
  try {
    new Headers({ "X-Lantai-Header-Probe": value });
    return true;
  } catch {
    return false;
  }
}

export function encodeHeaderValue(value: string): string {
  return encodeURIComponent(value);
}

/**
 * Authorization + X-Agent-Id for API requests. Caller sets Content-Type when needed.
 *
 * Do not put workspace paths in headers: local paths may contain Unicode, and
 * browser Headers reject non-Latin-1 values before fetch can send the request.
 */
export function buildAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  let selectedAgentId = "";
  const token = getApiToken();
  if (token && isHttpHeaderValueSafe(`Bearer ${token}`)) {
    headers.Authorization = `Bearer ${token}`;
  }
  try {
    // Read from sessionStorage first (per-tab agent), fall back to localStorage
    const agentStorage =
      sessionStorage.getItem("lantai-agent-storage") ||
      localStorage.getItem("lantai-agent-storage");
    if (agentStorage) {
      const parsed = JSON.parse(agentStorage);
      const selectedAgent = parsed?.state?.selectedAgent;
      if (selectedAgent) {
        selectedAgentId = String(selectedAgent);
        headers["X-Agent-Id"] = encodeHeaderValue(selectedAgentId);
      }
    }
  } catch (error) {
    console.warn("Failed to get selected agent from storage:", error);
  }
  try {
    const rawWorkspace = sessionStorage.getItem(
      "lantai:active-topic-workspace:v1",
    );
    if (rawWorkspace) {
      const workspace = JSON.parse(rawWorkspace);
      const workspaceAgentId = String(workspace?.agentId || "").trim();
      const grantId = String(workspace?.workspaceGrantId || "").trim();
      if (
        selectedAgentId &&
        workspaceAgentId === selectedAgentId &&
        /^wsg_[A-Za-z0-9_-]{8,200}$/.test(grantId) &&
        isHttpHeaderValueSafe(grantId)
      ) {
        headers["X-Workspace-Grant-Id"] = grantId;
      }
    }
  } catch (error) {
    console.warn("Failed to get active workspace grant:", error);
  }
  return headers;
}
