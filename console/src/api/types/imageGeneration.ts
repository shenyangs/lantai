import type { ModelSlotConfig } from "./provider";

export interface ImageGenerationConfig {
  enabled: boolean;
  models: ModelSlotConfig[];
  timeout_seconds: number;
  works_cover_enabled: boolean;
  works_cover_model: ModelSlotConfig;
}
