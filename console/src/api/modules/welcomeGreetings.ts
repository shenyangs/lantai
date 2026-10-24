import { request } from "../request";
import type {
  AiWelcomeGreeting,
  WelcomeCulturalCalendar,
  WelcomeTone,
} from "../../pages/Chat/welcomeGreetings/types";

export interface WelcomeGenerationRequest {
  locale: "zh-CN";
  local_date: string;
  timezone: string;
  scenario_keys: string[];
  cultural_calendar: WelcomeCulturalCalendar;
  tone: WelcomeTone;
  policy_version: string;
}

export interface WelcomeGenerationResponse {
  status:
    | "ready"
    | "disabled"
    | "unavailable"
    | "budget_exhausted"
    | "no_safe_candidates";
  candidates: AiWelcomeGreeting[];
  policy_version: string;
}

export const welcomeGreetingsApi = {
  prepare: (body: WelcomeGenerationRequest) =>
    request<WelcomeGenerationResponse>("/welcome-greetings/prepare", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};
