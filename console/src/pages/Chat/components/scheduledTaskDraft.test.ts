import { describe, expect, it } from "vitest";
import {
  buildCronJobSpecFromDraft,
  formatCronLabel,
  getCronJobInstructionPreview,
  parseScheduledTaskDraftOutput,
} from "./scheduledTaskDraft";

describe("scheduledTaskDraft", () => {
  it("parses a tool text block into a confirmed cron job spec", () => {
    const payload = {
      type: "scheduled_task_draft",
      summary: "每天 9 点运行",
      prompt: "帮我生成每日早报",
      spec: {
        name: "每日早报",
        enabled: true,
        schedule: {
          type: "cron",
          cron: "0 9 * * *",
          timezone: "Asia/Shanghai",
        },
        task_type: "agent",
        request: {
          input: [
            {
              role: "user",
              content: [{ type: "text", text: "帮我生成每日早报" }],
            },
          ],
        },
        dispatch: {
          type: "channel",
          channel: "console",
          target: {
            user_id: "",
            session_id: "",
          },
          mode: "stream",
        },
      },
    };

    const draft = parseScheduledTaskDraftOutput([
      { type: "text", text: JSON.stringify(payload) },
    ]);
    expect(draft).not.toBeNull();

    const spec = buildCronJobSpecFromDraft(draft!, {
      userId: "sam",
      sessionId: "console:sam",
      channel: "console",
    });

    expect(spec.id).toBeUndefined();
    expect(spec.name).toBe("每日早报");
    expect(spec.schedule.cron).toBe("0 9 * * *");
    expect(spec.dispatch.target.user_id).toBe("sam");
    expect(spec.dispatch.target.session_id).toBe("console:sam");
    expect(spec.request?.user_id).toBe("sam");
    expect(spec.request?.session_id).toBe("console:sam");
    expect(getCronJobInstructionPreview(spec)).toBe("帮我生成每日早报");
  });

  it("formats common cron expressions for confirmation copy", () => {
    expect(formatCronLabel("0 9 * * *", "Asia/Shanghai")).toBe(
      "每天 09:00（Asia/Shanghai）",
    );
    expect(formatCronLabel("30 10 * * mon,wed", "Asia/Shanghai")).toBe(
      "每周一、周三 10:30（Asia/Shanghai）",
    );
  });
});
