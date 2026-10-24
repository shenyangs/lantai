import { describe, expect, it } from "vitest";

import type { ActiveModelsInfo, ProviderInfo } from "../../../api/types";
import {
  canTestProviderConnection,
  getProviderConnectionTestModelId,
  shouldValidateProviderConnectionBeforeSave,
} from "./providerConnectionTest";

function makeModel(id: string, name = id): ProviderInfo["models"][number] {
  return {
    id,
    name,
    supports_multimodal: null,
    supports_image: null,
    supports_video: null,
    generate_kwargs: {},
  };
}

function makeProvider(overrides: Partial<ProviderInfo> = {}): ProviderInfo {
  return {
    id: "example",
    name: "Example",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [],
    extra_models: [],
    is_custom: false,
    is_local: false,
    support_model_discovery: false,
    support_connection_check: false,
    freeze_url: true,
    require_api_key: true,
    api_key: "",
    base_url: "https://api.example.com/v1",
    generate_kwargs: {},
    ...overrides,
  };
}

describe("provider connection tests", () => {
  it("allows provider-level checks even when no model is available", () => {
    const provider = makeProvider({ support_connection_check: true });

    expect(canTestProviderConnection(provider, null)).toBe(true);
    expect(getProviderConnectionTestModelId(provider, null)).toBeUndefined();
    expect(shouldValidateProviderConnectionBeforeSave(provider, null)).toBe(
      true,
    );
  });

  it("uses the active model when provider-level checks need a model", () => {
    const provider = makeProvider({
      id: "coding-plan",
      models: [makeModel("fallback-model", "Fallback")],
    });
    const activeModels: ActiveModelsInfo = {
      active_llm: { provider_id: "coding-plan", model: "active-model" },
    };

    expect(getProviderConnectionTestModelId(provider, activeModels)).toBe(
      "active-model",
    );
    expect(canTestProviderConnection(provider, activeModels)).toBe(true);
    expect(
      shouldValidateProviderConnectionBeforeSave(provider, activeModels),
    ).toBe(true);
  });

  it("falls back to the first known model", () => {
    const provider = makeProvider({
      models: [makeModel("first-model", "First")],
      extra_models: [makeModel("extra-model", "Extra")],
    });

    expect(getProviderConnectionTestModelId(provider, null)).toBe(
      "first-model",
    );
    expect(canTestProviderConnection(provider, null)).toBe(true);
  });

  it("does not force custom provider saves through model tests", () => {
    const provider = makeProvider({
      is_custom: true,
      models: [makeModel("custom-model", "Custom")],
    });

    expect(canTestProviderConnection(provider, null)).toBe(true);
    expect(shouldValidateProviderConnectionBeforeSave(provider, null)).toBe(
      false,
    );
  });

  it("lets local runtime endpoints be saved while their server is offline", () => {
    const provider = makeProvider({
      id: "ollama",
      is_local: true,
      require_api_key: false,
      support_connection_check: true,
      freeze_url: false,
      base_url: "http://localhost:11434",
    });

    expect(canTestProviderConnection(provider, null)).toBe(true);
    expect(shouldValidateProviderConnectionBeforeSave(provider, null)).toBe(
      false,
    );
  });

  it("binds a new TokenDance key before making any connection request", () => {
    const provider = makeProvider({
      id: "tokendance",
      support_connection_check: true,
    });

    expect(shouldValidateProviderConnectionBeforeSave(provider, null)).toBe(
      false,
    );
  });
});
