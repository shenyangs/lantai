export type KdocsDocumentView =
  | "recent"
  | "starred"
  | "my"
  | "shared"
  | "folder"
  | "search";

export interface KdocsDocumentActor {
  id: string;
  name: string;
}

export interface KdocsDocumentItem {
  key: string;
  drive_id: string;
  file_id: string;
  parent_id: string;
  name: string;
  type: string;
  suffix: string;
  is_folder: boolean;
  size: number | null;
  created_at?: string | number | null;
  modified_at?: string | number | null;
  version: string;
  content_hash: string;
  link_id: string;
  original_url: string;
  location: string;
  owner: KdocsDocumentActor | null;
  modifier: KdocsDocumentActor | null;
  shared: boolean;
  permissions: Record<string, boolean>;
}

export interface KdocsDocumentListResponse {
  view: KdocsDocumentView;
  items: KdocsDocumentItem[];
  next_page_token: string;
  total: number | null;
  parent: { drive_id: string; file_id: string };
}

export interface KdocsCoverage {
  body: "complete" | "partial" | "unknown";
  limitations: string[];
}

export interface KdocsDocumentReadResponse {
  coverage?: KdocsCoverage;
  continuation?: string;
  snapshot_id?: string;
  status: string;
  task_id: string;
  content: string;
  content_format: string;
  name: string;
  truncated: boolean;
  warnings: string[];
}

export interface KdocsDocumentLinkResponse {
  file_id: string;
  url: string;
}

export interface KdocsListDocumentsParams {
  view?: KdocsDocumentView;
  drive_id?: string;
  parent_id?: string;
  query?: string;
  page_token?: string;
  page_size?: number;
}

export interface KdocsReadDocumentRequest {
  file_id: string;
  task_id?: string;
  continuation?: string;
  format?: "markdown" | "plain" | "kdc";
  max_chars?: number;
  sheet_name?: string;
  sheet_range?: {
    row_from?: number;
    row_to?: number;
    col_from?: number;
    col_to?: number;
  };
}
