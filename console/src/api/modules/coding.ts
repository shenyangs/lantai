import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import { request } from "../request";
import type {
  CodeCodingCapabilitiesResponse,
  CodeCodingState,
  CodeFileContentResponse,
  CodeFileSaveRequest,
  CodeFileTreeResponse,
  CodeGitBranchesResponse,
  CodeGitCheckoutRequest,
  CodeGitCommitRequest,
  CodeGitCleanRequest,
  CodeGitCommitDiffResponse,
  CodeGitCreateBranchRequest,
  CodeGitDiscardRequest,
  CodeGitDiffResponse,
  CodeGitInitRequest,
  CodeGitLogResponse,
  CodeGitMutationResponse,
  CodeGitPathRequest,
  CodeGitResetRequest,
  CodeGitRevertRequest,
  CodeProjectActivateRequest,
  CodeProjectCreateEmptyRequest,
  CodeProjectCloneRequest,
  CodeProjectListResponse,
  CodeProjectSummary,
  CodeGitStatusResponse,
  CodeTaskListResponse,
  CodeTaskRun,
  CodeTaskStartRequest,
  CodeWorkbenchStateResponse,
  CodingFileChangeEvent,
} from "../types";

export interface CodingWorkspaceParams {
  workspaceDir?: string | null;
}

export interface CodingTreeParams extends CodingWorkspaceParams {
  path?: string | null;
  depth?: number;
  cursor?: string | null;
  limit?: number;
}

export interface CodingWorkbenchStateParams extends CodingWorkspaceParams {
  chatId?: string | null;
  treePath?: string | null;
  treeDepth?: number;
  treeLimit?: number;
}

export interface CodingWatchOptions extends CodingWorkspaceParams {
  signal?: AbortSignal;
  onChange: (event: CodingFileChangeEvent) => void;
  onReady?: (event: { root_path: string; count: number }) => void;
}

function appendWorkspaceParam(
  params: URLSearchParams,
  workspaceDir?: string | null,
): URLSearchParams {
  if (workspaceDir?.trim()) {
    params.set("workspace_dir", workspaceDir.trim());
  }
  return params;
}

