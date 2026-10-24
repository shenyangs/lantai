import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import dayjs from "dayjs";
import type { TFunction } from "i18next";
import type {
  CronJobSpecOutput,
  CronOccurrence,
} from "../../../api/types";
import { projectAuthoritativeOccurrences } from "./index";

const pageSource = readFileSync(
  join(process.cwd(), "src/pages/Control/CronJobs/index.tsx"),
  "utf8",
);

const job: CronJobSpecOutput = {
  id: "daily",
  name: "日报",
  enabled: true,
  schedule: {
    type: "cron",
    cron: "0 9 * * *",
    timezone: "Asia/Shanghai",
  },
  task_type: "text",
  text: "run",
  dispatch: {
    type: "channel",
    channel: "console",
    target: { user_id: "default", session_id: "console:main" },
  },
};

describe("cron occurrence projection", () => {
  it("renders only authoritative occurrences and retains the task timezone", () => {
    const source: CronOccurrence[] = [
      {
        job_id: "daily",
        scheduled_at: "2026-09-14T09:00:00+08:00",
        schedule_timezone: "Asia/Shanghai",
      },
    ];

    const result = projectAuthoritativeOccurrences(
      source,
      [job],
      dayjs("2026-09-14T00:00:00Z"),
      "zh-CN",
      ((key: string) => key) as TFunction,
    );

    expect(result).toHaveLength(1);
    expect(result[0].job.id).toBe("daily");
    expect(result[0].scheduleTimezone).toBe("Asia/Shanghai");
    expect(result[0].taskTimeLabel).toContain("09:00");
  });

  it("does not invent noon placeholders for custom schedules", () => {
    expect(pageSource).not.toContain("todayInWeek.hour(12).minute(0)");
    expect(pageSource).toContain("listCronOccurrences");
  });
});
