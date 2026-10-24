export interface ModelInfo {
  id: string;
  name: string;
  supports_multimodal: boolean | null;
  supports_image: boolean | null;
  supports_video: boolean | null;
  supports_tool_use?: boolean | null;
  probe_source?: string | null;
  is_free?: boolean;
  generate_kwargs: Record<string, unknown>;
  context_window?: number | null;
  max_context_window?: number | null;
  user_context_window?: number | null;
  max_output_tokens?: number | null;
  tokenizer?: string | null;
  recommended_output_reserve?: number | null;
  capability_source?: string | null;
  capability_verified_at?: string | null;
  runtime_state?: string | null;
  parameter_size?: string | null;
  quantization?: string | null;
  size_bytes?: number | null;
  /** Wire protocols explicitly advertised by the provider catalog. */
  supported_protocols?: string[];
  /** Whether this chat-capable model appears in chat model dropdowns. */
  show_in_chat?: boolean;
  /** Enable this media model for conversation-triggered video tasks. */
  use_for_video?: boolean;
}

export interface ProviderOAuthStart {
  flow_id: string;
  authorization_url: string;
  expires_in: number;
}

export interface ProviderOAuthStatus {
  status: "pending" | "exchanging" | "succeeded" | "failed" | "expired";
  message: string;
}

export interface TokenDanceBalance {
  authorization_context_id: string;
  balance: {
    credits: number;
    credits_used: number;
    balance: number;
  };
  balance_cny: string;
  credits_cny: string;
  credits_used_cny: string;
}

export interface TokenDanceUsageItem {
  id: string;
  model_id: string;
  created_at: string;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cost?: number;
  cost_cny: string;
  is_success?: boolean;
}

export interface TokenDanceUsage {
  authorization_context_id: string;
  items: TokenDanceUsageItem[];
  offset: number;
  limit: number;
  has_more: boolean;
}

export interface TokenDanceRedemptionResult {
  authorization_context_id: string;
  credits: number;
  credits_cny: string;
}

export type TokenDancePaymentStatus =
  | "pending"
  | "paid"
  | "failed"
  | "closed"
  | "refunded";

export interface TokenDancePaymentSession {
  authorization_context_id: string;
  id: string;
  amount: number;
  status: TokenDancePaymentStatus;
  payment_url: string;
  alipay_url?: string | null;
  status_url?: string;
  expired_at: number;
  created_at: number;
  paid_at?: number | null;
}

export type TokenDancePaymentSessionStatus = Pick<
  TokenDancePaymentSession,
  "id" | "status"
> &
  Partial<Omit<TokenDancePaymentSession, "id" | "status">>;

export interface ProviderInfo {
  id: string;
  name: string;
  api_key_prefix: string;
  chat_model: string;
  /** Built-in models (for built-in providers) or all models (for custom). */
  models: ModelInfo[];
  /** User-added models (deletable). Only populated for built-in providers. */
  extra_models: ModelInfo[];
  is_custom: boolean;
  is_local: boolean;
  /** Whether this provider supports fetching available models from the provider's API. */
  support_model_discovery: boolean;
  /** Whether this provider supports checking connection to the API without model configuration. */
  support_connection_check: boolean;
  /** Whether the backend implements the text-to-video runtime contract. */
  supports_video_generation?: boolean;
  /** True when the base_url should be frozen (not editable). */
  freeze_url: boolean;
  /** True when an API key is required for this provider. */
  require_api_key: boolean;
  api_key: string;
  base_url: string;
  generate_kwargs: Record<string, unknown>;
  meta?: Record<string, unknown>;
  /** Monotonic version of connection-affecting provider configuration. */
  configuration_revision?: number;
  connection_verified_revision?: number | null;
  connection_verified_at?: string | null;
  model_verified_revision?: number | null;
  model_verified_id?: string | null;
  model_verified_at?: string | null;
}

export interface ProviderConfigRequest {
  api_key?: string;
  credential_ownership_attested?: boolean;
  base_url?: string;
  chat_model?: string;
  generate_kwargs?: Record<string, unknown>;
}

export interface ModelSlotConfig {
  provider_id: string;
  model: string;
}

export interface ActiveModelsInfo {
  active_llm?: ModelSlotConfig;
  context_policy?: ContextPolicyInfo | null;
}

export interface ContextPolicyInfo {
  mode: "smart" | "max" | "economy" | string;
  hard_window: number;
  usable_input_tokens: number;
  target_input_tokens: number;
  output_reserve: number;
  safety_margin: number;
  used_unknown_fallback: boolean;
  status: string;
  source: string;
  switch_action: string;
}

export type ActiveModelScope = "effective" | "global" | "agent";

export interface GetActiveModelsRequest {
  scope?: ActiveModelScope;
  agent_id?: string;
}

export interface ModelSlotRequest {
  provider_id: string;
  model: string;
  scope: Exclude<ActiveModelScope, "effective">;
  agent_id?: string;
}

/* ---- Custom provider CRUD ---- */

