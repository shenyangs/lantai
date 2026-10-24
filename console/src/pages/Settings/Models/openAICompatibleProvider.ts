import type { CreateCustomProviderRequest } from "@/api/types";

export interface OpenAICompatibleProviderValues {
  name: string;
  base_url: string;
  api_key?: string;
  model_id?: string;
}

export interface NormalizedOpenAICompatibleProvider {
  request: CreateCustomProviderRequest;
  modelId: string;
}

function createProviderId(name: string, now: number): string {
  const slug = name
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 36);
  return `${slug || "openai-compatible"}-${now.toString(36)}`;
}

export function normalizeOpenAICompatibleProvider(
  values: OpenAICompatibleProviderValues,
  now: number = Date.now(),
): NormalizedOpenAICompatibleProvider {
  const name = values.name.trim();
  const apiKey = values.api_key?.trim() || "";

  return {
    request: {
      id: createProviderId(name, now),
      name,
      default_base_url: values.base_url.trim().replace(/\/+$/, ""),
      api_key: apiKey,
      chat_model: "OpenAIChatModel",
      support_model_discovery: true,
      require_api_key: Boolean(apiKey),
      meta: {
        provider_kind: "openai_compatible",
        user_configured_gateway: true,
      },
    },
    modelId: values.model_id?.trim() || "",
  };
}
