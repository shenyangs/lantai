import { describe, expect, it } from "vitest";

import type { ProviderInfo } from "../../../api/types";
import { remoteProviderReadiness } from "./providerReadiness";

function provider(overrides: Partial<ProviderInfo> = {}): ProviderInfo {
  return {
    id: "custom-provider",
    name: "Custom",
    api_key_prefix: "",
    chat_model: "model-1",
    models: [
      {
        id: "model-1",
        name: "Model 1",
        supports_multimodal: false,
        supports_image: false,
        supports_video: false,
        generate_kwargs: {},
      },
    ],
    extra_models: [],
    is_custom: true,
    is_local: false,
    support_model_discovery: true,
    support_connection_check: true,
    freeze_url: false,
    require_api_key: true,
    api_key: "",
    base_url: "https://example.test/v1",
    generate_kwargs: {},
    ...overrides,
  };
}

describe("remoteProviderReadiness", () => {
  it("keeps a custom provider with a missing required key in setup", () => {
    expect(remoteProviderReadiness(provider())).toBe("not_configured");
  });

  it("distinguishes model setup, an explicit check, and unverified configuration", () => {
    expect(
      remoteProviderReadiness(provider({ api_key: "sk-test", models: [] })),
    ).toBe("needs_models");
    expect(
      remoteProviderReadiness(
        provider({ id: "kimi-codingplan", api_key: "sk-test" }),
      ),
    ).toBe("needs_connection_check");
    expect(remoteProviderReadiness(provider({ api_key: "sk-test" }))).toBe(
      "configured_unverified",
    );
  });
});
