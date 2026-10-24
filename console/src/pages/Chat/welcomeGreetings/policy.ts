import type { PersonaMode } from "../../../theme/appearance";
import { FIXED_WELCOME_CATALOG } from "./catalog.zh-CN";
import type {
  AiWelcomeGreeting,
  WelcomeCulturalCalendar,
  WelcomeGreeting,
  WelcomeSnapshot,
  WelcomeTone,
} from "./types";

export type { WelcomeSnapshot } from "./types";

export const WELCOME_POLICY_VERSION = "welcome-v1.2026-09-27";
const SNAPSHOT_TTL_MS = 30 * 60 * 1000;
const RECENT_WINDOW = 8;

const NEUTRAL_GREETING: WelcomeGreeting = {
  id: "greeting.neutral.default",
  locale: "zh-CN",
  group: "neutral",
  title: "今天想做点什么？",
  subtitle: "可以从一个问题、一个想法，或者一件要推进的事开始。",
  source: "neutral",
  version: 1,
  scope: "new_chat_empty_welcome",
  tone: "restrained",
  canEnterChatHistory: false,
};

const FORBIDDEN_CONTEXT_REFERENCES = [
  /上次/u,
  /刚才/u,
  /接着/u,
  /继续.{0,8}(?:做|聊|改|写|推进)/u,
  /没(?:有)?做完/u,
  /还记得/u,
  /你最近/u,
  /你这几天/u,
  /那份(?:方案|文档|作品|任务)/u,
  /此前(?:的|那)/u,
  /欢迎回来/u,
  /(?:今天|这会儿)?(?:还在|又在)(?:忙|工作)/u,
  /(?:熬夜|凌晨|早点休息|辛苦了)/u,
  /(?:看起来|似乎|感觉)(?:你|今天)/u,
  /你的?(?:状态|心情|进度)/u,
  /这次/u,
  /沈行知/u,
];

const FESTIVAL_TEXT_REQUIREMENTS: readonly [RegExp, readonly string[]][] = [
  [/(?:春节|新春)/u, ["festival.spring_festival"]],
  [/除夕/u, ["festival.lunar_eve"]],
  [/元宵/u, ["festival.lantern"]],
  [/端午/u, ["festival.dragon_boat"]],
  [/七夕/u, ["festival.qixi"]],
  [/中秋/u, ["festival.mid_autumn"]],
  [/(?:国庆|十月一日)/u, ["festival.national_day"]],
  [/圣诞/u, ["festival.christmas"]],
  [/冬至/u, ["festival.winter_solstice"]],
  [/元旦/u, ["festival.new_year"]],
  [/新年/u, ["festival.new_year", "festival.spring_festival"]],
];

