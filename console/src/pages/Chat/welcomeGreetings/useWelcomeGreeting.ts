import { useEffect, useRef } from "react";

import { welcomeGreetingsApi } from "../../../api/modules/welcomeGreetings";
import type { AppearanceDisplaySettings } from "../../../theme/appearance";
import {
  buildWelcomeSnapshot,
  eligibleScenarioKeys,
  WELCOME_POLICY_VERSION,
} from "./policy";
import {
  readWelcomeCache,
  recordDisplayedGreeting,
  storePreparedCandidates,
} from "./state";
import type { WelcomeGreeting, WelcomeSnapshot } from "./types";

interface UseWelcomeGreetingInput {
  agentId: string;
  displaySettings: AppearanceDisplaySettings;
  enabled: boolean;
  entryId: string;
  fallbackTitle: string;
  fallbackSubtitle: string;
  locale: string;
}

function localDate(now: Date): string {
  return [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
}

export function useWelcomeGreeting({
  agentId,
  displaySettings,
  enabled,
  entryId,
  fallbackTitle,
  fallbackSubtitle,
  locale,
}: UseWelcomeGreetingInput): WelcomeGreeting {
  const snapshotRef = useRef<WelcomeSnapshot | null>(null);
  const resolvedLocale =
    typeof locale === "string" && locale.trim().length > 0 ? locale : "und";
  const isChinese = resolvedLocale.toLowerCase().startsWith("zh");
  const dynamicEnabled = enabled && isChinese && displaySettings.dynamicWelcome;
  const normalizedLocale = "zh-CN";
  const now = new Date();
  const cache =
    typeof window === "undefined"
      ? { recentGreetingIds: [], aiCandidates: [] }
      : readWelcomeCache(window.localStorage, agentId, normalizedLocale);

  if (!dynamicEnabled) {
    snapshotRef.current = null;
  } else {
    snapshotRef.current = buildWelcomeSnapshot({
      agentId,
      aiCandidates: displaySettings.aiWelcome ? cache.aiCandidates : [],
      culturalCalendar: displaySettings.welcomeCulturalCalendar,
      enabled: true,
      entryId: `${agentId}:${entryId}`,
      locale: normalizedLocale,
      now,
      personaMode: displaySettings.personaMode,
      previousSnapshot: snapshotRef.current,
      recentGreetingIds: cache.recentGreetingIds,
      tone: displaySettings.welcomeTone,
    });
  }

  const greeting: WelcomeGreeting = snapshotRef.current?.greeting ?? {
    id: "greeting.localized.default",
    locale: resolvedLocale,
    group: "neutral",
    title: fallbackTitle,
    subtitle: fallbackSubtitle,
    source: "neutral",
    version: 1,
    scope: "new_chat_empty_welcome",
    tone: "restrained",
    canEnterChatHistory: false,
  };

  useEffect(() => {
    if (!dynamicEnabled) return;
    recordDisplayedGreeting(
      window.localStorage,
      agentId,
      normalizedLocale,
      greeting.id,
    );
  }, [agentId, dynamicEnabled, entryId, greeting.id]);

  useEffect(() => {
    if (
      !dynamicEnabled ||
      !displaySettings.aiWelcome ||
      displaySettings.personaMode === "off"
    ) {
      return;
    }
    let active = true;
    const preparedAt = new Date();
    void welcomeGreetingsApi
      .prepare({
        locale: normalizedLocale,
        local_date: localDate(preparedAt),
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        scenario_keys: eligibleScenarioKeys(
          preparedAt,
          displaySettings.welcomeCulturalCalendar,
        ),
        cultural_calendar: displaySettings.welcomeCulturalCalendar,
        tone: displaySettings.welcomeTone,
        policy_version: WELCOME_POLICY_VERSION,
      })
      .then((response) => {
        if (
          active &&
          response.status === "ready" &&
          response.candidates.length > 0
        ) {
          // Deliberately do not update React state: this entry keeps its snapshot.
          storePreparedCandidates(
            window.localStorage,
            agentId,
            normalizedLocale,
            response.candidates,
          );
        }
      })
      .catch(() => {
        // Fixed copy is the complete fallback; background preparation stays silent.
      });
    return () => {
      active = false;
    };
  }, [
    agentId,
    displaySettings.aiWelcome,
    displaySettings.personaMode,
    displaySettings.welcomeCulturalCalendar,
    displaySettings.welcomeTone,
    dynamicEnabled,
    entryId,
  ]);

  return greeting;
}
