import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import dayjs from "dayjs";
import { parseNaturalLanguageReminderTime } from "./index";

const pageSource = readFileSync(
  join(process.cwd(), "src/pages/Control/Reminders/index.tsx"),
  "utf8",
);

describe("natural-language reminder time", () => {
  const now = dayjs("2026-09-07T10:20:00+08:00");

  it("parses relative Chinese dates and afternoon hours", () => {
    expect(
      parseNaturalLanguageReminderTime("明天下午 3 点", now)?.format(
        "YYYY-MM-DD HH:mm",
      ),
    ).toBe("2026-09-08 15:00");
  });

  it("parses calendar dates and explicit minutes", () => {
    expect(
      parseNaturalLanguageReminderTime("9月12日上午10:30", now)?.format(
        "YYYY-MM-DD HH:mm",
      ),
    ).toBe("2026-09-12 10:30");
  });

  it("preserves the Chinese half-hour meaning instead of silently rounding down", () => {
    expect(
      parseNaturalLanguageReminderTime("明天下午3点半", now)?.format(
        "YYYY-MM-DD HH:mm",
      ),
    ).toBe("2026-09-08 15:30");
  });

  it("does not invent a time for unrelated text", () => {
    expect(parseNaturalLanguageReminderTime("记得交材料", now)).toBeNull();
  });

  it("rejects normalized overflow dates", () => {
    expect(parseNaturalLanguageReminderTime("2月30日上午10点", now)).toBeNull();
    expect(parseNaturalLanguageReminderTime("13月5日上午10点", now)).toBeNull();
    expect(
      parseNaturalLanguageReminderTime("2026-02-30 10:00", now),
    ).toBeNull();
  });

  it("leaves the page-header reminder action secondary when the empty state owns the primary action", () => {
    expect(pageSource).toContain('activeTab === "today"');
    expect(pageSource).toContain("(timeline?.items.length ?? 0) === 0");
    expect(pageSource).toContain('? "default"');
  });
});
