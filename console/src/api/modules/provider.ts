import { request } from "../request";
import type {
  ProviderInfo,
  ProviderConfigRequest,
  ActiveModelsInfo,
  GetActiveModelsRequest,
  ModelSlotRequest,
  CreateCustomProviderRequest,
  AddModelRequest,
  ModelConfigRequest,
  LocalActionResponse,
  LocalModelConfig,
  LocalModelConfigRequest,
  TestConnectionResponse,
  TestProviderRequest,
  TestModelRequest,
  ModelCompatibilityReport,
  DiscoverModelsResponse,
  ProbeMultimodalResponse,
  ReasoningCapabilityResponse,
  SeriesResponse,
  DiscoverExtendedResponse,
  FilterModelsRequest,
  FilterModelsResponse,
  ProviderOAuthStart,
  ProviderOAuthStatus,
  TokenDanceBalance,
  TokenDanceUsage,
  TokenDanceRedemptionResult,
  TokenDancePaymentSession,
  TokenDancePaymentSessionStatus,
} from "../types";

function buildActiveModelQuery(params?: GetActiveModelsRequest): string {
  if (!params?.scope && !params?.agent_id) {
    return "/models/active";
  }

  const searchParams = new URLSearchParams();
  if (params.scope) {
    searchParams.set("scope", params.scope);
  }
  if (params.agent_id) {
    searchParams.set("agent_id", params.agent_id);
  }

  return `/models/active?${searchParams.toString()}`;
}

let listProvidersPromise: Promise<ProviderInfo[]> | null = null;
const activeModelPromises = new Map<string, Promise<ActiveModelsInfo>>();

export const providerApi = {
  listProviders: () => {
    if (listProvidersPromise) return listProvidersPromise;
    listProvidersPromise = request<ProviderInfo[]>("/models").finally(() => {
      listProvidersPromise = null;
    });
    return listProvidersPromise;
  },

  configureProvider: (providerId: string, body: ProviderConfigRequest) =>
    request<ProviderInfo>(`/models/${encodeURIComponent(providerId)}/config`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  startProviderOAuth: (providerId: string) =>
    request<ProviderOAuthStart>(
      `/models/${encodeURIComponent(providerId)}/oauth/start`,
      { method: "POST" },
    ),

  getProviderOAuthStatus: (providerId: string, flowId: string) =>
    request<ProviderOAuthStatus>(
      `/models/${encodeURIComponent(providerId)}/oauth/status/${encodeURIComponent(
        flowId,
      )}`,
    ),

  getTokenDanceBalance: () =>
    request<TokenDanceBalance>("/models/tokendance/account/balance"),

  getTokenDanceUsage: (limit = 5, window = "7d") => {
    const params = new URLSearchParams({
      limit: String(limit),
      window,
    });
    return request<TokenDanceUsage>(
      `/models/tokendance/account/usage?${params.toString()}`,
    );
  },

  redeemTokenDanceCode: (code: string) =>
    request<TokenDanceRedemptionResult>(
      "/models/tokendance/account/redemption/redeem",
      {
        method: "POST",
        body: JSON.stringify({ code }),
      },
    ),

  createTokenDancePaymentSession: (amount: number, clientIntentId: string) =>
    request<TokenDancePaymentSession>(
      "/models/tokendance/account/payment/sessions",
      {
        method: "POST",
        body: JSON.stringify({ amount, client_intent_id: clientIntentId }),
      },
    ),

  getTokenDancePaymentSession: (sessionId: string) =>
    request<TokenDancePaymentSessionStatus>(
      `/models/tokendance/account/payment/sessions/${encodeURIComponent(sessionId)}`,
    ),

  getActiveModels: (params?: GetActiveModelsRequest) => {
    const key = buildActiveModelQuery(params);
    const cached = activeModelPromises.get(key);
    if (cached) return cached;
    const promise = request<ActiveModelsInfo>(key).finally(() => {
      activeModelPromises.delete(key);
    });
    activeModelPromises.set(key, promise);
    return promise;
  },

  setActiveLlm: (body: ModelSlotRequest) =>
    request<ActiveModelsInfo>("/models/active", {
      method: "PUT",
      body: JSON.stringify(body),
    }).then((result) => {
      activeModelPromises.clear();
      return result;
    }),

  /* ---- Custom provider CRUD ---- */

  createCustomProvider: (body: CreateCustomProviderRequest) =>
    request<ProviderInfo>("/models/custom-providers", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteCustomProvider: (providerId: string) =>
    request<ProviderInfo[]>(
      `/models/custom-providers/${encodeURIComponent(providerId)}`,
      { method: "DELETE" },
    ),

  /* ---- Model CRUD (works for both built-in and custom providers) ---- */

  addModel: (providerId: string, body: AddModelRequest) =>
    request<ProviderInfo>(`/models/${encodeURIComponent(providerId)}/models`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  removeModel: (providerId: string, modelId: string) =>
    request<ProviderInfo>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}`,
      { method: "DELETE" },
    ),

  configureModel: (
    providerId: string,
    modelId: string,
    body: ModelConfigRequest,
  ) =>
    request<ProviderInfo>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}/config`,
      {
        method: "PUT",
        body: JSON.stringify(body),
      },
    ),

  configureLocalModelSettings: (body: LocalModelConfigRequest) =>
    request<LocalActionResponse>(`/local-models/config`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  getLocalModelConfig: () => request<LocalModelConfig>("/local-models/config"),

  /* ---- Test Connection ---- */

  testProviderConnection: (providerId: string, body?: TestProviderRequest) =>
    request<TestConnectionResponse>(
      `/models/${encodeURIComponent(providerId)}/test`,
      {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      },
    ),

  testModelConnection: (providerId: string, body: TestModelRequest) =>
    request<TestConnectionResponse>(
      `/models/${encodeURIComponent(providerId)}/models/test`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  checkModelCompatibility: (providerId: string, body: TestModelRequest) =>
    request<ModelCompatibilityReport>(
      `/models/${encodeURIComponent(providerId)}/models/compatibility-check`,
      {
        method: "POST",
        body: JSON.stringify(body),
      },
    ),

  discoverModels: (
    providerId: string,
    body?: TestProviderRequest,
    save: boolean = true,
  ) => {
    const url = new URL(
      `/models/${encodeURIComponent(providerId)}/discover`,
      window.location.origin,
    );
    url.searchParams.set("save", save.toString());
    return request<DiscoverModelsResponse>(url.pathname + url.search, {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    });
  },

  probeMultimodal: (providerId: string, modelId: string) =>
    request<ProbeMultimodalResponse>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}/probe-multimodal`,
      { method: "POST" },
    ),

  getReasoningCapability: (providerId: string, modelId: string) =>
    request<ReasoningCapabilityResponse>(
      `/models/${encodeURIComponent(providerId)}/models/${encodeURIComponent(
        modelId,
      )}/reasoning-capability`,
    ),

  /* ---- OpenRouter specific endpoints ---- */

  getOpenRouterSeries: () =>
    request<SeriesResponse>("/models/openrouter/series"),

  discoverOpenRouterExtended: (body?: TestProviderRequest) =>
    request<DiscoverExtendedResponse>("/models/openrouter/discover-extended", {
      method: "POST",
      body: body ? JSON.stringify(body) : undefined,
    }),

  filterOpenRouterModels: (body: FilterModelsRequest) =>
    request<FilterModelsResponse>("/models/openrouter/models/filter", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
