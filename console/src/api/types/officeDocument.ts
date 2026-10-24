export interface OfficeCliStatus {
  enabled: boolean;
  available: boolean;
  path: string;
  source: string;
  version: string | null;
  mcp_supported: boolean;
  config_path: string;
  repository_url: string;
  docs_url: string;
  install_command: string;
  message: string;
  error: string;
  features: string[];
}

export interface OfficeCliConfigUpdate {
  enabled?: boolean;
  officecli_path?: string;
}

export type OfficeDocumentPreviewMode =
  | "text"
  | "outline"
  | "annotated"
  | "stats"
  | "issues"
  | "html";

export interface OfficeDocumentPreviewRequest {
  file_path: string;
  mode?: OfficeDocumentPreviewMode;
}

export interface OfficeDocumentPreview {
  available: boolean;
  method: string;
  mode: string;
  content_type: string;
  content: string;
  file_path: string;
  output_path: string;
  message: string;
}

export interface OfficeDocumentCompareRequest {
  before_file_path: string;
  after_file_path: string;
}

export interface OfficeDocumentCompare {
  before_file_path: string;
  after_file_path: string;
  before_method: string;
  after_method: string;
  content_type: string;
  unified_diff: string;
  added_lines: number;
  removed_lines: number;
  changed: boolean;
  message: string;
}

export interface OfficeTaskTemplate {
  id: string;
  title: string;
  description: string;
  prompt: string;
}

export interface OfficeTaskTemplatesResponse {
  templates: OfficeTaskTemplate[];
}
