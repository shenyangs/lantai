import { describe, expect, it } from "vitest";

import { normalizeOpenAICompatibleProvider } from "./openAICompatibleProvider";

describe("normalizeOpenAICompatibleProvider", () => {
  it("keeps a user-defined relay URL and enables OpenAI-compatible discovery", () => {
    const normalized = normalizeOpenAICompatibleProvider(
      {
        name: "  My Relay  ",
        base_url: " https://relay.example.com/openai/v1/// ",
        api_key: " sk-relay ",
        model_id: " gpt-relay-1 ",
      },
      36,
    );

    expect(normalized).toEqual({
      request: {
        id: "my-relay-10",
        name: "My Relay",
        default_base_url: "https://relay.example.com/openai/v1",
        api_key: "sk-relay",
        chat_model: "OpenAIChatModel",
        support_model_discovery: true,
        require_api_key: true,
        meta: {
          provider_kind: "openai_compatible",
          user_configured_gateway: true,
        },
      },
      modelId: "gpt-relay-1",
    });
  });

  it("supports relays without an API key and a manually supplied model", () => {
    const normalized = normalizeOpenAICompatibleProvider(
      {
        name: "内网中转",
        base_url: "http://127.0.0.1:8080/v1/",
        model_id: "custom-model",
      },
      71,
    );

    expect(normalized.request.id).toBe("openai-compatible-1z");
    expect(normalized.request.default_base_url).toBe(
      "http://127.0.0.1:8080/v1",
    );
    expect(normalized.request.api_key).toBe("");
    expect(normalized.request.require_api_key).toBe(false);
    expect(normalized.modelId).toBe("custom-model");
  });
});
