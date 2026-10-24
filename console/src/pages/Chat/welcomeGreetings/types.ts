export type {
  WelcomeCulturalCalendar,
  WelcomeTone,
} from "../../../theme/appearance";
import type {
  WelcomeCulturalCalendar,
  WelcomeTone,
} from "../../../theme/appearance";
export type WelcomeGreetingSource = "fixed" | "ai" | "neutral";

export interface WelcomeGreeting {
  id: string;
  locale: string;
  group: string;
  title: string;
  subtitle: string;
  source: WelcomeGreetingSource;
  version: number;
  scope: "new_chat_empty_welcome";
  eligibilityKey?: string;
  tone: WelcomeTone;
  requiresCulturalPack?: boolean;
  canEnterChatHistory: false;
}

export interface AiWelcomeGreeting extends WelcomeGreeting {
  source: "ai";
  validFrom: string;
  validUntil: string;
  scenarioKeys: string[];
  reviewStatus: "approved";
  policyVersion: string;
}

export interface WelcomeSnapshot {
  entryId: string;
  selectedAt: string;
  expiresAt: string;
  greeting: WelcomeGreeting;
}

export interface WelcomeGenerationPreferences {
  culturalCalendar: WelcomeCulturalCalendar;
  tone: WelcomeTone;
}
