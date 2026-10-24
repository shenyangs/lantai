import type { ModelSlotConfig } from "./provider";

export interface VideoGenerationConfig {
  enabled: boolean;
  models: ModelSlotConfig[];
  timeout_seconds: number;
  legacy_model_flags_migrated?: boolean;
}
