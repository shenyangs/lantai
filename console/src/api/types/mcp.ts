/**
 * MCP (Model Context Protocol) client types
 */

export interface MCPClientInfo {
  /** Unique client key identifier */
  key: string;
  /** Monotonic version used to order configuration writes */
  revision: number;
  /** Client display name */
  name: string;
  /** Client description */
  description: string;
  /** Whether the client is enabled */
  enabled: boolean;
  /** MCP transport type */
  transport: "stdio" | "streamable_http" | "sse";
  /** Remote MCP endpoint URL for HTTP/SSE transport */
  url: string;
  /** HTTP headers for remote transport */
  headers: Record<string, string>;
  /** Command to launch the MCP server */
  command: string;
  /** Command-line arguments */
  args: string[];
  /** Environment variables */
  env: Record<string, string>;
  /** Working directory for stdio command */
  cwd: string;
  /** OAuth 2.0 Authorization Code + PKCE configuration (secrets masked) */
  oauth: MCPOAuthConfig;
  /** Empty means all server-advertised tools; otherwise explicit allowlist */
  allowed_tools: string[];
}

export interface MCPOAuthConfig {
  enabled: boolean;
  authorization_url: string;
  token_url: string;
  client_id: string;
  client_secret: string;
  scopes: string[];
  redirect_uri: string;
  access_token: string;
  refresh_token: string;
  expires_at: number | null;
  token_type: string;
  extra_authorize_params: Record<string, string>;
  extra_token_params: Record<string, string>;
}

export interface MCPConnectorLifecycleInfo {
  client_key: string;
  status:
    | "disabled"
    | "configuration_required"
    | "authorization_required"
    | "connecting"
    | "ready"
    | "error";
  auth_status:
    | "not_required"
    | "ready"
    | "refreshable"
    | "authorization_required";
  connection_status:
    | "disabled"
    | "disconnected"
    | "connecting"
    | "ready"
    | "error";
  tool_count: number;
  allowed_tools: string[];
  last_probe_at: number | null;
  error_category: string | null;
  error_code: string | null;
  error_message: string | null;
}

export interface MCPOAuthStartResponse {
  authorization_url: string;
  state: string;
  expires_at: number;
}

export interface OfficialMCPConnectorInfo {
  key: string;
  name: string;
  description: string;
  url: string;
  docs_url: string;
  official: boolean;
  configured: boolean;
}

export interface OpenChatCutMCPInfo {
  client_key: "openchatcut";
  name: string;
  description: string;
  configured: boolean;
  enabled: boolean;
  endpoint: string;
  token_configured: boolean;
  connection_status:
    | "not_configured"
    | "disabled"
    | "connecting"
    | "ready"
    | "error";
  repository_url: string;
  docs_url: string;
  license: string;
  approval_mode: "manual";
}

export interface OpenChatCutMCPConfigureRequest {
  endpoint?: string;
  token?: string;
  enabled: boolean;
}

export interface MCPClientCreateRequest {
  /** Unique client key identifier */
  client_key: string;
  /** Client configuration */
  client: {
    /** Client display name */
    name: string;
    /** Client description */
    description?: string;
    /** Whether to enable the client */
    enabled?: boolean;
    /** MCP transport type */
    transport?: "stdio" | "streamable_http" | "sse";
    /** Remote MCP endpoint URL for HTTP/SSE transport */
    url?: string;
    /** HTTP headers for remote transport */
    headers?: Record<string, string>;
    /** Command to launch the MCP server */
    command?: string;
    /** Command-line arguments */
    args?: string[];
    /** Environment variables */
    env?: Record<string, string>;
    /** Working directory for stdio command */
    cwd?: string;
    oauth?: Partial<MCPOAuthConfig>;
    allowed_tools?: string[];
  };
}

export interface MCPToolInfo {
  /** Tool name */
  name: string;
  /** Tool description */
  description: string;
  /** JSON Schema for the tool's input parameters */
  input_schema: Record<string, unknown>;
}

export type GuanlanPackageStatus =
  | "idle"
  | "checking"
  | "updating"
  | "ready"
  | "error";

export interface GuanlanToolInfo {
  /** Tool name */
  name: string;
  /** Tool description */
  description: string;
}