function codingQuery(
  workspaceDir?: string | null,
  extra?: Record<string, string | number | undefined | null>,
): string {
  const params = appendWorkspaceParam(new URLSearchParams(), workspaceDir);
  for (const [key, value] of Object.entries(extra || {})) {
    if (value !== undefined && value !== null && String(value).trim()) {
      params.set(key, String(value));
    }
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

function parseSseBlock(block: string): { event: string; data: unknown } | null {
  const lines = block.split(/\r?\n/);
  let event = "message";
  const dataLines: string[] = [];
  for (const line of lines) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}

async function streamFileChanges(options: CodingWatchOptions): Promise<void> {
  const url = getApiUrl(
    `/coding/files/watch${codingQuery(options.workspaceDir)}`,
  );
  const response = await fetch(url, {
    method: "GET",
    headers: buildAuthHeaders(),
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`Coding watcher failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\n\n/);
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      const parsed = parseSseBlock(block);
      if (!parsed) continue;
      if (parsed.event === "ready" && options.onReady) {
        options.onReady(parsed.data as { root_path: string; count: number });
      }
      if (parsed.event === "change") {
        options.onChange(parsed.data as CodingFileChangeEvent);
      }
    }
  }
}

export const codingApi = {
  getCapabilities: () =>
    request<CodeCodingCapabilitiesResponse>("/coding/capabilities"),

  getWorkbenchState: ({
    workspaceDir,
    chatId,
    treePath,
    treeDepth,
    treeLimit,
  }: CodingWorkbenchStateParams = {}) =>
    request<CodeWorkbenchStateResponse>(
      `/coding/workbench-state${codingQuery(workspaceDir, {
        chat_id: chatId,
        tree_path: treePath,
        tree_depth: treeDepth,
        tree_limit: treeLimit,
      })}`,
    ),

  getState: (chatId: string) =>
    request<CodeCodingState>(
      `/coding/state?chat_id=${encodeURIComponent(chatId)}`,
    ),

  saveState: (
    body: CodeCodingState,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeCodingState>(`/coding/state${codingQuery(workspaceDir)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  listRecentProjects: ({ workspaceDir }: CodingWorkspaceParams = {}) =>
    request<CodeProjectListResponse>(
      `/coding/projects/recent${codingQuery(workspaceDir)}`,
    ),

  activateProject: (body: CodeProjectActivateRequest) =>
    request<CodeProjectSummary>("/coding/projects/activate", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  createEmptyProject: (
    body: CodeProjectCreateEmptyRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeProjectSummary>(
      `/coding/projects/create-empty${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  listFiles: ({
    workspaceDir,
    path,
    depth,
    cursor,
    limit,
  }: CodingTreeParams = {}) =>
    request<CodeFileTreeResponse>(
      `/coding/files/tree${codingQuery(workspaceDir, {
        path,
        depth,
        cursor,
        limit,
      })}`,
    ),

  readFile: (path: string, { workspaceDir }: CodingWorkspaceParams = {}) =>
    request<CodeFileContentResponse>(
      `/coding/files/content${codingQuery(workspaceDir, { path })}`,
    ),

  downloadFile: async (
    path: string,
    { workspaceDir }: CodingWorkspaceParams = {},
  ): Promise<Blob> => {
    const response = await fetch(
      getApiUrl(`/coding/files/download${codingQuery(workspaceDir, { path })}`),
      { headers: buildAuthHeaders() },
    );
    if (!response.ok) throw new Error("无法下载该工作区文件");
    return response.blob();
  },

  saveFile: (
    path: string,
    body: CodeFileSaveRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeFileContentResponse>(
      `/coding/files/content${codingQuery(workspaceDir, { path })}`,
      {
        method: "PUT",
        body: JSON.stringify(body),
      },
    ),

  watchFiles: streamFileChanges,

  getGitStatus: ({ workspaceDir }: CodingWorkspaceParams = {}) =>
    request<CodeGitStatusResponse>(
      `/coding/git/status${codingQuery(workspaceDir)}`,
    ),

  getGitDiff: (
    path?: string | null,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitDiffResponse>(
      `/coding/git/diff${codingQuery(workspaceDir, { path })}`,
    ),

  getGitLog: ({ workspaceDir }: CodingWorkspaceParams = {}) =>
    request<CodeGitLogResponse>(`/coding/git/log${codingQuery(workspaceDir)}`),

  getGitBranches: ({ workspaceDir }: CodingWorkspaceParams = {}) =>
    request<CodeGitBranchesResponse>(
      `/coding/git/branches${codingQuery(workspaceDir)}`,
    ),

  checkoutGitBranch: (
    body: CodeGitCheckoutRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/checkout${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  createGitBranch: (
    body: CodeGitCreateBranchRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/create-branch${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  getGitCommitDiff: (
    commit: string,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitCommitDiffResponse>(
      `/coding/git/commit-diff${codingQuery(workspaceDir, { commit })}`,
    ),

  stageGitPaths: (
    body: CodeGitPathRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/stage${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  unstageGitPaths: (
    body: CodeGitPathRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/unstage${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  commitGit: (
    body: CodeGitCommitRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/commit${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  initGit: (
    body: CodeGitInitRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/init${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  discardGitPaths: (
    body: CodeGitDiscardRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/discard${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  cleanGit: (
    body: CodeGitCleanRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/clean${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  resetGitHard: (
    body: CodeGitResetRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/reset-hard${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  revertGitCommit: (
    body: CodeGitRevertRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/git/revert${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  cloneProject: (
    body: CodeProjectCloneRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeGitMutationResponse>(
      `/coding/projects/clone${codingQuery(workspaceDir)}`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  importZipProject: async (
    file: File,
    targetDir: string,
    {
      workspaceDir,
      confirmed,
      confirmationPhrase,
    }: CodingWorkspaceParams & {
      confirmed: boolean;
      confirmationPhrase: string;
    },
  ) => {
    const params = appendWorkspaceParam(new URLSearchParams(), workspaceDir);
    params.set("target_dir", targetDir);
    params.set("confirmed", confirmed ? "true" : "false");
    params.set("confirmation_phrase", confirmationPhrase);
    const body = new FormData();
    body.append("file", file);
    const response = await fetch(
      getApiUrl(`/coding/projects/import-zip?${params.toString()}`),
      {
        method: "POST",
        headers: buildAuthHeaders(),
        body,
      },
    );
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(
        `Import zip failed: ${response.status} ${response.statusText}${
          text ? ` - ${text}` : ""
        }`,
      );
    }
    return response.json() as Promise<CodeGitMutationResponse>;
  },

  startTask: (
    body: CodeTaskStartRequest,
    { workspaceDir }: CodingWorkspaceParams = {},
  ) =>
    request<CodeTaskRun>(`/coding/tasks${codingQuery(workspaceDir)}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listTasks: ({ workspaceDir }: CodingWorkspaceParams = {}) =>
    request<CodeTaskListResponse>(`/coding/tasks${codingQuery(workspaceDir)}`),

  getTask: (taskId: string) =>
    request<CodeTaskRun>(`/coding/tasks/${encodeURIComponent(taskId)}`),

  stopTask: (taskId: string) =>
    request<CodeTaskRun>(`/coding/tasks/${encodeURIComponent(taskId)}/stop`, {
      method: "POST",
    }),
};
