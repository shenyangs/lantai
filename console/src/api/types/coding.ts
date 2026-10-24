export type CodingFileKind = "file" | "directory";

export interface CodeFileNode {
  name: string;
  path: string;
  kind: CodingFileKind;
  size: number;
  modified_time?: string | null;
  children: CodeFileNode[];
  has_more?: boolean;
  next_cursor?: string | null;
}

export interface CodeFileTreeResponse {
  root_path: string;
  root_name: string;
  path: string;
  children: CodeFileNode[];
  total_entries: number;
  truncated: boolean;
  max_entries: number;
  depth: number;
  limit: number;
  cursor?: string | null;
  next_cursor?: string | null;
  has_more: boolean;
}

export interface CodeFileContentResponse {
  path: string;
  content: string;
  size: number;
  modified_time: string;
  sha256: string;
  encoding: string;
  binary: boolean;
  truncated: boolean;
}

export interface CodeFileSaveRequest {
  content: string;
  expected_sha256?: string | null;
}

export interface CodeGitStatusEntry {
  status: string;
  path: string;
  original_path?: string | null;
  index_status: string;
  worktree_status: string;
  staged: boolean;
  unstaged: boolean;
  untracked: boolean;
}

export interface CodeGitStatusResponse {
  is_repo: boolean;
  branch?: string | null;
  entries: CodeGitStatusEntry[];
  summary: Record<string, number>;
  error?: string | null;
}

export interface CodeGitDiffResponse {
  is_repo: boolean;
  path?: string | null;
  unstaged: string;
  staged: string;
  truncated: boolean;
  error?: string | null;
}

export interface CodeGitLogEntry {
  hash: string;
  short_hash: string;
  author: string;
  date: string;
  subject: string;
}

export interface CodeGitLogResponse {
  is_repo: boolean;
  entries: CodeGitLogEntry[];
  error?: string | null;
}

export interface CodeGitBranchEntry {
  name: string;
  current: boolean;
  upstream?: string | null;
}

export interface CodeGitBranchesResponse {
  is_repo: boolean;
  branches: CodeGitBranchEntry[];
  current?: string | null;
  error?: string | null;
}

export interface CodeGitCommitDiffResponse {
  is_repo: boolean;
  commit?: string | null;
  diff: string;
  stat: string;
  truncated: boolean;
  error?: string | null;
}

export interface CodeGitMutationResponse {
  is_repo: boolean;
  ok: boolean;
  paths: string[];
  stdout: string;
  stderr: string;
  error?: string | null;
}

export interface CodeGitPathRequest {
  paths: string[];
}

export interface CodeGitCommitRequest {
  message: string;
  confirmed: boolean;
}

export interface CodeGitInitRequest {
  confirmed: boolean;
  initial_commit?: boolean;
  message?: string;
}

export interface CodeGitDiscardRequest {
  paths: string[];
  confirmed: boolean;
  confirmation_phrase: string;
}

export interface CodeGitCleanRequest {
  paths?: string[];
  confirmed: boolean;
  confirmation_phrase: string;
}

export interface CodeGitResetRequest {
  target?: string;
  confirmed: boolean;
  confirmation_phrase: string;
}

export interface CodeGitRevertRequest {
  commit: string;
  confirmed: boolean;
}

export interface CodeGitCheckoutRequest {
  name: string;
}

export interface CodeGitCreateBranchRequest {
  name: string;
  checkout?: boolean;
}

export interface CodeProjectCloneRequest {
  url: string;
  target_dir: string;
  confirmed: boolean;
  confirmation_phrase: string;
}

export interface CodeProjectSummary {
  workspace_dir: string;
  root_name: string;
  exists: boolean;
  is_git: boolean;
  status: string;
  chat_id?: string | null;
  updated_at?: string | null;
  workspace_grant_id?: string | null;
}

export interface CodeProjectListResponse {
  projects: CodeProjectSummary[];
}

export interface CodeProjectCreateEmptyRequest {
  target_dir: string;
}

export interface CodeProjectActivateRequest {
  workspace_dir: string;
  chat_id?: string | null;
}

export interface CodeCodingState {
  chat_id: string;
  session_id?: string | null;
  workspace_dir?: string | null;
  active_file?: string | null;
  open_tabs: string[];
  dirty_drafts: Record<string, string>;
  pending_diffs: string[];
  git_summary: Record<string, number>;
  last_run?: string | null;
  next_recovery_step?: string | null;
  updated_at?: string | null;
}

export interface CodeTaskStartRequest {
  command: string;
  confirmed: boolean;
  timeout_seconds?: number;
}

export interface CodeTaskRun {
  task_id: string;
  workspace_dir: string;
  command: string;
  status: string;
  started_at: string;
  ended_at?: string | null;
  returncode?: number | null;
  stdout_tail: string;
  stderr_tail: string;
  output_seq?: number;
  updated_at?: string | null;
  error?: string | null;
  timeout_seconds?: number;
  runtime_attempt_id?: string | null;
  runtime_revision?: number;
}

export interface CodeTaskListResponse {
  tasks: CodeTaskRun[];
}

export interface CodeWorkbenchHealth {
  status: string;
  message: string;
}

export interface CodeWorkbenchStateResponse {
  workspace_dir: string;
  workspace_name: string;
  health: CodeWorkbenchHealth;
  capabilities: CodeCodingCapabilitiesResponse;
  file_tree?: CodeFileTreeResponse | null;
  git_status?: CodeGitStatusResponse | null;
  recent_projects: CodeProjectSummary[];
  tasks: CodeTaskRun[];
  coding_state?: CodeCodingState | null;
  watch_interval_seconds: number;
}

export interface CodeCodingCapabilitiesResponse {
  distribution: string;
  file_tree: boolean;
  file_read: boolean;
  file_write: boolean;
  file_watch: boolean;
  git_read: boolean;
  git_write: boolean;
  git_dangerous_write: boolean;
  project_import: boolean;
  ast_tools: boolean;
  lsp_tools: boolean;
  external_process_tools: boolean;
  background_tasks: boolean;
  reasons: Record<string, string>;
}

export interface CodingFileChangeEvent {
  root_path: string;
  changed_paths: string[];
  count: number;
}