export interface GuanlanMCPInfo {
  /** Reserved client key */
  client_key: string;
  /** Display name */
  name: string;
  /** Display description */
  description: string;
  /** Whether the built-in MCP client is enabled for the current agent */
  enabled: boolean;
  /** Whether this agent already has the reserved client in agent.json */
  configured: boolean;
  /** Live native-capability state; package presence alone is not readiness */
  capability_status: "disabled" | "starting" | "ready" | "degraded" | "error";
  /** Whether the Guanlan MCP transport completed its handshake */
  connected: boolean;
  /** Number of runtime start/reload attempts */
  runtime_attempt_count: number;
  /** Stable diagnostic category without raw exception text */
  runtime_error_code: string | null;
  /** User-facing native runtime status */
  runtime_message: string;
  /** Epoch timestamp of the latest runtime state change */
  runtime_updated_at: number | null;
  /** Whether Guanlan is importable in the backend Python runtime */
  package_available: boolean;
  /** Installed Guanlan package version */
  package_version: string | null;
  /** Guanlan version shipped inside this Lantai build */
  bundled_version: string | null;
  /** Whether the active runtime is bundled or a newer user installation */
  runtime_source: "bundled" | "user";
  /** Last package install/update status */
  package_status: GuanlanPackageStatus;
  /** Human-readable package status detail */
  package_message: string;
  /** Whether this distribution can update Guanlan without an app release */
  package_update_supported: boolean;
  /** Whether this update request actually switched the active Guanlan runtime */
  package_updated: boolean;
  /** Runtime version that was active before a successful update */
  package_previous_version: string | null;
  /** Whether the active MCP client is being reloaded for this update */
  package_restart_required: boolean;
  /** Candidate version discovered during the latest update check */
  package_candidate_version: string | null;
  /** Metadata channel used by the latest update check */
  package_metadata_source: "pypi" | "github_tags" | "unavailable";
  /** Whether metadata was obtained from a non-installing fallback source */
  package_metadata_degraded: boolean;
  /** Whether the discovered candidate is newer than the current runtime */
  package_update_available: boolean;
  /** Project repository URL */
  repository_url: string;
  /** Project license */
  license: string;
  /** Built-in tool summary */
  tools: GuanlanToolInfo[];
}

export interface OfficeCliToolInfo {
  /** Tool name */
  name: string;
  /** Tool description */
  description: string;
}

export interface OfficeCliMCPInfo {
  /** Reserved client key */
  client_key: string;
  /** Display name */
  name: string;
  /** Display description */
  description: string;
  /** Whether the built-in MCP client is enabled for the current agent */
  enabled: boolean;
  /** Whether this agent already has the reserved client in agent.json */
  configured: boolean;
  /** Whether the officecli executable is detected */
  available: boolean;
  /** Detected executable path */
  executable_path: string | null;
  /** How the executable was found */
  executable_source: string;
  /** Detected OfficeCLI version */
  version: string | null;
  /** Human-readable status detail */
  package_message: string;
  /** Project repository URL */
  repository_url: string;
  /** Documentation URL */
  docs_url: string;
  /** Built-in tool summary */
  tools: OfficeCliToolInfo[];
}

export interface ChromeDevToolsMCPInfo {
  client_key: "chrome-devtools";
  name: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  available: boolean;
  capability_status: "disabled" | "starting" | "ready" | "degraded" | "error";
  connected: boolean;
  runtime_attempt_count: number;
  runtime_error_code: string | null;
  runtime_message: string;
  runtime_updated_at: number | null;
  package_version: string;
  runtime_source: "bundled" | "source" | "missing";
  repository_url: string;
  docs_url: string;
  license: "Apache-2.0";
  profile_mode: "persistent_dedicated";
  attaches_existing_chrome: false;
  usage_statistics_enabled: false;
  update_checks_enabled: false;
  performance_crux_enabled: true;
  network_headers_redacted: false;
  unrestricted_paths_enabled: true;
  filesystem_access: "host_authorized";
  screencast_enabled: boolean;
  allowed_tools: string[];
}

export interface MCPClientUpdateRequest {
  /** Last resource version shown when this edit was made */
  expected_revision?: number;
  /** Client display name */
  name?: string;
  /** Client description */
  description?: string;
  /** Whether to enable the client */
  enabled?: boolean;
  /** MCP transport type */
  transport?: "stdio" | "streamable_http" | "sse";
  /** Remote MCP endpoint URL for HTTP/SSE transport */
  url?: string;
  /** HTTP headers for remote transport */
  headers?: Record<string, string>;
  /** Command to launch the MCP server */
  command?: string;
  /** Command-line arguments */
  args?: string[];
  /** Environment variables */
  env?: Record<string, string>;
  /** Working directory for stdio command */
  cwd?: string;
  oauth?: Partial<MCPOAuthConfig>;
  allowed_tools?: string[];
}
