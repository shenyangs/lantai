import { beforeEach, describe, expect, it } from "vitest";
import {
  buildAccountSetupPath,
  buildModelsSetupPath,
  consumeModelSetupDraft,
  consumeModelSetupDraftSnapshot,
  ensureFirstModelTaskDraft,
  mergeRestoredModelSetupInput,
  normalizeModelSetupReturnTo,
  parseModelSetupMode,
  persistModelSetupDraft,
  persistModelSetupDraftSnapshot,
} from "./modelSetupFlow";

describe("model setup flow", () => {
  beforeEach(() => window.sessionStorage.clear());

  it("accepts only supported setup modes and local chat return paths", () => {
    expect(parseModelSetupMode("watcha")).toBe("watcha");
    expect(parseModelSetupMode("other")).toBeNull();
    expect(normalizeModelSetupReturnTo("/chat/thread-1?from=draft")).toBe(
      "/chat/thread-1?from=draft",
    );
    expect(normalizeModelSetupReturnTo("https://evil.example/chat")).toBe(
      "/chat",
    );
    expect(normalizeModelSetupReturnTo("//evil.example/chat")).toBe("/chat");
    expect(normalizeModelSetupReturnTo("/settings")).toBe("/chat");
  });

  it("builds explicit account and model setup routes", () => {
    expect(buildAccountSetupPath("/chat/thread-1")).toBe(
      "/account?setup=watcha&returnTo=%2Fchat%2Fthread-1",
    );
    expect(buildModelsSetupPath("local", "/chat")).toBe(
      "/models?setup=local&returnTo=%2Fchat",
    );
  });

  it("restores a pending draft once", () => {
    persistModelSetupDraft("/chat/thread-1", "  继续刚才的问题  ");
    expect(consumeModelSetupDraft("/chat/thread-1")).toBe("继续刚才的问题");
    expect(consumeModelSetupDraft("/chat/thread-1")).toBe("");
  });

  it("preserves the submitted attachment envelope and task context once", () => {
    persistModelSetupDraftSnapshot("/chat/thread-1", {
      composerText: "请审阅附件",
      submittedInput: [
        {
          role: "user",
          content: [
            { type: "text", text: "请审阅附件" },
            { type: "file", file_id: "file-1", name: "brief.pdf" },
          ],
        },
      ],
      taskContext: {
        agentId: "default",
        selectedSkillName: "research",
        deepThinking: { enabled: true, effort: "high" },
      },
    });

    expect(consumeModelSetupDraftSnapshot("/chat/thread-1")).toMatchObject({
      version: 2,
      returnTo: "/chat/thread-1",
      composerText: "请审阅附件",
      submittedInput: [
        {
          role: "user",
          content: [
            { type: "text", text: "请审阅附件" },
            { type: "file", file_id: "file-1", name: "brief.pdf" },
          ],
        },
      ],
      taskContext: {
        agentId: "default",
        selectedSkillName: "research",
        deepThinking: { enabled: true, effort: "high" },
      },
    });
    expect(consumeModelSetupDraftSnapshot("/chat/thread-1")).toBeNull();
  });

  it("restores attachments into an edited message without duplicating them", () => {
    const restored = [
      {
        role: "user",
        content: [
          { type: "text", text: "原文" },
          { type: "file", file_id: "file-1" },
        ],
      },
    ];
    const current = [{ role: "user", content: "编辑后的文本" }];

    const once = mergeRestoredModelSetupInput(current, restored);
    const twice = mergeRestoredModelSetupInput(once, restored);

    expect(once).toEqual([
      {
        role: "user",
        content: [
          { type: "text", text: "编辑后的文本" },
          { type: "file", file_id: "file-1" },
        ],
      },
    ]);
    expect(twice).toEqual(once);
  });

  it("offers one editable small task without overwriting a user draft", () => {
    expect(ensureFirstModelTaskDraft("/chat/thread-1")).toBe(true);
    const suggested = consumeModelSetupDraftSnapshot("/chat/thread-1");
    expect(suggested?.composerText).toMatch(/会议记录/);

    persistModelSetupDraft("/chat/thread-1", "用户原来的问题");
    expect(ensureFirstModelTaskDraft("/chat/thread-1")).toBe(false);
    expect(consumeModelSetupDraft("/chat/thread-1")).toBe("用户原来的问题");
  });
});
