import { request } from "../request";
import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";

export type EvidenceSpan = { source_version_id: string; start: number; end: number; quote: string };
export type ReportClaim = { start: number; end: number; evidence: EvidenceSpan[] };
export type ResearchBudget = { reading_chars: number; review_calls: number; review_seconds: number; model_calls?: number; model_tokens?: number; model_seconds?: number };
export type ResearchObservedRange = { min: number | null; max: number | null; known_samples: number };
export type ResearchEstimateResult = {
  schema_version: number; fingerprint: string; generated_at: number;
  binding: { project_id: string; revision: number; report_id: string | null; source_versions: string[] };
  reading: { status: "indexed" | "partial" | "unknown"; remaining_blocks: number; remaining_chars: number;
    additional_budget_chars: number; budget_remaining_chars: number; fits_budget: boolean;
    incomplete_sources: string[]; stale_sources: string[] };
  workflow: { status: "unknown"; reason: string; model_calls: null; tokens: null; seconds: null; cost_cny: null };
  historical_scope: string; excluded_incomplete_calls: number; exhausted_budgets: string[];
  historical_calls: { config_hash: string; surface: string; provider: string; model: string; samples: number;
    tokens: ResearchObservedRange; seconds: ResearchObservedRange; cost_cny: ResearchObservedRange }[];
};
export type ResearchProject = {
  local_ocr?: boolean;
  query_preview_required?: boolean;
  inference?: { calls: number; known_tokens: number; unknown_token_calls: number; known_cost_cny: number | null; unknown_cost_calls: number; seconds: number; surfaces: string[]; configurations: { version: string; provider: string; model: string; generation?: Record<string, unknown>; dimensions?: number; max_length?: number; use_dimensions?: boolean }[] };
  subquestions?: string[]; outline?: string[];
  budget?: ResearchBudget; budget_usage?: ResearchBudget; budget_remaining?: ResearchBudget;
  project_id: string; revision: number; question: string; scope: string; exclusions: string;
  policy: "local_sources" | "public_search" | "offline"; source_paths: string[]; source_versions: string[];
  source_levels?: Record<string, "fulltext" | "abstract" | "excerpt">;
  archived: boolean;
  report_id: string | null; review_id: string | null; review_status: string; stale_sources: string[]; stale_bibliography: string[];
  reading: { read_blocks: number; total_blocks: number; incomplete_sources: string[] };
};
export type ResearchSource = {
  source_kind?: "user_note" | "model_summary" | "imported_note";
  id: string; path: string; text?: string; text_hash: string; raw_hash: string; coverage: string;
  evidence_level: string; issues: string[]; snapshot_path: string; work_id: string | null;
  blocks: { index: number; page: number | null; start: number; end: number }[];
  pages: { page: number | null; start: number; end: number; issue: string; text_origin?: string; layout_status?: string; regions?: { start: number; end: number; x: number; y: number; width: number; height: number; confidence: number | null }[] }[];
};
export type ResearchPagePreview = { source_version_id: string; raw_hash: string; page: number; width: number; height: number; png_base64: string; overlay_allowed: boolean; overlay_reason: string };
export type ResearchPageTables = {
  source_version_id: string; raw_hash: string; page: number;
  status: "needs_visual_review" | "no_ruled_tables" | "unavailable" | "size_limit";
  page_text: string;
  tables: { bbox: number[]; rows: ({ text: string; bbox: number[] } | null)[][]; header_status: "unreviewed"; footnote_status: "unresolved" }[];
};
export type ResearchReport = {
  id: string; revision: number; text: string; text_hash: string; claims: ReportClaim[]; created_at: number;
};
export type ReviewClaim = {
  start: number; end: number; text: string; assessment: string; reason: string; evidence_indices: number[];
  importance?: "critical" | "normal"; disposition?: "accepted" | "disclosed" | "unresolved";
  disclosure?: { start: number; end: number; text: string };
};
export type ResearchReview = {
  id: string; status: string; report_hash: string; coverage: [number, number][]; issues: string[];
  identity: { provider: string; model: string };
  claims: ReviewClaim[];
  critical_review?: { required_claims: number; claims: ReviewClaim[]; checkpoint_ids: string[] };
};
export type HumanReview = {
  id: string; report_id: string; report_hash: string; review_id: string | null; revision: number;
  text: string; verdict: "agree" | "disagree" | "question"; created_at: number;
  anchor: { start: number; end: number; text: string } | null;
};
export type ResearchDetail = {
  project: ResearchProject; sources: ResearchSource[]; report: ResearchReport | null;
  human_reviews?: HumanReview[];
  review: ResearchReview | null; report_history: Omit<ResearchReport, "text" | "claims">[];
};
export type LibraryOrganization = { revision: number; tags: string[]; collections: string[] };
export type PdfRegionAnchor = { source_version_id: string; source_hash: string; page: number; coordinate_system: "pdf_raster_normalized_top_left_v1"; x: number; y: number; width: number; height: number };
export type NoteAnchor = { source_version_id: string; quote: string; start?: number; end?: number };
export type LibraryWork = {
  organization?: LibraryOrganization;
  work_id: string; selected_version_id: string; csl: Record<string, unknown> & { title: string; DOI?: string; abstract?: string };
  versions: { id: string; metadata_hash: string; csl: Record<string, unknown> & { title: string } }[];
  origins: { provider: string; version_id: string; url?: string }[];
  quality_issues: string[]; evidence_level: string; fulltext_read: boolean;
  annotations: { id: string; text: string; version_id?: string; region_anchor?: PdfRegionAnchor & { verified_against_local_source: false }; anchor?: NoteAnchor & { source_hash: string; pages: (number | null)[] }; origin: { provider: string; external_anchor?: { page_label?: string; attachment_version?: number; quote?: string; verified_against_local_source?: boolean } }; annotation_type: string }[];
  source_versions: { source: string; work: string; version: string }[];
};
export type QueryPreview = { id: string; query: string; revision: number; limit: number; status: string; result_id?: string; requests: Record<string, { endpoint: string; params: Record<string, string | number>; version: string }> };
export type LiteratureResult = {
  query?: string; requests?: QueryPreview["requests"];
  id: string; status: string; sources: Record<string, { status: string; reason?: string; http_status?: number; count?: number; cache?: string; negative_cache?: boolean; retrieved_at?: string; retry_at?: number }>;
  papers: { title: string; doi: string; year: number | null; authors: string[]; quality_issues: string[];
    records: { source: string; title: string; url?: string; open_access?: boolean }[] }[];
};
const post = <T>(path: string, body: unknown) => request<T>(`/workspace/research${path}`, {
  method: "POST", body: JSON.stringify(body),
});
export const researchApi = {
  humanReview: (id: string, body: { revision: number; report_id: string; expected_review_id: string | null;
    text: string; verdict: HumanReview["verdict"]; anchor: { text: string } | null; idempotency_key: string }) =>
    post<HumanReview>(`/projects/${encodeURIComponent(id)}/human-reviews`, body),
  exportReport: (id: string, revision: number, report_id: string, format: string, style: string) => post<{ id: string; filename: string; status: string }>(`/projects/${encodeURIComponent(id)}/exports`, { revision, report_id, format, style, idempotency_key: crypto.randomUUID() }),
  exportFile: async (id: string) => {
    const response = await fetch(getApiUrl(`/workspace/research/exports/${encodeURIComponent(id)}/file`), { headers: buildAuthHeaders() });
    if (!response.ok) throw new Error(`导出文件无法下载（${response.status}），请刷新项目后重试`);
    return response.blob();
  },
  projects: (archived = false) => request<{ items: ResearchProject[]; pending_deletions: { project_id: string; revision: number }[] }>(`/workspace/research/projects?archived=${archived}`),
  archive: (p: ResearchProject) => post<ResearchProject>(`/projects/${encodeURIComponent(p.project_id)}/archive`, { expected_revision: p.revision }),
  restore: (p: ResearchProject) => post<ResearchProject>(`/projects/${encodeURIComponent(p.project_id)}/restore`, { expected_revision: p.revision }),
  delete: (p: { project_id: string; revision: number }) => post<{ status: string }>(`/projects/${encodeURIComponent(p.project_id)}/delete`, { expected_revision: p.revision }),
  project: (id: string) => request<ResearchDetail>(`/workspace/research/projects/${encodeURIComponent(id)}`),
  estimate: (id: string, revision: number) => request<ResearchEstimateResult>(`/workspace/research/projects/${encodeURIComponent(id)}/estimate?revision=${revision}`),
  create: (body: { local_ocr?: boolean; query_preview_required?: boolean; subquestions?: string[]; outline?: string[]; budget?: ResearchBudget; question: string; source_paths: string[]; source_levels?: ResearchProject["source_levels"]; policy: ResearchProject["policy"]; scope: string; exclusions: string; idempotency_key: string }) => post<ResearchProject>("/projects", body),
  revise: (id: string, body: { local_ocr?: boolean; query_preview_required?: boolean; subquestions?: string[]; outline?: string[]; budget?: ResearchBudget; expected_revision: number; question?: string; scope?: string; exclusions?: string; source_paths?: string[]; source_levels?: ResearchProject["source_levels"]; refresh_sources?: boolean; idempotency_key: string }) => post<ResearchProject>(`/projects/${encodeURIComponent(id)}/revisions`, body),
  adoptBibliography: (p: ResearchProject) => post<ResearchProject>(`/projects/${encodeURIComponent(p.project_id)}/bibliography`, { expected_revision: p.revision, idempotency_key: crypto.randomUUID() }),
  uploadFigure: (id: string, revision: number, data: string) => post<{ id: string; sha256: string }>(`/projects/${encodeURIComponent(id)}/figures`, { revision, data, idempotency_key: crypto.randomUUID() }),
  save: (id: string, body: { revision: number; text: string; claims: ReportClaim[]; expected_report_id: string | null; idempotency_key: string }) => post<ResearchReport>(`/projects/${encodeURIComponent(id)}/reports`, {
    ...body,
    // Retrieved evidence includes read-only locations; the write schema only accepts anchors.
    claims: body.claims.map(({ start, end, evidence }) => ({
      start, end,
      evidence: evidence.map(({ source_version_id, start, end, quote }) => ({ source_version_id, start, end, quote })),
    })),
  }),
  sourcePage: (id: string, page: number) => request<ResearchPagePreview>(`/workspace/research/sources/${encodeURIComponent(id)}/pages/${page}`),
  sourceTables: (id: string, page: number) => request<ResearchPageTables>(`/workspace/research/sources/${encodeURIComponent(id)}/pages/${page}/tables`),
  source: (id: string) => request<ResearchSource>(`/workspace/research/sources/${encodeURIComponent(id)}`),
  report: (id: string) => request<ResearchReport>(`/workspace/research/reports/${encodeURIComponent(id)}`),
  read: (id: string, revision: number, source: string, first = 0) => post<{ blocks: { index: number; page: number | null; start: number; end: number; text: string }[]; total_blocks: number }>(`/projects/${encodeURIComponent(id)}/read`, {
    revision, source_version_id: source, first_block: first, block_count: 1,
  }),
  library: () => request<{ items: LibraryWork[] }>("/workspace/research/library"),
  work: (id: string) => request<LibraryWork>(`/workspace/research/library/works/${encodeURIComponent(id)}`),
  importLibrary: (format: string, text: string) => post<{ status: string; items: { work_id: string; conflict: boolean; duplicate: boolean }[] }>("/library/import", { format, text }),
  zotero: () => post<{ status: string; message?: string; warnings?: string[]; items?: unknown[] }>("/library/zotero", {}),
  search: (project: ResearchProject, query: string, options: { query_mode: "title_author"; author: string } | undefined = undefined) => post<LiteratureResult>("/library/search", { project_id: project.project_id, revision: project.revision, query, limit: 8, ...options }),
  queryPreviews: (id: string) => request<{ items: QueryPreview[] }>(`/workspace/research/projects/${encodeURIComponent(id)}/query-previews`),
  decideQuery: (id: string, approve: boolean) => post<LiteratureResult>(`/library/query-previews/${encodeURIComponent(id)}/decision`, { approve }),
  searchRecord: (id: string) => request<LiteratureResult>(`/workspace/research/library/searches/${encodeURIComponent(id)}`),
  importCandidate: (search_id: string, index: number) => post<{ items: { work_id: string }[] }>("/library/candidates", { search_id, index }),
  exportLibrary: (format: string) => request<{ text: string }>(`/workspace/research/library/export/${encodeURIComponent(format)}`),
  bibliography: (work_ids: string[], style: string) => post<{ bibliography: string[]; citations: string[]; provenance: Record<string, unknown> }>("/library/bibliography", { work_ids, style }),
  chooseVersion: (work: LibraryWork, version_id: string) => post<LibraryWork>(`/library/works/${encodeURIComponent(work.work_id)}/version`, { version_id, expected_version_id: work.selected_version_id }),
  organize: (id: string, body: { expected_revision: number; tags: string[]; collections: string[] }) => post<LibraryWork>(`/library/works/${encodeURIComponent(id)}/organization`, body),
  regionNote: (id: string, text: string, region_anchor: PdfRegionAnchor) => post<unknown>(`/library/works/${encodeURIComponent(id)}/notes`, { text, region_anchor }),
  note: (id: string, text: string, anchor?: NoteAnchor) => post<unknown>(`/library/works/${encodeURIComponent(id)}/notes`, { text, anchor }),
  noteSource: (id: string, project: ResearchProject) => post<{ project: ResearchProject; source_version_id: string }>(`/library/notes/${encodeURIComponent(id)}/source`, { project_id: project.project_id, revision: project.revision, idempotency_key: crypto.randomUUID() }),
  linkSource: (id: string, source_version_id: string) => post<LibraryWork>(`/library/works/${encodeURIComponent(id)}/sources`, { source_version_id }),
  fulltext: (id: string, project: ResearchProject, location_index: number) => post<{ project: ResearchProject; work: LibraryWork }>(`/library/works/${encodeURIComponent(id)}/fulltext`, { project_id: project.project_id, revision: project.revision, location_index, idempotency_key: crypto.randomUUID() }),
};