function hash32(value: string): number {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function localDayKey(now: Date): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function timeScenario(hour: number): string {
  if (hour >= 5 && hour <= 8) return "time.dawn";
  if (hour >= 9 && hour <= 11) return "time.morning";
  if (hour >= 12 && hour <= 13) return "time.noon";
  if (hour >= 14 && hour <= 17) return "time.afternoon";
  if (hour >= 18 && hour <= 22) return "time.evening";
  return "time.late";
}

function lunarMonthDay(now: Date): { month: number; day: number } | null {
  try {
    const parts = new Intl.DateTimeFormat("en-u-ca-chinese", {
      month: "numeric",
      day: "numeric",
    }).formatToParts(now);
    const month = Number(parts.find((part) => part.type === "month")?.value);
    const day = Number(parts.find((part) => part.type === "day")?.value);
    return Number.isFinite(month) && Number.isFinite(day)
      ? { month, day }
      : null;
  } catch {
    return null;
  }
}

function chinaFestivalKeys(now: Date): string[] {
  const keys: string[] = [];
  const lunar = lunarMonthDay(now);
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  const tomorrowLunar = lunarMonthDay(tomorrow);

  if (lunar?.month === 1 && lunar.day === 1) {
    keys.push("festival.spring_festival");
  }
  if (lunar?.month === 1 && lunar.day === 15) {
    keys.push("festival.lantern");
  }
  if (lunar?.month === 5 && lunar.day === 5) {
    keys.push("festival.dragon_boat");
  }
  if (lunar?.month === 7 && lunar.day === 7) {
    keys.push("festival.qixi");
  }
  if (lunar?.month === 8 && lunar.day === 15) {
    keys.push("festival.mid_autumn");
  }
  if (lunar && tomorrowLunar?.month === 1 && tomorrowLunar.day === 1) {
    keys.push("festival.lunar_eve");
  }

  if (now.getMonth() === 9 && now.getDate() === 1) {
    keys.push("festival.national_day");
  }

  const winterSolsticeDays: Record<number, number> = {
    2024: 21,
    2025: 21,
    2026: 22,
    2027: 22,
    2028: 21,
    2029: 21,
    2030: 22,
    2031: 22,
    2032: 21,
    2033: 21,
    2034: 22,
    2035: 22,
  };
  if (
    now.getMonth() === 11 &&
    winterSolsticeDays[now.getFullYear()] === now.getDate()
  ) {
    keys.push("festival.winter_solstice");
  }
  return keys;
}

function globalFestivalKeys(now: Date): string[] {
  if (now.getMonth() === 0 && now.getDate() === 1) {
    return ["festival.new_year"];
  }
  if (now.getMonth() === 11 && now.getDate() === 25) {
    return ["festival.christmas"];
  }
  return [];
}

export function eligibleScenarioKeys(
  now: Date,
  culturalCalendar: WelcomeCulturalCalendar,
): string[] {
  const keys = ["universal", timeScenario(now.getHours())];
  const day = now.getDay();
  const date = now.getDate();

  if (day === 1) keys.push("rhythm.week_start");
  if (day >= 2 && day <= 4) keys.push("rhythm.week_middle");
  if (day === 0 || day === 6) keys.push("rhythm.weekend");
  if (date <= 3) keys.push("rhythm.month_start");

  const next = new Date(now);
  next.setDate(date + 3);
  if (next.getMonth() !== now.getMonth()) {
    keys.push("rhythm.month_end");
  }

  if (culturalCalendar === "china" || culturalCalendar === "all") {
    keys.push(...chinaFestivalKeys(now));
  }
  if (culturalCalendar === "global" || culturalCalendar === "all") {
    keys.push(...globalFestivalKeys(now));
  }
  return Array.from(new Set(keys));
}

export function isSafeAiWelcomeGreeting(
  candidate: AiWelcomeGreeting,
  now: Date,
  scenarioKeys: readonly string[],
  locale: string,
  tone: WelcomeTone,
): boolean {
  if (
    candidate.scope !== "new_chat_empty_welcome" ||
    candidate.source !== "ai" ||
    candidate.reviewStatus !== "approved" ||
    candidate.canEnterChatHistory !== false ||
    candidate.policyVersion !== WELCOME_POLICY_VERSION ||
    candidate.locale !== locale ||
    candidate.tone !== tone
  ) {
    return false;
  }
  const title = candidate.title.trim();
  const subtitle = candidate.subtitle.trim();
  if (!title || !subtitle || title.length > 24 || subtitle.length > 48) {
    return false;
  }
  const text = `${title}\n${subtitle}`;
  if (FORBIDDEN_CONTEXT_REFERENCES.some((pattern) => pattern.test(text))) {
    return false;
  }
  if (
    FESTIVAL_TEXT_REQUIREMENTS.some(
      ([pattern, allowedKeys]) =>
        pattern.test(text) &&
        !allowedKeys.some((key) => candidate.scenarioKeys.includes(key)),
    )
  ) {
    return false;
  }
  const validFrom = Date.parse(candidate.validFrom);
  const validUntil = Date.parse(candidate.validUntil);
  if (
    !Number.isFinite(validFrom) ||
    !Number.isFinite(validUntil) ||
    now.getTime() < validFrom ||
    now.getTime() >= validUntil
  ) {
    return false;
  }
  return (
    candidate.scenarioKeys.length > 0 &&
    candidate.scenarioKeys.every((key) => scenarioKeys.includes(key))
  );
}

function choose<T>(items: readonly T[], seed: string): T {
  return items[hash32(seed) % items.length]!;
}

function selectGreeting(input: {
  aiCandidates: readonly AiWelcomeGreeting[];
  culturalCalendar: WelcomeCulturalCalendar;
  entryId: string;
  locale: string;
  now: Date;
  recentGreetingIds: readonly string[];
  tone: WelcomeTone;
}): WelcomeGreeting {
  const scenarioKeys = eligibleScenarioKeys(input.now, input.culturalCalendar);
  const recent = new Set(input.recentGreetingIds.slice(-RECENT_WINDOW));
  const festivalKeys = scenarioKeys.filter((key) =>
    key.startsWith("festival."),
  );
  const timeKeys = scenarioKeys.filter((key) => key.startsWith("time."));
  const rhythmKeys = scenarioKeys.filter((key) => key.startsWith("rhythm."));
  const safeAi = input.aiCandidates.filter(
    (candidate) =>
      !recent.has(candidate.id) &&
      isSafeAiWelcomeGreeting(
        candidate,
        input.now,
        scenarioKeys,
        input.locale,
        input.tone,
      ),
  );
  const contextMatchedAi =
    festivalKeys.length > 0
      ? safeAi.filter((candidate) =>
          candidate.scenarioKeys.some((key) => festivalKeys.includes(key)),
        )
      : safeAi;
  const seed = [
    input.entryId,
    localDayKey(input.now),
    scenarioKeys.join(","),
    WELCOME_POLICY_VERSION,
  ].join("|");

  if (contextMatchedAi.length > 0 && hash32(`${seed}|source`) % 100 < 55) {
    return choose(contextMatchedAi, `${seed}|ai`);
  }

  const regularOrder = (() => {
    const roll = hash32(`${seed}|fixed-layer`) % 100;
    if (rhythmKeys.length > 0 && roll < 20) {
      return [rhythmKeys, timeKeys, ["universal"]];
    }
    if (roll < 70) {
      return [timeKeys, rhythmKeys, ["universal"]];
    }
    return [["universal"], timeKeys, rhythmKeys];
  })();
  const groupOrder =
    festivalKeys.length > 0
      ? [festivalKeys, timeKeys, rhythmKeys, ["universal"]]
      : regularOrder;
  const firstAvailable = (excludeRecent: boolean) => {
    for (const groups of groupOrder) {
      const candidates = FIXED_WELCOME_CATALOG.filter(
        (item) =>
          item.locale === input.locale &&
          groups.includes(item.eligibilityKey || item.group) &&
          (!excludeRecent || !recent.has(item.id)),
      );
      if (candidates.length > 0) return candidates;
    }
    return [];
  };
  const freshPool = firstAvailable(true);
  const reusablePool = freshPool.length > 0 ? [] : firstAvailable(false);
  const pool =
    freshPool.length > 0
      ? freshPool
      : reusablePool.length > 0
        ? reusablePool
        : FIXED_WELCOME_CATALOG;
  return choose(pool, `${seed}|fixed`);
}

export function buildWelcomeSnapshot(input: {
  agentId: string;
  aiCandidates: readonly AiWelcomeGreeting[];
  culturalCalendar: WelcomeCulturalCalendar;
  enabled?: boolean;
  entryId: string;
  locale: string;
  now: Date;
  personaMode: PersonaMode;
  previousSnapshot?: WelcomeSnapshot | null;
  recentGreetingIds: readonly string[];
  tone?: WelcomeTone;
}): WelcomeSnapshot {
  const neutralRequired =
    input.enabled === false || input.personaMode === "off";
  if (
    input.previousSnapshot &&
    input.previousSnapshot.entryId === input.entryId &&
    (input.previousSnapshot.greeting.source === "neutral") === neutralRequired
  ) {
    return input.previousSnapshot;
  }

  const selectedAt = input.now.toISOString();
  const greeting = neutralRequired
    ? NEUTRAL_GREETING
    : selectGreeting({
        aiCandidates: input.aiCandidates,
        culturalCalendar: input.culturalCalendar,
        entryId: input.entryId,
        locale: input.locale,
        now: input.now,
        recentGreetingIds: input.recentGreetingIds,
        tone: input.tone ?? "natural",
      });

  return {
    entryId: input.entryId,
    selectedAt,
    expiresAt: new Date(input.now.getTime() + SNAPSHOT_TTL_MS).toISOString(),
    greeting,
  };
}
