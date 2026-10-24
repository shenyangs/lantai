import { describe, expect, it } from "vitest";
import type { ProviderInfo } from "../../../api/types";
import {
  isTokenDanceCredentialConnected,
  partitionProviderSections,
} from "./providerSections";

function provider(id: string, isLocal = false): ProviderInfo {
  return {
    id,
    name: id,
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [],
    extra_models: [],
    is_custom: id === "my-api",
    is_local: isLocal,
    support_model_discovery: false,
    support_connection_check: false,
    freeze_url: false,
    require_api_key: false,
    api_key: "",
    base_url: "https://example.com",
    generate_kwargs: {},
  };
}

describe("partitionProviderSections", () => {
  it("keeps TokenDance out of the ordinary remote provider group", () => {
    const result = partitionProviderSections(
      [provider("tokendance"), provider("my-api"), provider("ollama", true)],
      "",
    );

    expect(result.tokendanceProvider?.id).toBe("tokendance");
    expect(result.regularProviders.map((item) => item.id)).toEqual(["my-api"]);
    expect(result.localProviders.map((item) => item.id)).toEqual(["ollama"]);
  });

  it("has no TokenDance block when the account-gated API omits it", () => {
    const result = partitionProviderSections([provider("my-api")], "");
    expect(result.tokendanceProvider).toBeNull();
    expect(result.regularProviders).toHaveLength(1);
  });
});

describe("isTokenDanceCredentialConnected", () => {
  it("requires the backend-safe active binding projection", () => {
    const item = provider("tokendance");
    item.api_key = "******";

    expect(isTokenDanceCredentialConnected(item)).toBe(false);
    item.meta = { credential_status: "reauthorization_required" };
    expect(isTokenDanceCredentialConnected(item)).toBe(false);
    item.meta = { credential_status: "wrong_account" };
    expect(isTokenDanceCredentialConnected(item)).toBe(false);
    item.meta = { credential_status: "active" };
    expect(isTokenDanceCredentialConnected(item)).toBe(true);
  });
});