export interface CreateCustomProviderRequest {
  id: string;
  name: string;
  default_base_url?: string;
  api_key?: string;
  api_key_prefix?: string;
  chat_model?: string;
  models?: ModelInfo[];
  support_model_discovery?: boolean;
  require_api_key?: boolean;
  meta?: Record<string, unknown>;
}

export interface AddModelRequest {
  id: string;
  name: string;
  is_free?: boolean;
  supports_multimodal?: boolean | null;
  supports_image?: boolean | null;
  supports_video?: boolean | null;
  probe_source?: string | null;
  context_window?: number | null;
  user_context_window?: number | null;
  max_output_tokens?: number | null;
  recommended_output_reserve?: number | null;
  tokenizer?: string | null;
}

export interface ModelConfigRequest {
  generate_kwargs?: Record<string, unknown>;
  context_window?: number | null;
  user_context_window?: number | null;
  max_output_tokens?: number | null;
  recommended_output_reserve?: number | null;
  tokenizer?: string | null;
  show_in_chat?: boolean;
  use_for_video?: boolean;
}

export interface LocalModelConfig {
  max_context_length: number;
  port: number | null;
}

export interface LocalModelConfigRequest {
  max_context_length?: number;
  port?: number | null;
  generate_kwargs?: Record<string, unknown>;
}

/* ---- Local models ---- */

export interface LocalModelInfo {
  id: string;
  name: string;
  size_bytes: number;
  downloaded: boolean;
  source: LocalDownloadSource;
}

export type LocalDownloadSource = "huggingface" | "modelscope" | "auto";

export interface LocalServerStatus {
  available: boolean;
  installable: boolean;
  installed: boolean;
  port: number | null;
  model_name: string | null;
  message: string | null;
}

export interface LocalServerUpdateStatus {
  has_update: boolean;
}

export interface LocalDownloadProgress {
  status:
    | "idle"
    | "pending"
    | "downloading"
    | "canceling"
    | "completed"
    | "failed"
    | "cancelled";
  model_name: string | null;
  downloaded_bytes: number;
  total_bytes: number | null;
  speed_bytes_per_sec: number;
  source: LocalDownloadSource | null;
  error: string | null;
  local_path: string | null;
}

export interface LocalActionResponse {
  status: string;
  message: string;
}

export interface StartLocalServerRequest {
  model_id: string;
}

/* ---- Test Connection ---- */

export interface TestConnectionResponse {
  success: boolean;
  message: string;
  receipt_recorded?: boolean;
  configuration_revision?: number | null;
  verified_model_id?: string | null;
  verified_at?: string | null;
}

export interface TestProviderRequest {
  api_key?: string;
  base_url?: string;
  chat_model?: string;
  model_id?: string;
  generate_kwargs?: Record<string, unknown>;
  include_extended?: boolean;
}

export interface TestModelRequest {
  model_id: string;
}

export type ModelCompatibilityStatus = "supported" | "partial" | "unsupported";

export type ModelCompatibilityCheckStatus =
  | "passed"
  | "failed"
  | "not_observed";

export interface ModelCompatibilityCheck {
  id: string;
  status: ModelCompatibilityCheckStatus;
  detail_code: string;
  event_count: number;
}

export interface ModelCompatibilityReport {
  schema_version: 1;
  provider_id: string;
  model_id: string;
  status: ModelCompatibilityStatus;
  checks: ModelCompatibilityCheck[];
  duration_ms: number;
  event_counts: Record<string, number>;
  trace_digest: string;
  data_classification: "structure_only";
}

export interface DiscoverModelsResponse {
  success: boolean;
  message: string;
  models: ModelInfo[];
  added_count: number;
}

export interface ProbeMultimodalResponse {
  supports_image: boolean;
  supports_video: boolean;
  supports_multimodal: boolean;
  image_message: string;
  video_message: string;
}

export interface ReasoningCapabilityResponse {
  supported: boolean;
  mode: string;
  fixed: boolean;
  default_enabled: boolean;
  note: string;
}

/* ---- OpenRouter extended model types ---- */

export interface ExtendedModelInfo {
  id: string;
  name: string;
  supports_multimodal?: boolean | null;
  supports_image?: boolean | null;
  supports_video?: boolean | null;
  probe_source?: string | null;
  is_free?: boolean;
  provider: string;
  input_modalities: string[];
  output_modalities: string[];
  pricing: Record<string, string>;
  context_window?: number | null;
  max_output_tokens?: number | null;
  recommended_output_reserve?: number | null;
  tokenizer?: string | null;
}

export interface FilterModelsRequest {
  providers?: string[];
  input_modalities?: string[];
  output_modalities?: string[];
  max_prompt_price?: number;
  is_free?: boolean;
}

export interface SeriesResponse {
  series: string[];
}

export interface DiscoverExtendedResponse {
  success: boolean;
  models: ExtendedModelInfo[];
  providers: string[];
  total_count: number;
}

export interface FilterModelsResponse {
  success: boolean;
  models: ExtendedModelInfo[];
  total_count: number;
}
