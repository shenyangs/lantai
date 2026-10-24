import { describe, expect, it } from "vitest";
import type { ProviderInfo } from "../../../api/types/provider";
import { getProviderApiKeyUrl, getProviderDocsUrl } from "./providerMetadata";

function makeProvider(meta?: ProviderInfo["meta"]): ProviderInfo {
  return {
    id: "tokendance",
    name: "TokenDance",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: true,
    support_connection_check: true,
    freeze_url: true,
    require_api_key: true,
    api_key: "",
    base_url: "https://tokendance.space/gateway/v1",
    generate_kwargs: {},
    meta,
  };
}

describe("provider metadata links", () => {
  it("returns TokenDance setup links from provider metadata", () => {
    const provider = makeProvider({
      docs_url: "https://tokendance.space/docs/quickstart",
      api_key_url: "https://tokendance.space/keys",
    });

    expect(getProviderDocsUrl(provider)).toBe(
      "https://tokendance.space/docs/quickstart",
    );
    expect(getProviderApiKeyUrl(provider)).toBe(
      "https://tokendance.space/keys",
    );
  });

  it("ignores non-http provider metadata links", () => {
    const provider = makeProvider({
      docs_url: "javascript:alert(1)",
      api_key_url: "file:///tmp/key",
    });

    expect(getProviderDocsUrl(provider)).toBeNull();
    expect(getProviderApiKeyUrl(provider)).toBeNull();
  });
});
