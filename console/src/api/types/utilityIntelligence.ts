export type UtilityIntelligenceMode = "follow_active" | "dedicated";

export interface UtilityIntelligenceConfig {
  mode: UtilityIntelligenceMode;
  background_processing_enabled: boolean;
  model: {
    provider_id: string;
    model: string;
  };
  timeout_seconds: number;
}

export interface BackgroundSemanticStatus {
  background_processing_enabled: boolean;
  routing_mode: UtilityIntelligenceMode;
  queue: {
    agent_id: string;
    worker_running: boolean;
    pending: number;
    running: number;
    completed: number;
    failed: number;
    last_error: string;
    last_failed_at: number | null;
    last_completed_at: number | null;
  };
}

export type UtilityArtifactKind =
  | "creative"
  | "correspondence"
  | "copy"
  | "script";

export interface UtilityClassifiedArtifact {
  kind: UtilityArtifactKind;
  title: string;
  source_text: string;
  start: number;
  end: number;
}

export interface UtilityResearchAdvisory {
  label: "注意" | "提示";
  body: string;
}

export interface UtilityArtifactClassificationResponse {
  artifacts: UtilityClassifiedArtifact[];
  advisory?: UtilityResearchAdvisory | null;
  status: "classified" | "disabled" | "unavailable" | "failed";
  model: string;
}
