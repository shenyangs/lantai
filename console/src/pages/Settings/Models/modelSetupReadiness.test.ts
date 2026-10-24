import { describe, expect, it } from "vitest";
import type {
  ActiveModelsInfo,
  ProviderInfo,
} from "../../../api/types/provider";
import { deriveModelSetupReadiness } from "./modelSetupReadiness";

function provider(
  overrides: Partial<ProviderInfo> = {},
): ProviderInfo {
  return {
    id: "company-api",
    name: "Company API",
    api_key_prefix: "",
    chat_model: "OpenAIChatModel",
    models: [
      {
        id: "model-a",
        name: "Model A",
        supports_multimodal: null,
        supports_image: null,
        supports_video: null,
        generate_kwargs: {},
      },
    ],
    extra_models: [],
    is_custom: true,
    is_local: false,
    support_model_discovery: true,
    support_connection_check: false,
    freeze_url: false,
    require_api_key: true,
    api_key: "******",
    base_url: "https://models.example/v1",
    generate_kwargs: {},
    configuration_revision: 3,
    connection_verified_revision: null,
    connection_verified_at: null,
    model_verified_revision: null,
    model_verified_id: null,
    model_verified_at: null,
    ...overrides,
  };
}

const active: ActiveModelsInfo = {
  active_llm: { provider_id: "company-api", model: "model-a" },
};

describe("deriveModelSetupReadiness", () => {
  it("keeps a fully filled configuration pending until a real probe is recorded", () => {
    const result = deriveModelSetupReadiness({
      mode: "own",
      providers: [provider()],
      activeModels: active,
    });

    expect(result.providerConfigured).toBe(true);
    expect(result.modelSelected).toBe(true);
    expect(result.modelVerified).toBe(false);
    expect(result.status).toBe("configured_unverified");
  });

  it("invalidates an old probe after either config revision or model changes", () => {
    const verified = provider({
      connection_verified_revision: 3,
      connection_verified_at: "2026-09-19T08:00:00Z",
      model_verified_revision: 3,
      model_verified_id: "model-a",
      model_verified_at: "2026-09-19T08:00:00Z",
    });

    expect(
      deriveModelSetupReadiness({
        mode: "own",
        providers: [verified],
        activeModels: active,
      }).status,
    ).toBe("verified");

    expect(
      deriveModelSetupReadiness({
        mode: "own",
        providers: [
          { ...verified, configuration_revision: 4 },
        ],
        activeModels: active,
      }).status,
    ).toBe("verification_stale");

    expect(
      deriveModelSetupReadiness({
        mode: "own",
        providers: [verified],
        activeModels: {
          active_llm: {
            provider_id: "company-api",
            model: "model-b",
          },
        },
      }).status,
    ).toBe("verification_stale");
  });

  it("derives readiness from the full provider collection, independent of search results", () => {
    const verified = provider({
      connection_verified_revision: 3,
      connection_verified_at: "2026-09-19T08:00:00Z",
      model_verified_revision: 3,
      model_verified_id: "model-a",
      model_verified_at: "2026-09-19T08:00:00Z",
    });

    const beforeSearch = deriveModelSetupReadiness({
      mode: "own",
      providers: [verified],
      activeModels: active,
    });
    const noVisibleSearchMatches: ProviderInfo[] = [];

    expect(beforeSearch.status).toBe("verified");
    expect(noVisibleSearchMatches).toHaveLength(0);
    expect(
      deriveModelSetupReadiness({
        mode: "own",
        providers: [verified],
        activeModels: active,
      }),
    ).toEqual(beforeSearch);
  });

  it("does not borrow a verified provider from another setup mode", () => {
    const result = deriveModelSetupReadiness({
      mode: "local",
      providers: [
        provider({
          connection_verified_revision: 3,
          model_verified_revision: 3,
          model_verified_id: "model-a",
        }),
      ],
      activeModels: active,
    });

    expect(result.status).toBe("not_configured");
    expect(result.provider).toBeNull();
  });
});
