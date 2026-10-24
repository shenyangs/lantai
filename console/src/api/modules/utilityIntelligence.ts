import { request } from "../request";
import type {
  BackgroundSemanticStatus,
  UtilityArtifactClassificationResponse,
  UtilityIntelligenceConfig,
} from "../types/utilityIntelligence";

export const utilityIntelligenceApi = {
  getUtilityIntelligenceConfig: () =>
    request<UtilityIntelligenceConfig>("/utility-intelligence/config"),

  getBackgroundSemanticStatus: () =>
    request<BackgroundSemanticStatus>("/utility-intelligence/status"),

  updateUtilityIntelligenceConfig: (body: UtilityIntelligenceConfig) =>
    request<UtilityIntelligenceConfig>("/utility-intelligence/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  classifyUtilityArtifacts: (text: string, signal?: AbortSignal) =>
    request<UtilityArtifactClassificationResponse>(
      "/utility-intelligence/artifacts/classify",
      {
        method: "POST",
        body: JSON.stringify({ text }),
        signal,
      },
    ),
};
