import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { createRef, memo, useEffect, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  applyRuntimeControlIdentityFrame,
  applyRuntimeProgressFrame,
  chainChatBoundaryWheel,
  chooseActiveSessionMessages,
  ConversationTimeAnchor,
  formatChatTimestamp,
  reconcileActiveSessionProjection,
  type ILantaiRuntimeWebUIRef,
  LantaiRuntimeWebUI,
  mergeRuntimeOutputs,
  scrollMessageListToEnd,
  useChatAnywhereOptions,
  useChatAnywhereSessions,
  useChatAnywhereSessionsState,
  visibleSessionOwnerMatches,
} from "./lantaiChat";
import {
  clearPendingChatMessageNavigation,
  peekPendingChatMessageNavigation,
  requestChatMessageNavigation,
} from "./lantaiChatMessageNavigation";
import LantaiResponseActions from "./lantaiChatResponseActions";

describe("runtime progress control frames", () => {
  it("projects the server-owned initial Run and Attempt identity", () => {
    const sessions = [{ id: "session-1", messages: [] }];

    const next = applyRuntimeControlIdentityFrame(sessions, "session-1", {
      object: "response",
      status: "in_progress",
      metadata: {
        initial_status: true,
        run_id: "run-server-owned",
        turn_receipt: {
          run_id: "run-server-owned",
          attempt_id: "run-server-owned:chat-attempt-1",
        },
      },
    });

    expect(next[0].messages).toEqual([]);
    expect(next[0].runtimeState).toMatchObject({
      run_id: "run-server-owned",
      attempt_id: "run-server-owned:chat-attempt-1",
    });
  });

  it.each([true, false])(
    "updates a new Run's active state only for a live send (generating: %s)",
    (generating) => {
      const sessions = [
        {
          id: "session-1",
          generating,
          messages: [],
          runtimeState: { status: "completed", run_id: "run-previous" },
        },
      ];
      const next = applyRuntimeControlIdentityFrame(sessions, "session-1", {
        object: "response",
        status: "in_progress",
        metadata: {
          initial_status: true,
          run_id: "run-next",
          turn_receipt: {
            run_id: "run-next",
            attempt_id: "run-next:attempt-1",
          },
        },
      });
      expect(next[0].runtimeState).toMatchObject({
        status: generating ? "running" : "completed",
        run_id: "run-next",
      });
      expect(next[0].generating).toBe(generating);
      expect(next[0].messages).toEqual([]);
    },
  );

  it("records transport heartbeats without creating transcript content", () => {
    const sessions = [
      {
        id: "session-1",
        messages: [],
        runtimeState: {
          status: "running",
          run_id: "run-1",
          transport_status: "connecting",
        },
      },
    ];

    const next = applyRuntimeProgressFrame(
      sessions,
      "session-1",
      {
        object: "runtime_progress",
        metadata: {
          structured_keepalive: true,
          last_update_at: "2026-08-28T12:34:30Z",
        },
      },
      "2026-08-28T12:34:31Z",
    );

    expect(next[0].messages).toEqual([]);
    expect(next[0].runtimeState).toMatchObject({
      status: "running",
      run_id: "run-1",
      updated_at: "2026-08-28T12:34:30Z",
      can_reconnect: true,
      can_stop: true,
      transport_status: "connected",
      transport_error: null,
    });
  });

  it("never resurrects a terminal session when a late heartbeat arrives", () => {
    const sessions = [
      {
        id: "session-1",
        generating: false,
        status: "idle",
        messages: [],
        runtimeState: {
          status: "completed",
          run_id: "run-1",
          transport_status: "connected",
        },
      },
    ];

    const next = applyRuntimeProgressFrame(
      sessions,
      "session-1",
      {
        object: "runtime_progress",
        metadata: {
          structured_keepalive: true,
          last_update_at: "2026-08-28T12:34:30Z",
        },
      },
      "2026-08-28T12:34:31Z",
    );

    expect(next[0]).toMatchObject({
      generating: false,
      status: "idle",
      runtimeState: {
        status: "completed",
        run_id: "run-1",
      },
    });
  });
});

function p0ChatIncident(incidentId: string) {
  const library = JSON.parse(
    readFileSync(
      resolve(
        process.cwd(),
        "../tests/fixtures/runtime/incidents/p0_incident_library_v1.json",
      ),
      "utf8",
    ),
  ) as {
    schema_version: number;
    incidents: Array<{
      incident_id: string;
      input: Record<string, unknown>;
      expected: Record<string, unknown>;
    }>;
  };
  expect(library.schema_version).toBe(1);
  return library.incidents.find((item) => item.incident_id === incidentId)!;
}

describe("active session projection isolation", () => {
  const userMessage = (id: string, text: string) => ({
    id,
    role: "user" as const,
    cards: [
      {
        code: "RequestCard",
        data: { input: [{ content: [{ type: "text", text }] }] },
      },
    ],
  });

  it("never carries visible messages across different sessions", () => {
    const incident = p0ChatIncident("chat-cross-talk-v1");
    const visibleMessageIds = incident.input.visible_message_ids as string[];
    const activeMessageIds = incident.input.active_message_ids as string[];
    const expected = incident.expected.projected_message_ids as string[];
    const visibleHello = visibleMessageIds.map((id) =>
      userMessage(id, "你好啊，小伙子"),
    );
    const tokyoHistory = activeMessageIds.map((id) =>
      userMessage(id, "东京行程骨架"),
    );

    expect(
      reconcileActiveSessionProjection(visibleHello, tokyoHistory, false).map(
        (message) => message.id,
      ),
    ).toEqual(expected);
  });

  it("still preserves accepted user messages inside the same session", () => {
    const accepted = [userMessage("accepted-user", "继续")];
    const laggingSnapshot = [userMessage("persisted-user", "上一条")];

    expect(
      reconcileActiveSessionProjection(accepted, laggingSnapshot, true).map(
        (message) => message.id,
      ),
    ).toEqual(["accepted-user", "persisted-user"]);
  });

  it("does not treat a shared provider alias as canonical ownership", () => {
    const visibleOwnerIds = new Set(["session-1", "console:shared-workspace"]);

    expect(visibleSessionOwnerMatches(visibleOwnerIds, "session-1")).toBe(true);
    expect(visibleSessionOwnerMatches(visibleOwnerIds, "session-2")).toBe(
      false,
    );
  });
});

describe("legacy history recovery notice", () => {
  it("surfaces the quarantined source instead of silently showing an empty chat", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          session: {
            api: {
              getSessionList: async () => [
                {
                  id: "legacy-chat-a",
                  messages: [],
                  meta: {
                    state_migration_state: "needs_manual_recovery",
                    legacy_history_notice:
                      "检测到旧版混合历史，已隔离，原始内容仍可恢复",
                    legacy_source_reference: {
                      session_id: "console:legacy-shared-source",
                    },
                  },
                },
              ],
            },
          },
        }}
      />,
    );

    expect(
      await screen.findByText("检测到旧版混合历史，已隔离，原始内容仍可恢复"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("恢复来源：console:legacy-shared-source"),
    ).toBeInTheDocument();
  });

  it("creates an independent copy only after the user chooses recovery", async () => {
    const recoverLegacyHistory = vi.fn(async () => [
      {
        id: "recovered-chat",
        name: "旧会话 · 恢复副本",
        messages: [],
        meta: {
          legacy_recovery_provenance: {
            method: "explicit_copy",
            selection: "all_preserved_messages",
          },
        },
      },
      {
        id: "legacy-chat-a",
        messages: [],
        meta: {
          state_migration_state: "needs_manual_recovery",
        },
      },
    ]);

    render(
      <LantaiRuntimeWebUI
        options={{
          session: {
            api: {
              getSessionList: async () => [
                {
                  id: "legacy-chat-a",
                  messages: [],
                  meta: {
                    state_migration_state: "needs_manual_recovery",
                    legacy_history_notice:
                      "检测到旧版混合历史，已隔离，原始内容仍可恢复",
                    legacy_source_reference: {
                      session_id: "console:legacy-shared-source",
                    },
                  },
                },
              ],
              recoverLegacyHistory,
            },
          },
        }}
      />,
    );

    const recoveryButton = await screen.findByRole("button", {
      name: "恢复为独立副本",
    });
    expect(
      screen.getByText("不会覆盖当前会话或删除原始文件。"),
    ).toBeInTheDocument();
    expect(recoverLegacyHistory).not.toHaveBeenCalled();

    fireEvent.click(recoveryButton);

    await waitFor(() => {
      expect(recoverLegacyHistory).toHaveBeenCalledWith("legacy-chat-a");
      expect(
        screen.queryByText("检测到旧版混合历史，已隔离，原始内容仍可恢复"),
      ).not.toBeInTheDocument();
    });
  });
});

function sharedConsoleStreamControlObjects(): string[] {
  const raw = readFileSync(
    resolve(process.cwd(), "../tests/fixtures/console_stream_protocol_v1.json"),
    "utf8",
  );
  const fixture = JSON.parse(raw) as {
    schema_version: number;
    control_objects: string[];
  };
  expect(fixture.schema_version).toBe(1);
  return fixture.control_objects;
}

describe("conversation time anchors", () => {
  it("shows only the time by default and reveals the full date on click", () => {
    const value = "2026-07-27T12:23:00.000Z";
    render(
      <ConversationTimeAnchor
        value={value}
        locale="zh-CN"
        timeZone="Asia/Shanghai"
      />,
    );

    const anchor = screen.getByRole("button", { name: "20:23" });
    expect(anchor).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(anchor);

    expect(
      screen.getByRole("button", {
        name: "7月27日 星期一 20:23",
      }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("normalizes seconds, milliseconds, and ISO timestamps", () => {
    expect(
      formatChatTimestamp(1785154980, {
        locale: "zh-CN",
        timeZone: "Asia/Shanghai",
      }),
    ).toBe("20:23");
    expect(
      formatChatTimestamp(1785154980000, {
        locale: "zh-CN",
        timeZone: "Asia/Shanghai",
      }),
    ).toBe("20:23");
    expect(
      formatChatTimestamp("2026-07-27T12:23:00Z", {
        expanded: true,
        locale: "zh-CN",
        timeZone: "Asia/Shanghai",
      }),
    ).toBe("7月27日 星期一 20:23");
  });

  it("keeps the persisted legacy wall-clock time", () => {
    expect(
      formatChatTimestamp("2026-07-27 15:09:02.274", {
        locale: "zh-CN",
        timeZone: "Asia/Shanghai",
      }),
    ).toBe("15:09");
  });
});

const responseCardCode = "LantaiRuntimeResponseCard";

describe("runtime output identity", () => {
  it.each(["message.delta", "message.completed"])(
    "merges converter %s narration with its durable history without a mirror marker",
    (eventType) => {
      const message = {
        id: "provider-narration",
        object: "message",
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "text", text: "文件很大，我先看具体内容。" }],
        metadata: {
          source_message_id: "provider-narration",
          client_turn_id: "turn-1",
        },
      };
      const direct = {
        ...message,
        id: "provider-narration:message",
        metadata: {
          source_message_id: "provider-narration:message",
          runtime_item_id: "provider-narration:message",
          runtime_event_type: eventType,
          client_turn_id: "turn-1",
        },
      };
      for (const [first, second] of [
        [direct, message],
        [message, direct],
      ]) {
        const merged = mergeRuntimeOutputs([first] as any, [second] as any);
        expect(merged).toHaveLength(1);
        expect(merged[0].content[0].text).toBe(message.content[0].text);
      }
    },
  );

  it("lets the terminal snapshot repair an exactly repeated live answer", () => {
    const answer = "文件写好了，我现在跑一遍验证。";
    const output = mergeRuntimeOutputs(
      [
        {
          id: "provider-answer",
          object: "message",
          type: "message",
          role: "assistant",
          status: "in_progress",
          content: [{ type: "text", text: answer.repeat(3) }],
          metadata: { source_message_id: "logical-provider-answer" },
        },
      ] as any,
      [
        {
          id: "provider-answer",
          object: "message",
          type: "message",
          role: "assistant",
          status: "completed",
          content: [{ type: "text", text: answer }],
          metadata: { source_message_id: "logical-provider-answer" },
        },
      ] as any,
    );

    expect(output).toHaveLength(1);
    expect(output[0]?.status).toBe("completed");
    expect(output[0]?.content?.[0]?.text).toBe(answer);
  });

  it("does not replace a complete live answer with a shorter terminal prefix", () => {
    const fullAnswer =
      "PACKAGED_MODEL_REPLY: PACKAGED_PROVIDER_STREAM_UI_PROMPT_SWITCH_f57e52c3";
    const shorterSnapshot = fullAnswer.slice(0, -1);
    const output = mergeRuntimeOutputs(
      [
        {
          id: "provider-answer",
          object: "message",
          type: "message",
          role: "assistant",
          status: "in_progress",
          content: [{ type: "text", text: fullAnswer }],
          metadata: { source_message_id: "logical-provider-answer" },
        },
      ] as any,
      [
        {
          id: "provider-answer",
          object: "message",
          type: "message",
          role: "assistant",
          status: "completed",
          content: [{ type: "text", text: shorterSnapshot }],
          metadata: { source_message_id: "logical-provider-answer" },
        },
      ] as any,
    );

    expect(output).toHaveLength(1);
    expect(output[0]?.status).toBe("completed");
    expect(output[0]?.content?.[0]?.text).toBe(fullAnswer);
  });

  it("merges direct SSE and durable ledger aliases by source message id", () => {
    const output = mergeRuntimeOutputs(
      [
        {
          id: "client-turn-1:answer-1:message",
          object: "message",
          type: "message",
          role: "assistant",
          status: "in_progress",
          content: [{ type: "text", text: "RUNTIME_CONFIGURED_PROVIDER_ONE" }],
          metadata: {
            native_runtime_stream: true,
            source_message_id: "answer-1",
          },
        },
      ] as any,
      [
        {
          id: "answer-1",
          object: "message",
          type: "message",
          role: "assistant",
          status: "completed",
          content: [{ type: "text", text: "RUNTIME_CONFIGURED_PROVIDER_ONE" }],
          metadata: { source_message_id: "answer-1" },
        },
      ] as any,
    );

    expect(output).toHaveLength(1);
    expect(output[0]?.status).toBe("completed");
  });

  it("merges live narrative suffix aliases with the durable source id", () => {
    const output = mergeRuntimeOutputs(
      [
        {
          id: "client-turn-12:msg-provider:message",
          object: "message",
          type: "message",
          role: "assistant",
          status: "completed",
          content: [{ type: "text", text: "LAST_VISIBLE_ANSWER" }],
          metadata: {
            native_runtime_stream: true,
            source_message_id: "msg-provider:message",
            client_turn_id: "client-turn-12",
            run_id: "run-live-12",
          },
        },
      ] as any,
      [
        {
          id: "msg-provider:message",
          object: "message",
          type: "message",
          role: "assistant",
          status: "completed",
          content: [{ type: "text", text: "LAST_VISIBLE_ANSWER" }],
          metadata: {
            source_message_id: "msg-provider",
            client_turn_id: "client-turn-12",
          },
        },
      ] as any,
    );

    expect(output).toHaveLength(1);
    expect(output[0]?.metadata?.source_message_id).toBe("msg-provider");
  });

  it("uses a durable narrative id when Ququ source metadata is absent", () => {
    const output = mergeRuntimeOutputs(
      [
        {
          id: "client-turn-12:ququ-provider:message",
          object: "message",
          type: "message",
          role: "assistant",
          status: "completed",
          content: [{ type: "text", text: "~>[echo] DISTINCT_QUQU" }],
          metadata: {
            native_runtime_stream: true,
            source_message_id: "ququ-provider:message",
            client_turn_id: "client-turn-12",
            run_id: "run-live-12",
          },
        },
      ] as any,
      [
        {
          id: "ququ-provider:message",
          object: "message",
          type: "message",
          role: "assistant",
          status: "in_progress",
          content: [{ type: "text", text: "~>[echo] DISTINCT_QUQU" }],
          metadata: { client_turn_id: "client-turn-12" },
        },
      ] as any,
    );

    expect(output).toHaveLength(1);
    expect(output[0]?.status).toBe("completed");
  });

  it("reindexes a direct output when a later frame reveals its source identity", () => {
    const identity = {
      client_turn_id: "client-turn-late-source",
      run_id: "run-late-source",
    };
    const answer = "LATE_SOURCE_IDENTITY_ANSWER";
    const output = mergeRuntimeOutputs([], [
      {
        id: "direct-provider-answer",
        object: "message",
        type: "message",
        role: "assistant",
        status: "in_progress",
        content: [{ type: "text", text: answer }],
        metadata: identity,
      },
      {
        id: "direct-provider-answer",
        object: "message",
        type: "message",
        role: "assistant",
        status: "in_progress",
        content: [{ type: "text", text: answer }],
        metadata: {
          ...identity,
          source_message_id: "logical-provider-answer",
        },
      },
      {
        id: "client-turn-late-source:ledger-provider-answer:message",
        object: "message",
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "text", text: answer }],
        metadata: {
          ...identity,
          native_runtime_stream: true,
          source_message_id: "logical-provider-answer",
        },
      },
    ] as any);

    expect(output).toHaveLength(1);
    expect(output[0]?.status).toBe("completed");
    expect(output[0]?.metadata?.source_message_id).toBe(
      "logical-provider-answer",
    );
  });

  it("keeps transport aliases so a late direct delta cannot recreate a completed answer", () => {
    const identity = {
      client_turn_id: "client-turn-late-delta",
      run_id: "run-late-delta",
    };
    const answer = "RUNTIME_CONFIGURED_PROVIDER_ONE";
    const reconciled = mergeRuntimeOutputs(
      [
        {
          id: "provider-message",
          object: "message",
          type: "message",
          role: "assistant",
          status: "in_progress",
          content: [{ type: "text", text: answer }],
          metadata: {
            ...identity,
            source_message_id: "provider-message",
          },
        },
      ] as any,
      [
        {
          id: "client-turn-late-delta:provider-message:message",
          object: "message",
          type: "message",
          role: "assistant",
          status: "completed",
          content: [{ type: "text", text: answer }],
          metadata: {
            ...identity,
            native_runtime_stream: true,
            source_message_id: "provider-message",
          },
        },
      ] as any,
    );

    expect(reconciled).toHaveLength(1);
    expect(reconciled[0]?.status).toBe("completed");
    expect(reconciled[0]?.metadata?.runtime_output_alias_ids).toEqual(
      expect.arrayContaining([
        "provider-message",
        "client-turn-late-delta:provider-message:message",
      ]),
    );

    const afterLateDelta = mergeRuntimeOutputs([], [
      reconciled[0],
      {
        id: "provider-message",
        object: "message",
        type: "message",
        role: "assistant",
        status: "in_progress",
        content: [{ type: "text", text: answer }],
        metadata: {},
      },
    ] as any);

    expect(afterLateDelta).toHaveLength(1);
    expect(afterLateDelta[0]?.status).toBe("completed");
    expect(afterLateDelta[0]?.content).toEqual(
      expect.arrayContaining([expect.objectContaining({ text: answer })]),
    );
  });

  it("merges a late direct delta when both owners share a suffixed source id", () => {
    const identity = {
      client_turn_id: "client-turn-reverse-race",
      run_id: "run-reverse-race",
    };
    const answer = "RUNTIME_CONFIGURED_PROVIDER_ONE";
    const output = mergeRuntimeOutputs([], [
      {
        id: "client-turn-reverse-race:provider-answer:message",
        object: "message",
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "text", text: answer }],
        metadata: {
          ...identity,
          native_runtime_stream: true,
          source_message_id: "provider-answer:message",
        },
      },
      {
        id: "provider-answer:message",
        object: "message",
        type: "message",
        role: "assistant",
        status: "in_progress",
        content: [{ type: "text", text: answer }],
        metadata: {
          ...identity,
          source_message_id: "provider-answer:message",
        },
      },
    ] as any);

    expect(output).toHaveLength(1);
    expect(output[0]?.status).toBe("completed");
    expect(output[0]?.metadata?.runtime_output_alias_ids).toEqual(
      expect.arrayContaining([
        "client-turn-reverse-race:provider-answer:message",
        "provider-answer:message",
      ]),
    );
  });

  it("drops an explicitly marked echo when it merely repeats the answer", () => {
    const answer = "这是同一条最终回答";
    const output = mergeRuntimeOutputs([], [
      {
        id: "main-answer",
        object: "message",
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "text", text: answer }],
        metadata: { source_message_id: "main-answer" },
      },
      {
        id: "ququ-echo",
        object: "message",
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "text", text: `~>[echo] ${answer}` }],
        metadata: { source_message_id: "ququ-echo" },
      },
    ] as any);

    expect(output).toHaveLength(1);
    expect(output[0]?.id).toBe("main-answer");
  });

  it("reconciles live and durable collaboration cards by tool-call identity", () => {
    const turnMetadata = { client_turn_id: "client-turn-collaboration" };
    const toolRow = (
      id: string,
      type: "plugin_call" | "plugin_call_output",
    ) => ({
      id,
      object: "message",
      type,
      role: type === "plugin_call_output" ? "tool" : "assistant",
      status: "completed",
      content: [
        {
          type: type === "plugin_call" ? "function_call" : "function_result",
          data: {
            call_id: "call-collaborate-1",
            name: "collaborate_with_agents",
          },
        },
      ],
      metadata: turnMetadata,
    });
    const assistant = (id: string, prefix: string) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata: turnMetadata,
            output: [
              toolRow(`${prefix}-started`, "plugin_call"),
              toolRow(`${prefix}-completed`, "plugin_call_output"),
            ],
          },
        },
      ],
    });
    const user = {
      id: "collaboration-user",
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [
              { content: [{ type: "text", text: "并行调查后给我结论" }] },
            ],
          },
        },
      ],
    };

    const messages = chooseActiveSessionMessages(
      [user, assistant("live-collaboration", "live")],
      [user, assistant("durable-collaboration", "durable")],
    );
    const assistants = messages.filter(
      (message) => message.role === "assistant",
    );
    const output = assistants[0]?.cards?.[0]?.data?.output || [];

    expect(assistants).toHaveLength(1);
    expect(output).toHaveLength(2);
    expect(output.map((item: any) => item.type)).toEqual([
      "plugin_call",
      "plugin_call_output",
    ]);
  });

  it("restores the durable interleaved timeline after a live stream groups tools at the end", () => {
    const turnMetadata = { client_turn_id: "client-turn-timeline" };
    const output = (id: string, type: string, text: string) => ({
      id,
      object: "message",
      type,
      role: type === "plugin_call_output" ? "tool" : "assistant",
      status: "completed",
      content: [{ type: "text", text }],
      metadata: {
        ...turnMetadata,
        source_message_id: id,
      },
    });
    const assistant = (id: string, outputs: ReturnType<typeof output>[]) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata: turnMetadata,
            output: outputs,
          },
        },
      ],
    });
    const user = {
      id: "timeline-user",
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: { input: [{ content: [{ type: "text", text: "帮我总结" }] }] },
        },
      ],
    };
    const canonical = [
      output("thinking-1", "reasoning", "先读取文章"),
      output("interim-narration", "message", "我先读取原文并确认结构。"),
      output("tool-call-1", "plugin_call", "guanlan_read"),
      output("tool-result-1", "plugin_call_output", "读取失败"),
      output("thinking-2", "reasoning", "换一种方式"),
      output("tool-call-2", "plugin_call", "web_search"),
      output("tool-result-2", "plugin_call_output", "找到结果"),
      output("final-answer", "message", "这是最终总结"),
      output("ququ-echo", "message", "~>[echo] 这次总算找到了。"),
    ];
    const groupedLive = [
      canonical[0],
      canonical[1],
      canonical[4],
      canonical[7],
      canonical[2],
      canonical[3],
      canonical[5],
      canonical[6],
      canonical[8],
    ];

    const messages = chooseActiveSessionMessages(
      [user, assistant("live-timeline", groupedLive)],
      [user, assistant("durable-timeline", canonical)],
    );
    const response = messages[1]?.cards?.[0]?.data as {
      output?: Array<{ id?: string }>;
    };

    expect(response.output?.map((item) => item.id)).toEqual(
      canonical.map((item) => item.id),
    );
  });
});

function sse(payload: unknown): Uint8Array {
  return new TextEncoder().encode(`data: ${JSON.stringify(payload)}\n\n`);
}

function runningTurnIdentitySse(
  runId = "run-server-owned",
  attemptId = `${runId}:chat-attempt-1`,
): Uint8Array {
  return sse({
    object: "response",
    status: "in_progress",
    metadata: {
      initial_status: true,
      run_id: runId,
      turn_receipt: {
        run_id: runId,
        attempt_id: attemptId,
      },
    },
  });
}

async function selectActiveRuntimeIntent(
  label: "立即调整方向" | "排队下一任务",
) {
  const textarea = document.querySelector<HTMLTextAreaElement>(
    ".lantai-sender textarea",
  );
  expect(textarea).not.toBeNull();
  fireEvent.keyDown(textarea as HTMLTextAreaElement, {
    key: "Enter",
    code: "Enter",
    metaKey: label === "立即调整方向",
    altKey: label === "排队下一任务",
  });
}

function StreamCard({ data }: { data: any }) {
  const rows = Array.isArray(data?.output) ? data.output : [];
  return (
    <div data-testid="stream-output" data-response-status={data?.status}>
      {data?.error?.message ? (
        <section data-kind="error">{data.error.message}</section>
      ) : null}
      {rows.map((row: any) => (
        <section
          key={row.id}
          data-kind={row.type}
          data-status={row.status}
          data-source-message-id={row.metadata?.source_message_id}
          data-client-turn-id={row.metadata?.client_turn_id}
          data-chat-thinking={row.type === "reasoning" ? "1" : undefined}
        >
          {(Array.isArray(row.content) ? row.content : [])
            .map((item: any) => item?.text || "")
            .join("")}
        </section>
      ))}
    </div>
  );
}

function RecoveryStreamCard({ data }: { data: any }) {
  const retryGeneration = useChatAnywhereOptions(
    (options) => options.recovery?.retryGeneration,
  );
  return (
    <div>
      <StreamCard data={data} />
      {data?.error ? (
        <button type="button" onClick={() => retryGeneration?.()}>
          重新生成
        </button>
      ) : null}
    </div>
  );
}

function TurnBoundaryContractCard(props: { data: any }) {
  return (
    <div data-testid="turn-boundary-card">
      <span data-lantai-response-avatar="1">assistant</span>
      <StreamCard data={props.data} />
      <LantaiResponseActions {...props} />
    </div>
  );
}

function PendingAwareStreamCard({ data }: { data: any }) {
  const rows = Array.isArray(data?.output) ? data.output : [];
  if (
    rows.length === 0 &&
    ["created", "in_progress", "running"].includes(String(data?.status || ""))
  ) {
    return (
      <div
        data-testid="assistant-pending"
        data-client-turn-id={data?.metadata?.client_turn_id}
        data-optimistic-pending={String(
          data?.metadata?.optimistic_pending === true,
        )}
      >
        正在思考
      </div>
    );
  }
  return <StreamCard data={data} />;
}

function SessionAwarePendingStreamCard({ data }: { data: any }) {
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  const currentSession = sessions.find(
    (session) => session.id === currentSessionId,
  );
  const rows = Array.isArray(data?.output) ? data.output : [];
  const activeResponse = ["created", "in_progress", "running"].includes(
    String(data?.status || ""),
  );
  if (rows.length === 0 && activeResponse) {
    const sessionGenerating = Boolean(
      currentSession?.generating === true ||
      String(currentSession?.status || "").toLowerCase() === "running",
    );
    const visible = Boolean(
      data?.metadata?.optimistic_pending === true && sessionGenerating,
    );
    return visible ? (
      <div data-testid="session-aware-pending">已接收请求</div>
    ) : null;
  }
  return <StreamCard data={data} />;
}

function DelayedThinkingStreamCard({ data }: { data: any }) {
  const rows = Array.isArray(data?.output) ? data.output : [];
  const hasReasoning = rows.some((row: any) => row.type === "reasoning");
  const [thinkingCommitted, setThinkingCommitted] = useState(false);

  useEffect(() => {
    if (!hasReasoning || thinkingCommitted) return;
    const timer = window.setTimeout(() => setThinkingCommitted(true), 650);
    return () => window.clearTimeout(timer);
  }, [hasReasoning, thinkingCommitted]);

  return (
    <div data-testid="delayed-stream-output">
      {rows.map((row: any) =>
        row.type !== "reasoning" || thinkingCommitted ? (
          <section
            key={row.id}
            data-kind={row.type}
            data-status={row.status}
            data-chat-thinking={row.type === "reasoning" ? "1" : undefined}
          >
            {(Array.isArray(row.content) ? row.content : [])
              .map((item: any) => item?.text || "")
              .join("")}
          </section>
        ) : null,
      )}
    </div>
  );
}

const MemoizedStreamCard = memo(StreamCard);

function PrimaryStreamOwnerProbe() {
  const { sessions, currentSessionId, setSessions } =
    useChatAnywhereSessionsState();
  const active = sessions.find((session) => session.id === currentSessionId);
  return (
    <>
      <output data-testid="primary-stream-owner">
        {String(active?.__primaryStreamActive === true)}
      </output>
      <button
        type="button"
        onClick={() =>
          active &&
          setSessions([{ id: active.id, messages: active.messages || [] }])
        }
      >
        Simulate session refresh
      </button>
    </>
  );
}

function RuntimeLifecycleProbe() {
  const { currentSessionId, setSessions } = useChatAnywhereSessionsState();
  const updateRuntimeStatus = (
    status: "running" | "completed" | "failed" | "interrupted",
  ) => {
    setSessions((previous) =>
      previous.map((session) =>
        session.id === currentSessionId
          ? {
              ...session,
              generating: status === "running",
              status: status === "running" ? "running" : "idle",
              runtimeState: { status },
            }
          : session,
      ),
    );
  };
  return (
    <>
      <button type="button" onClick={() => updateRuntimeStatus("running")}>
        Mark runtime active
      </button>
      <button type="button" onClick={() => updateRuntimeStatus("completed")}>
        Mark runtime complete
      </button>
      <button type="button" onClick={() => updateRuntimeStatus("interrupted")}>
        Mark runtime interrupted
      </button>
      <button type="button" onClick={() => updateRuntimeStatus("failed")}>
        Mark runtime failed
      </button>
    </>
  );
}

function RuntimeIdentityAliasProbe() {
  const { currentSessionId, setSessions } = useChatAnywhereSessionsState();
  const updateRuntimeIdentity = (
    mode:
      | "all"
      | "run-only"
      | "rotated"
      | "new-attempt"
      | "legacy-run"
      | "legacy-tagged",
  ) => {
    setSessions((previous) =>
      previous.map((session) =>
        session.id === currentSessionId
          ? {
              ...session,
              generating: true,
              status: "running",
              runtimeState: {
                status: "running",
                ...(mode === "legacy-run"
                  ? { run_id: "run-legacy-identity-alias-race" }
                  : mode === "legacy-tagged"
                    ? {
                        run_id: "run-legacy-identity-alias-race",
                        attempt_id: "attempt-legacy-identity-alias-race",
                      }
                    : mode === "new-attempt"
                      ? {
                          run_id: "run-new-identity-alias-race",
                          attempt_id: "attempt-new-identity-alias-race",
                          client_turn_id: "turn-new-identity-alias-race",
                        }
                      : mode === "rotated"
                        ? { turn_id: "rotated-turn-identity-alias-race" }
                        : {
                            run_id: "run-identity-alias-race",
                            attempt_id: "attempt-old-identity-alias-race",
                            ...(mode === "all"
                              ? { client_turn_id: "turn-identity-alias-race" }
                              : {}),
                          }),
                can_stop: true,
              },
            }
          : session,
      ),
    );
  };
  return (
    <>
      <button type="button" onClick={() => updateRuntimeIdentity("all")}>
        Mark runtime aliases
      </button>
      <button type="button" onClick={() => updateRuntimeIdentity("run-only")}>
        Replay alternate runtime alias
      </button>
      <button type="button" onClick={() => updateRuntimeIdentity("rotated")}>
        Replay rotated runtime aliases
      </button>
      <button
        type="button"
        onClick={() => updateRuntimeIdentity("new-attempt")}
      >
        Replay newer runtime attempt
      </button>
      <button type="button" onClick={() => updateRuntimeIdentity("legacy-run")}>
        Mark legacy runtime identity
      </button>
      <button
        type="button"
        onClick={() => updateRuntimeIdentity("legacy-tagged")}
      >
        Replay legacy runtime with attempt
      </button>
    </>
  );
}

function RuntimeTransportProbe() {
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  const active = sessions.find((session) => session.id === currentSessionId);
  const runtimeState =
    active?.runtimeState && typeof active.runtimeState === "object"
      ? (active.runtimeState as Record<string, unknown>)
      : {};
  return (
    <output data-testid="runtime-transport-status">
      {String(runtimeState.transport_status || "none")}
    </output>
  );
}

function CurrentSessionProbe() {
  const currentSessionId = useChatAnywhereSessionsState(
    (state) => state.currentSessionId,
  );
  return (
    <output data-testid="current-session-id">
      {currentSessionId || "none"}
    </output>
  );
}

function DoubleCreateSessionButton() {
  const { createSession } = useChatAnywhereSessions();
  return (
    <button
      type="button"
      onClick={() => {
        void createSession();
        void createSession();
      }}
    >
      Create twice
    </button>
  );
}

function CreateSessionButton() {
  const { createSession } = useChatAnywhereSessions();
  return (
    <button type="button" onClick={() => void createSession()}>
      Create new session
    </button>
  );
}

function SessionMetadataPatchButton() {
  const setSessions = useChatAnywhereSessionsState(
    (state) => state.setSessions,
  );
  return (
    <button
      type="button"
      onClick={() =>
        setSessions([
          {
            id: "session-1",
            messages: [
              {
                id: "history-user",
                role: "user",
                cards: [],
              },
            ],
          },
        ])
      }
    >
      Apply metadata patch
    </button>
  );
}

function SessionSwitchButton() {
  const setCurrentSessionId = useChatAnywhereSessionsState(
    (state) => state.setCurrentSessionId,
  );
  return (
    <button type="button" onClick={() => setCurrentSessionId("session-2")}>
      Switch session
    </button>
  );
}

function SessionAliasSwitchButton() {
  const setCurrentSessionId = useChatAnywhereSessionsState(
    (state) => state.setCurrentSessionId,
  );
  return (
    <button
      type="button"
      onClick={() => setCurrentSessionId("runtime-session-1")}
    >
      Switch to runtime alias
    </button>
  );
}

function MultiStageSessionIdentitySwitchButton() {
  const { setSessions, setCurrentSessionId } = useChatAnywhereSessionsState();
  return (
    <button
      type="button"
      onClick={() => {
        setSessions([
          {
            id: "backend-session-1",
            realId: "runtime-session-1",
            sessionId: "session-1",
            messages: [],
          },
        ]);
        setCurrentSessionId("runtime-session-1");
      }}
    >
      Switch through backend identity
    </button>
  );
}

function SwitchToTemporarySessionButton() {
  const { setSessions, setCurrentSessionId } = useChatAnywhereSessionsState();
  return (
    <button
      type="button"
      onClick={() => {
        setSessions([{ id: "session-1", messages: [] }]);
        setCurrentSessionId("session-1");
      }}
    >
      Switch to temporary identity
    </button>
  );
}

function MergeStoppedSessionIdentityButton() {
  const { setSessions, setCurrentSessionId } = useChatAnywhereSessionsState();
  return (
    <button
      type="button"
      onClick={() => {
        setSessions([
          {
            id: "backend-session-1",
            realId: "runtime-session-1",
            sessionId: "session-1",
            messages: [],
            generating: true,
            status: "running",
            runtimeState: {
              status: "running",
              run_id: "run-stopped-before-alias-merge",
              can_stop: true,
            },
          },
        ]);
        setCurrentSessionId("runtime-session-1");
      }}
    >
      Merge stopped session identity
    </button>
  );
}

function SessionDraftSwitchButtons() {
  const setCurrentSessionId = useChatAnywhereSessionsState(
    (state) => state.setCurrentSessionId,
  );
  return (
    <>
      <button type="button" onClick={() => setCurrentSessionId("session-1")}>
        Switch to session one
      </button>
      <button type="button" onClick={() => setCurrentSessionId("session-2")}>
        Switch to session two
      </button>
    </>
  );
}

function ReplaceSessionsButton({
  sessions,
}: {
  sessions: Array<Record<string, unknown>>;
}) {
  const setSessions = useChatAnywhereSessionsState(
    (state) => state.setSessions,
  );
  return (
    <button type="button" onClick={() => setSessions(sessions as any)}>
      Replace sessions
    </button>
  );
}

function RefreshSessionsWithoutPendingAssistantButton() {
  const sessions = useChatAnywhereSessionsState((state) => state.sessions);
  const setSessions = useChatAnywhereSessionsState(
    (state) => state.setSessions,
  );
  return (
    <button
      type="button"
      onClick={() =>
        setSessions(
          sessions.map((session) => ({
            id: session.id,
            sessionId: session.sessionId,
            messages: (session.messages || []).filter(
              (message) => message.role === "user",
            ),
            generating: false,
            status: "idle",
          })),
        )
      }
    >
      Refresh sessions without pending assistant
    </button>
  );
}

function RuntimeRescuePatchButtons() {
  const setSessions = useChatAnywhereSessionsState(
    (state) => state.setSessions,
  );
  return (
    <>
      <button
        type="button"
        onClick={() =>
          setSessions([
            {
              id: "session-1",
              messages: [
                {
                  id: "runtime-rescued-assistant",
                  role: "assistant",
                  msgStatus: "finished",
                  cards: [
                    {
                      code: responseCardCode,
                      data: {
                        id: "runtime-rescued-response",
                        object: "response",
                        status: "completed",
                        output: [
                          {
                            id: "runtime-rescued-output",
                            type: "message",
                            status: "completed",
                            role: "assistant",
                            content: [
                              {
                                type: "text",
                                text: "runtime rescued answer",
                              },
                            ],
                            metadata: { native_runtime_stream: true },
                          },
                        ],
                      },
                    },
                  ],
                },
              ],
            },
          ])
        }
      >
        Apply runtime rescue
      </button>
      <button
        type="button"
        onClick={() =>
          setSessions([
            {
              id: "session-1",
              messages: [
                {
                  id: "history-user-only",
                  role: "user",
                  cards: [],
                },
              ],
            },
          ])
        }
      >
        Apply user-only history
      </button>
    </>
  );
}

function SplitAssistantPatchButton() {
  const setSessions = useChatAnywhereSessionsState(
    (state) => state.setSessions,
  );
  return (
    <button
      type="button"
      onClick={() =>
        setSessions([
          {
            id: "session-1",
            messages: [
              {
                id: "user-1",
                role: "user",
                cards: [],
              },
              {
                id: "assistant-reasoning",
                role: "assistant",
                msgStatus: "finished",
                cards: [
                  {
                    code: responseCardCode,
                    data: {
                      id: "response-reasoning",
                      object: "response",
                      status: "completed",
                      output: [
                        {
                          id: "run-1:reasoning",
                          type: "reasoning",
                          status: "completed",
                          role: "assistant",
                          content: [
                            {
                              type: "text",
                              text: "先判断",
                              status: "completed",
                            },
                          ],
                          metadata: { native_runtime_stream: true },
                        },
                      ],
                      metadata: { native_runtime_stream: true },
                    },
                  },
                ],
              },
              {
                id: "assistant-answer",
                role: "assistant",
                msgStatus: "finished",
                cards: [
                  {
                    code: responseCardCode,
                    data: {
                      id: "response-answer",
                      object: "response",
                      status: "completed",
                      output: [
                        {
                          id: "run-1:message",
                          type: "message",
                          status: "completed",
                          role: "assistant",
                          content: [
                            {
                              type: "text",
                              text: "然后回答",
                              status: "completed",
                            },
                          ],
                        },
                      ],
                    },
                  },
                ],
              },
            ],
          },
        ])
      }
    >
      Apply split assistant patch
    </button>
  );
}

function CompletedReasoningAnswerPatchButton() {
  const setSessions = useChatAnywhereSessionsState(
    (state) => state.setSessions,
  );
  return (
    <button
      type="button"
      onClick={() =>
        setSessions([
          {
            id: "session-1",
            messages: [
              {
                id: "user-1",
                role: "user",
                cards: [],
              },
              {
                id: "assistant-reasoning",
                role: "assistant",
                msgStatus: "finished",
                cards: [
                  {
                    code: responseCardCode,
                    data: {
                      id: "response-reasoning",
                      object: "response",
                      status: "completed",
                      output: [
                        {
                          id: "reasoning-1",
                          type: "reasoning",
                          status: "completed",
                          role: "assistant",
                          content: [
                            {
                              type: "text",
                              text: "先想清楚",
                              status: "completed",
                            },
                          ],
                        },
                      ],
                    },
                  },
                ],
              },
              {
                id: "assistant-answer",
                role: "assistant",
                msgStatus: "finished",
                cards: [
                  {
                    code: responseCardCode,
                    data: {
                      id: "response-answer",
                      object: "response",
                      status: "completed",
                      output: [
                        {
                          id: "answer-1",
                          type: "message",
                          status: "completed",
                          role: "assistant",
                          content: [
                            {
                              type: "text",
                              text: "再回答",
                              status: "completed",
                            },
                          ],
                        },
                      ],
                    },
                  },
                ],
              },
            ],
          },
        ])
      }
    >
      Apply completed reasoning answer patch
    </button>
  );
}

function DistinctTurnPatchButton() {
  const setSessions = useChatAnywhereSessionsState(
    (state) => state.setSessions,
  );
  const assistant = (turnId: string, text: string) => ({
    id: `assistant-${turnId}`,
    role: "assistant",
    msgStatus: "finished",
    cards: [
      {
        code: responseCardCode,
        data: {
          id: `response-${turnId}`,
          object: "response",
          status: "completed",
          output: [
            {
              id: `${turnId}:message`,
              type: "message",
              status: "completed",
              role: "assistant",
              content: [{ type: "text", text, status: "completed" }],
              metadata: {
                native_runtime_stream: true,
                native_runtime_turn_id: turnId,
              },
            },
          ],
          metadata: {
            native_runtime_stream: true,
            native_runtime_turn_id: turnId,
          },
        },
      },
    ],
  });
  return (
    <button
      type="button"
      onClick={() =>
        setSessions([
          {
            id: "session-1",
            messages: [
              { id: "user-1", role: "user", cards: [] },
              { id: "user-2", role: "user", cards: [] },
              assistant("turn-1", "第一轮回复"),
              assistant("turn-2", "第二轮回复"),
            ],
          },
        ])
      }
    >
      Apply distinct turns
    </button>
  );
}

describe("LantaiRuntimeWebUI local stream shim", () => {
  it("keeps composer capability and input tools in visual keyboard order", () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          sender: {
            prefix: <button aria-label="能力设置">能力</button>,
            beforeAttachments: <button aria-label="前置工具">前置</button>,
            afterAttachments: <button aria-label="上下文状态">状态</button>,
            attachments: {
              trigger: () => <button aria-label="添加附件">附件</button>,
            },
          },
        }}
      />,
    );

    const capability = screen.getByRole("button", { name: "能力设置" });
    const leadingTool = screen.getByRole("button", { name: "前置工具" });
    const attachment = screen.getByRole("button", { name: "添加附件" });
    const context = screen.getByRole("button", { name: "上下文状态" });
    const toolGroup = leadingTool.closest(".lantai-sender-input-tools");

    expect(toolGroup).not.toBeNull();
    expect(attachment.closest(".lantai-sender-input-tools")).toBe(toolGroup);
    expect(context.closest(".lantai-sender-input-tools")).toBe(toolGroup);
    expect(
      capability.compareDocumentPosition(leadingTool) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      leadingTool.compareDocumentPosition(attachment) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      attachment.compareDocumentPosition(context) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("rejects imperative message writes owned by a background session", async () => {
    const chatRef = createRef<ILantaiRuntimeWebUIRef>();
    const sessions = [
      { id: "session-1", realId: "chat-1", messages: [] },
      { id: "session-2", realId: "chat-2", messages: [] },
    ];
    render(
      <LantaiRuntimeWebUI
        ref={chatRef}
        options={{
          theme: { rightHeader: <SessionSwitchButton /> },
          session: {
            api: {
              getSessionList: async () => sessions,
              getSession: async (sessionId: string) =>
                sessions.find((session) => session.id === sessionId)!,
            },
          },
        }}
      />,
    );

    await waitFor(() => expect(chatRef.current).not.toBeNull());
    const progress = {
      id: "transient-response",
      object: "response",
      status: "in_progress",
      output: [],
    } as any;
    expect(
      chatRef.current?.messages.upsertTransientAssistantResponse(
        ["session-1", "chat-1"],
        "progress-session-1",
        progress,
      ),
    ).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Switch session" }));
    await waitFor(() =>
      expect(
        chatRef.current?.messages.upsertTransientAssistantResponse(
          ["session-1", "chat-1"],
          "stale-progress-session-1",
          progress,
        ),
      ).toBe(false),
    );
    expect(
      chatRef.current?.messages.removeAllMessages(["session-1", "chat-1"]),
    ).toBe(false);
  });

  it("renders a turn navigator and exposes the same precise message jump", async () => {
    const requestCardCode = "LantaiRuntimeRequestCard";
    const history = Array.from({ length: 6 }, (_, index) => ({
      id: `navigator-user-${index + 1}`,
      role: "user",
      cards: [
        {
          code: requestCardCode,
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  {
                    type: "text",
                    text: `导航问题 ${index + 1}`,
                  },
                ],
              },
            ],
          },
        },
      ],
    }));
    const chatRef = createRef<ILantaiRuntimeWebUIRef>();

    render(
      <LantaiRuntimeWebUI
        ref={chatRef}
        options={{
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: history },
              ],
              getSession: async () => ({
                id: "session-1",
                messages: history,
              }),
            },
          },
        }}
      />,
    );

    await waitFor(() =>
      expect(screen.getAllByText("导航问题 6").length).toBeGreaterThan(0),
    );
    const list = document.querySelector<HTMLDivElement>(
      ".lantai-chat-anywhere-message-list",
    )!;
    const scrollTo = vi.fn();
    Object.defineProperties(list, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 2_400 },
      scrollTop: { configurable: true, writable: true, value: 0 },
      scrollTo: { configurable: true, value: scrollTo },
    });
    Array.from(
      list.querySelectorAll<HTMLElement>("[data-lantai-message-id]"),
    ).forEach((node, index) => {
      Object.defineProperty(node, "offsetTop", {
        configurable: true,
        value: index * 400,
      });
      vi.spyOn(node, "getBoundingClientRect").mockReturnValue({
        top: index * 400,
        bottom: index * 400 + 40,
        left: 120,
        right: 720,
        width: 600,
        height: 40,
        x: 120,
        y: index * 400,
        toJSON: () => ({}),
      });
    });
    vi.spyOn(list, "getBoundingClientRect").mockReturnValue({
      top: 0,
      bottom: 600,
      left: 0,
      right: 900,
      width: 900,
      height: 600,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    fireEvent.scroll(list);
    await waitFor(() =>
      expect(
        screen.getByRole("navigation", { name: "会话定位" }),
      ).toHaveAttribute("data-visible", "true"),
    );

    fireEvent.click(
      screen.getByRole("button", {
        name: "第 4 轮：导航问题 4",
      }),
    );
    expect(scrollTo).toHaveBeenLastCalledWith({
      top: 1_080,
      behavior: "smooth",
    });

    scrollTo.mockClear();
    expect(
      chatRef.current?.navigation.scrollToMessage("navigator-user-5"),
    ).toBe(true);
    expect(scrollTo).toHaveBeenLastCalledWith({
      top: 1_480,
      behavior: "smooth",
    });

    scrollTo.mockClear();
    requestChatMessageNavigation({
      chatId: "session-1",
      messageId: "navigator-user-2",
      source: "search",
    });
    await waitFor(() =>
      expect(scrollTo).toHaveBeenLastCalledWith({
        top: 280,
        behavior: "smooth",
      }),
    );
    expect(peekPendingChatMessageNavigation()).toBeNull();
    clearPendingChatMessageNavigation();
  });

  it("auto-follows without smooth-scroll contention and respects users reading history", () => {
    const list = document.createElement("div");
    const scrollTo = vi.fn();
    Object.defineProperties(list, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 1_800 },
      scrollTop: { configurable: true, writable: true, value: 1_100 },
      scrollTo: { configurable: true, value: scrollTo },
    });

    expect(scrollMessageListToEnd(list)).toBe(true);
    expect(scrollTo).toHaveBeenCalledWith({
      top: 1_800,
      behavior: "auto",
    });

    scrollTo.mockClear();
    list.scrollTop = 200;

    expect(scrollMessageListToEnd(list)).toBe(false);
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("does not fight an upward wheel gesture while a response is streaming", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "滚动期间继续输出" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    await act(
      () =>
        new Promise<void>((resolve) =>
          window.requestAnimationFrame(() =>
            window.requestAnimationFrame(() => resolve()),
          ),
        ),
    );

    const list = document.querySelector<HTMLDivElement>(
      ".lantai-chat-anywhere-message-list",
    )!;
    const scrollTo = vi.fn();
    Object.defineProperties(list, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 1_800 },
      scrollTop: { configurable: true, writable: true, value: 1_200 },
      scrollTo: { configurable: true, value: scrollTo },
    });

    fireEvent.wheel(list, { deltaY: -40 });
    list.scrollTop = 1_160;
    scrollTo.mockClear();

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-scroll",
          type: "message",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "answer-scroll",
          delta: true,
          text: "新内容",
          status: "in_progress",
        }),
      );
    });

    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent("新内容"),
    );
    await act(
      () =>
        new Promise<void>((resolve) =>
          window.requestAnimationFrame(() => resolve()),
        ),
    );
    expect(scrollTo).not.toHaveBeenCalled();
    expect(list.scrollTop).toBe(1_160);

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-scroll",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "新内容" }],
        }),
      );
      streamController!.enqueue(
        sse({ object: "response", id: "response-scroll", status: "completed" }),
      );
      streamController!.close();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
  });

  it("hands a boundary wheel gesture from a process surface to the message list", () => {
    const messageList = document.createElement("div");
    const processSurface = document.createElement("div");
    processSurface.dataset.chatScrollChain = "1";
    processSurface.append(document.createElement("span"));
    Object.defineProperties(messageList, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 1_800 },
      scrollTop: { configurable: true, writable: true, value: 300 },
    });
    Object.defineProperties(processSurface, {
      clientHeight: { configurable: true, value: 240 },
      scrollHeight: { configurable: true, value: 960 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    });

    expect(
      chainChatBoundaryWheel(
        processSurface.firstElementChild,
        -140,
        messageList,
      ),
    ).toBe(true);
    expect(messageList.scrollTop).toBe(160);

    messageList.scrollTop = 300;
    processSurface.scrollTop = 120;

    expect(
      chainChatBoundaryWheel(
        processSurface.firstElementChild,
        -140,
        messageList,
      ),
    ).toBe(false);
    expect(messageList.scrollTop).toBe(300);
  });

  it("preserves accepted user cards when a runtime snapshot omits one", () => {
    const user = (id: string, text: string) => ({
      id,
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [{ content: [{ type: "text", text }] }],
          },
        },
      ],
    });
    const firstUser = user("user-1", "第一条不能丢");
    const secondUser = user("user-2", "第二条仍在");
    const secondReply = {
      id: "assistant-2",
      role: "assistant" as const,
      cards: [],
    };

    const messages = chooseActiveSessionMessages(
      [firstUser, secondUser],
      [secondUser, secondReply],
    );

    expect(messages.map((message) => message.id)).toEqual([
      "user-1",
      "user-2",
      "assistant-2",
    ]);
  });

  it("replaces optimistic cards with their persisted equivalents without duplicating a route transcript", () => {
    const user = (
      id: string,
      text: string,
      options: { persisted?: boolean; clientTurnId?: string } = {},
    ) => ({
      id,
      role: "user" as const,
      metadata: options.clientTurnId
        ? { client_turn_id: options.clientTurnId }
        : undefined,
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  options.persisted
                    ? {
                        type: "input_text",
                        text,
                        status: "completed",
                        metadata: { source: "backend" },
                      }
                    : { type: "text", text, status: "created" },
                ],
              },
            ],
          },
        },
      ],
    });
    const current = [
      user("optimistic-1", "第一条", { clientTurnId: "turn-1" }),
      { id: "optimistic-answer-1", role: "assistant" as const, cards: [] },
      user("optimistic-2", "第二条", { clientTurnId: "turn-2" }),
      { id: "optimistic-answer-2", role: "assistant" as const, cards: [] },
    ];
    const incoming = [
      user("persisted-1", "第一条", {
        persisted: true,
        clientTurnId: "turn-1",
      }),
      { id: "persisted-answer-1", role: "assistant" as const, cards: [] },
      user("persisted-2", "第二条", {
        persisted: true,
        clientTurnId: "turn-2",
      }),
      { id: "persisted-answer-2", role: "assistant" as const, cards: [] },
    ];

    expect(
      chooseActiveSessionMessages(current, incoming).map(
        (message) => message.id,
      ),
    ).toEqual(incoming.map((message) => message.id));
  });

  it("drops a stale optimistic waiting card after route hydration proves the turn completed", () => {
    const user = {
      id: "user-route-return",
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [{ content: [{ type: "text", text: "网页版，A" }] }],
          },
        },
      ],
    };
    const completedAssistant = {
      id: "assistant-route-return",
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: "response-route-return",
            object: "response",
            status: "completed",
            metadata: { client_turn_id: "user-route-return" },
            output: [
              {
                id: "answer-route-return",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: "游戏做完了。" }],
                metadata: { client_turn_id: "user-route-return" },
              },
            ],
          },
        },
      ],
    };
    const staleOptimisticAssistant = {
      id: "assistant-stale-optimistic",
      role: "assistant" as const,
      msgStatus: "loading",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: "response-stale-optimistic",
            object: "response",
            status: "in_progress",
            metadata: { optimistic_pending: true },
            output: [],
          },
        },
      ],
    };

    const messages = chooseActiveSessionMessages(
      [user, completedAssistant, staleOptimisticAssistant],
      [user, completedAssistant],
    );

    expect(messages.map((message) => message.id)).toEqual([
      "user-route-return",
      "assistant-route-return",
    ]);
  });

  it("keeps a real optimistic waiting card when the latest user has no durable reply", () => {
    const user = (id: string, text: string, clientTurnId: string) => ({
      id,
      role: "user" as const,
      metadata: { client_turn_id: clientTurnId },
      cards: [
        {
          code: "RequestCard",
          data: { input: [{ content: [{ type: "text", text }] }] },
        },
      ],
    });
    const previousAssistant = {
      id: "assistant-previous",
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: "response-previous",
            object: "response",
            status: "completed",
            output: [
              {
                id: "answer-previous",
                type: "message",
                status: "completed",
                content: [{ type: "text", text: "上一轮回答" }],
              },
            ],
          },
        },
      ],
    };
    const optimisticAssistant = {
      id: "assistant-new-optimistic",
      role: "assistant" as const,
      msgStatus: "loading",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: "response-new-optimistic",
            object: "response",
            status: "in_progress",
            metadata: { optimistic_pending: true },
            output: [],
          },
        },
      ],
    };
    const previousUser = user("user-previous", "上一轮");
    const latestUser = user("user-latest", "新问题");
    const current = [
      previousUser,
      previousAssistant,
      latestUser,
      optimisticAssistant,
    ];
    const incoming = [previousUser, previousAssistant, latestUser];

    expect(chooseActiveSessionMessages(current, incoming)).toEqual(current);
  });

  it("does not regress a completed image result to a stale in-progress snapshot", () => {
    const assistant = (
      status: "completed" | "in_progress",
      outputStatus: "completed" | "in_progress",
    ) => ({
      id: "assistant-image",
      role: "assistant" as const,
      msgStatus: status === "completed" ? "finished" : "loading",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: "image-response",
            object: "response",
            status,
            metadata: { native_runtime_turn_id: "image-turn-1" },
            output: [
              {
                id: "image-tool-output",
                type: "tool",
                status: outputStatus,
                role: "assistant",
                content:
                  outputStatus === "completed"
                    ? [
                        {
                          type: "image",
                          image_url: "/files/generated/jing-ye-si.png",
                        },
                      ]
                    : [],
                metadata: { native_runtime_turn_id: "image-turn-1" },
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages(
      [assistant("completed", "completed")],
      [assistant("in_progress", "in_progress")],
    );
    const response = messages[0]?.cards?.[0]?.data as {
      status?: string;
      output?: Array<{ status?: string; content?: unknown[] }>;
    };

    expect(response.status).toBe("completed");
    expect(response.output?.[0]?.status).toBe("completed");
    expect(response.output?.[0]?.content).toHaveLength(1);
  });

  it("reconciles one streamed answer with its differently identified persisted transcript", () => {
    const user = {
      id: "user-semantic-reconcile",
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [{ content: [{ type: "text", text: "请回答" }] }],
          },
        },
      ],
    };
    const assistant = (
      id: string,
      turnId: string,
      streamed: boolean,
      includeReasoning: boolean,
    ) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata: {
              native_runtime_turn_id: turnId,
              ...(streamed ? { native_runtime_stream: true } : {}),
            },
            output: [
              ...(includeReasoning
                ? [
                    {
                      id: `${id}-reasoning`,
                      type: "reasoning",
                      status: "completed",
                      role: "assistant",
                      content: [{ type: "reasoning", text: "先想清楚" }],
                      metadata: {
                        source_message_id: "semantic-reconcile-reasoning",
                      },
                    },
                  ]
                : []),
              {
                id: `${id}-answer`,
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: "同一轮最终回答" }],
                metadata: {
                  source_message_id: "semantic-reconcile-answer",
                },
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages(
      [user, assistant("live", "live-turn", true, true)],
      [user, assistant("persisted", "persisted-run", false, true)],
    );
    const response = messages[1]?.cards?.[0]?.data as {
      output?: Array<{ type?: string; content?: Array<{ text?: string }> }>;
    };

    expect(messages).toHaveLength(2);
    expect(response.output?.map((item) => item.type)).toEqual([
      "reasoning",
      "message",
    ]);
    expect(
      response.output?.filter(
        (item) => item.content?.[0]?.text === "同一轮最终回答",
      ),
    ).toHaveLength(1);
  });

  it("preserves two same-text outputs when their source identities differ", () => {
    const assistant = (sourceId: string) => ({
      id: `assistant-${sourceId}`,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `response-${sourceId}`,
            object: "response",
            status: "completed",
            metadata: { client_turn_id: "client-turn-two-sources" },
            output: [
              {
                id: `output-${sourceId}`,
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: "我在。" }],
                metadata: { source_message_id: sourceId },
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages([
      assistant("source-a"),
      assistant("source-b"),
    ]);
    const output = messages[0]?.cards?.[0]?.data?.output || [];

    expect(messages).toHaveLength(1);
    expect(output).toHaveLength(2);
    expect(output.map((item: any) => item.metadata?.source_message_id)).toEqual(
      ["source-a", "source-b"],
    );
  });

  it("replaces one live narrative slot with its durable version for the same client turn", () => {
    const assistant = (
      id: string,
      text: string,
      metadata: Record<string, unknown>,
      outputMetadata: Record<string, unknown>,
    ) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata,
            output: [
              {
                id: `${id}-answer`,
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text }],
                metadata: outputMetadata,
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages([
      assistant(
        "live",
        "实时部分",
        { client_turn_id: "client-turn-2" },
        {
          client_turn_id: "client-turn-2",
          source_message_id: "client-turn-2-answer",
        },
      ),
      assistant(
        "persisted",
        "落库完整",
        { run_id: "shared-queue-run" },
        {
          client_turn_id: "client-turn-2",
          source_message_id: "client-turn-2-answer",
        },
      ),
    ]);
    const response = messages[0]?.cards?.[0]?.data as {
      output?: Array<{ content?: Array<{ text?: string }> }>;
    };

    expect(messages).toHaveLength(1);
    expect(
      response.output?.flatMap((item) =>
        (item.content || []).map((content) => content.text),
      ),
    ).toEqual(["落库完整"]);
  });

  it("does not merge reasoning from one queued turn into the next turn answer", () => {
    const assistant = (
      id: string,
      turnId: string,
      type: "reasoning" | "message",
      text: string,
    ) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata: {
              client_turn_id: turnId,
              run_id: "shared-queue-run",
            },
            output: [
              {
                id: `${id}-${type}`,
                type,
                status: "completed",
                role: "assistant",
                content: [
                  {
                    type: type === "reasoning" ? "reasoning" : "text",
                    text,
                  },
                ],
                metadata: {
                  client_turn_id: turnId,
                  run_id: "shared-queue-run",
                },
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages([
      assistant("turn-2-thinking", "client-turn-2", "reasoning", "第二轮思考"),
      assistant("turn-3-answer", "client-turn-3", "message", "第三轮答案"),
    ]);

    expect(messages).toHaveLength(2);
    expect(
      messages.map((message) =>
        (message.cards?.[0]?.data?.output || [])
          .flatMap((item: any) => item.content || [])
          .map((item: any) => item.text)
          .join(""),
      ),
    ).toEqual(["第二轮思考", "第三轮答案"]);
  });

  it("reconciles three queued turns one-to-one when durable history includes a final echo", () => {
    const user = (id: string, text: string) => ({
      id,
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [{ content: [{ type: "text", text }] }],
          },
        },
      ],
    });
    const assistant = (
      id: string,
      clientTurnId: string,
      answer: string,
      streamed: boolean,
      echo = "",
    ) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata: {
              client_turn_id: clientTurnId,
              ...(streamed ? { native_runtime_stream: true } : {}),
            },
            output: [
              {
                id: `${id}-answer`,
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: answer }],
                metadata: {
                  client_turn_id: clientTurnId,
                  source_message_id: `${clientTurnId}:answer`,
                },
              },
              ...(echo
                ? [
                    {
                      id: `${id}-echo`,
                      type: "message",
                      status: "completed",
                      role: "assistant",
                      content: [{ type: "text", text: echo }],
                      metadata: {
                        client_turn_id: clientTurnId,
                        source_message_id: `${clientTurnId}:echo`,
                      },
                    },
                  ]
                : []),
            ],
          },
        },
      ],
    });
    const current = [
      user("live-user-1", "继续"),
      assistant("live-assistant-1", "client-turn-1", "第一条回答", true),
      user("live-user-2", "继续"),
      assistant("live-assistant-2", "client-turn-2", "第二条回答", true),
      user("live-user-3", "继续"),
      assistant("live-assistant-3", "client-turn-3", "第三条回答", true),
    ];
    const incoming = [
      user("persisted-user-1", "继续"),
      assistant("persisted-assistant-1", "client-turn-1", "第一条回答", false),
      user("persisted-user-2", "继续"),
      assistant("persisted-assistant-2", "client-turn-2", "第二条回答", false),
      user("persisted-user-3", "继续"),
      assistant(
        "persisted-assistant-3",
        "client-turn-3",
        "第三条回答",
        false,
        "鹭鹭补充",
      ),
    ];

    const messages = chooseActiveSessionMessages(current, incoming);
    const assistants = messages.filter(
      (message) => message.role === "assistant",
    );
    const texts = assistants.flatMap((message) => {
      const data = message.cards?.[0]?.data as {
        output?: Array<{ content?: Array<{ text?: string }> }>;
      };
      return (data.output || []).flatMap((output) =>
        (output.content || []).map((content) => content.text || ""),
      );
    });

    expect(messages).toHaveLength(6);
    expect(assistants).toHaveLength(3);
    expect(texts.filter((text) => text === "第二条回答")).toHaveLength(1);
    expect(texts.filter((text) => text === "第三条回答")).toHaveLength(1);
    expect(texts.filter((text) => text === "鹭鹭补充")).toHaveLength(1);
  });

  it("does not let a shorter terminal snapshot truncate a completed live answer", () => {
    const fullAnswer =
      "PACKAGED_MODEL_REPLY: PACKAGED_PROVIDER_STREAM_UI_PROMPT_SWITCH_f57e52c3";
    const shorterSnapshot = fullAnswer.slice(0, -1);
    const user = {
      id: "route-switch-user",
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: { input: [{ content: [{ type: "text", text: "继续" }] }] },
        },
      ],
    };
    const assistant = (text: string, streamed: boolean) => ({
      id: "route-switch-assistant",
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: "route-switch-response",
            object: "response",
            status: "completed",
            metadata: {
              client_turn_id: "route-switch-turn",
              ...(streamed ? { native_runtime_stream: true } : {}),
            },
            output: [
              {
                id: "route-switch-answer",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text }],
                metadata: {
                  client_turn_id: "route-switch-turn",
                  source_message_id: "route-switch-provider-answer",
                  ...(streamed ? { native_runtime_stream: true } : {}),
                },
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages(
      [user, assistant(fullAnswer, true)],
      [user, assistant(shorterSnapshot, false)],
    );
    const response = messages.find((message) => message.role === "assistant")
      ?.cards?.[0]?.data as {
      output?: Array<{ content?: Array<{ text?: string }> }>;
    };

    expect(response.output?.[0]?.content?.[0]?.text).toBe(fullAnswer);
  });

  it("anchors a lagging durable snapshot to queued user turns when runtime aliases differ", () => {
    const user = (id: string, text: string) => ({
      id,
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: { input: [{ content: [{ type: "text", text }] }] },
        },
      ],
    });
    const assistant = (
      id: string,
      answer: string,
      metadata: Record<string, unknown>,
      streamed = false,
    ) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata: {
              ...metadata,
              ...(streamed ? { native_runtime_stream: true } : {}),
            },
            output: [
              {
                id: `${id}-answer`,
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: answer }],
                metadata,
              },
            ],
          },
        },
      ],
    });
    const current = [
      user("live-user-1", "继续"),
      assistant(
        "live-assistant-1",
        "第一条回答正在完成",
        { client_turn_id: "client-1", run_id: "shared-run" },
        true,
      ),
      user("live-user-2", "继续"),
      assistant(
        "live-assistant-2",
        "第二条回答正在完成",
        { client_turn_id: "client-2", run_id: "shared-run" },
        true,
      ),
      user("live-user-3", "继续"),
      assistant(
        "live-assistant-3",
        "第三条回答",
        { client_turn_id: "client-3", run_id: "shared-run" },
        true,
      ),
    ];
    // The durable projection is intentionally one turn behind and only knows
    // the transport run id. Text also differs from the live token snapshot,
    // so neither exact identity nor exact-answer matching can reconcile it.
    const incoming = [
      user("persisted-user-1", "继续"),
      assistant("persisted-assistant-1", "第一条回答已完成", {
        run_id: "shared-run",
      }),
      user("persisted-user-2", "继续"),
      assistant("persisted-assistant-2", "第二条回答已完成", {
        run_id: "shared-run",
      }),
    ];

    const messages = chooseActiveSessionMessages(current, incoming);
    expect(
      messages.filter((message) => message.role === "assistant"),
    ).toHaveLength(3);
    expect(messages.filter((message) => message.role === "user")).toHaveLength(
      3,
    );
  });

  it("does not move later guidance above an already visible running turn", () => {
    const user = (id: string, text: string) => ({
      id,
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: { input: [{ content: [{ type: "text", text }] }] },
        },
      ],
    });
    const assistant = (id: string, text: string, status: string) => ({
      id,
      role: "assistant" as const,
      msgStatus: status === "completed" ? "finished" : "loading",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status,
            metadata: { client_turn_id: "turn-1" },
            output: [
              {
                id: `${id}-answer`,
                type: "message",
                status,
                role: "assistant",
                content: [{ type: "text", text }],
                metadata: { client_turn_id: "turn-1" },
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages(
      [
        user("local-user-1", "先整理这个文档"),
        assistant("local-running", "正在读取附件", "in_progress"),
        user("local-user-2", "把客户偏好也列出来"),
        user("local-user-3", "关联人物别漏掉"),
      ],
      [
        user("persisted-user-1", "先整理这个文档"),
        user("persisted-user-2", "把客户偏好也列出来"),
        user("persisted-user-3", "关联人物别漏掉"),
        assistant("persisted-complete", "已开始逐表提取", "completed"),
      ],
    );

    expect(messages.map((message) => message.role)).toEqual([
      "user",
      "assistant",
      "user",
      "user",
    ]);
    const output = messages[1]?.cards?.[0]?.data?.output as Array<{
      content?: Array<{ text?: string }>;
    }>;
    expect(output[0]?.content?.[0]?.text).toBe("已开始逐表提取");
  });

  it("drops completed duplicate user cards when a full session reload arrives", () => {
    const user = (id: string, text: string, clientTurnId: string) => ({
      id,
      role: "user" as const,
      metadata: { client_turn_id: clientTurnId },
      cards: [
        {
          code: "RequestCard",
          data: {
            metadata: { client_turn_id: clientTurnId },
            input: [
              {
                role: "user",
                type: "message",
                content: [{ type: "text", text }],
                metadata: { client_turn_id: clientTurnId },
              },
            ],
          },
        },
      ],
    });
    const completedAssistant = (id: string) => ({
      id,
      role: "assistant" as const,
      cards: [],
    });
    const incoming = [
      user("msg-first", "第一条", "turn-first"),
      completedAssistant("backend-answer-first"),
      user("msg-second", "第二条", "turn-second"),
      completedAssistant("backend-answer-second"),
    ];
    const current = [
      user("msg-first", "第一条", "turn-first"),
      user("msg-second", "第二条", "turn-second"),
      completedAssistant("native-answer-first"),
      user("user-first", "第一条", "turn-first"),
      user("user-second", "第二条", "turn-second"),
      completedAssistant("native-answer-second"),
    ];

    expect(
      chooseActiveSessionMessages(current, incoming).map(
        (message) => message.id,
      ),
    ).toEqual(incoming.map((message) => message.id));
  });

  it("reconciles an attachment turn by client identity when URLs change", () => {
    const user = (id: string, clientTurnId: string, fileUrl: string) => ({
      id,
      role: "user" as const,
      metadata: { client_turn_id: clientTurnId },
      cards: [
        {
          code: "RequestCard",
          data: {
            client_turn_id: clientTurnId,
            input: [
              {
                role: "user",
                type: "message",
                metadata: { client_turn_id: clientTurnId },
                content: [
                  { type: "text", text: "根据这个内容做PPT。" },
                  {
                    type: "file",
                    file_name: "brief.docx",
                    file_url: fileUrl,
                  },
                ],
              },
            ],
          },
        },
      ],
    });
    const current = [
      user("optimistic-user", "client-attachment-1", "blob:local-preview"),
    ];
    const incoming = [
      user(
        "persisted-user",
        "client-attachment-1",
        "/files/preview/workspace/brief.docx",
      ),
    ];

    const reconciled = chooseActiveSessionMessages(current, incoming);

    expect(reconciled).toHaveLength(1);
    expect(reconciled[0]?.role).toBe("user");
  });

  it("lets canonical history repair an assistant-before-prompt live race", () => {
    const clientTurnId = "user-csv-turn";
    const user = (id: string) => ({
      id,
      role: "user" as const,
      metadata: { client_turn_id: clientTurnId },
      cards: [
        {
          code: "RequestCard",
          data: {
            client_turn_id: clientTurnId,
            input: [
              {
                role: "user",
                content: [{ type: "text", text: "生成多个文件" }],
              },
            ],
          },
        },
      ],
    });
    const assistant = (id: string, canonical = false) => ({
      id,
      role: "assistant" as const,
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            status: "completed",
            metadata: {
              client_turn_id: clientTurnId,
              ...(canonical ? { canonical_history_timeline: true } : {}),
            },
            output: [],
          },
        },
      ],
    });

    const reconciled = chooseActiveSessionMessages(
      [assistant("live-answer"), user("optimistic-user")],
      [user("persisted-user"), assistant("persisted-answer", true)],
    );

    expect(reconciled.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
  });

  it("removes duplicate prompt cards from the same accepted client turn", () => {
    const clientTurnId = "user-archive-duplicate";
    const user = (id: string) => ({
      id,
      role: "user" as const,
      metadata: { client_turn_id: clientTurnId },
      cards: [
        {
          code: "RequestCard",
          data: {
            client_turn_id: clientTurnId,
            input: [{ content: [{ type: "text", text: "同一条 prompt" }] }],
          },
        },
      ],
    });
    const assistant = {
      id: "answer",
      role: "assistant" as const,
      cards: [],
    };

    const reconciled = chooseActiveSessionMessages([
      user("optimistic-user"),
      assistant,
      user("duplicate-user"),
    ]);

    expect(reconciled.map((message) => message.id)).toEqual([
      "optimistic-user",
      "answer",
    ]);
  });

  it("collapses a settings-return cache mirror into the richer optimistic attachment turn", () => {
    const clientTurnId = "user-1788879036222-7da24ren";
    const optimistic = {
      id: clientTurnId,
      role: "user" as const,
      metadata: { client_turn_id: clientTurnId },
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [
              {
                role: "user",
                content: [
                  { type: "text", text: "仔细审查这个游记" },
                  {
                    type: "file",
                    file_name: "乐业天坑五日行程单.md",
                    file_url: "blob:http://localhost/itinerary",
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    const restoredCacheMirror = {
      id: `client-turn:${clientTurnId}`,
      role: "user" as const,
      metadata: { client_turn_id: clientTurnId },
      cards: [
        {
          code: "RequestCard",
          data: {
            client_turn_id: clientTurnId,
            input: [
              {
                role: "user",
                content: [{ type: "text", text: "仔细审查这个游记" }],
              },
            ],
          },
        },
      ],
    };

    const projection = chooseActiveSessionMessages(
      [optimistic],
      [
        restoredCacheMirror,
        {
          id: "runtime-progress",
          role: "assistant" as const,
          cards: [],
        },
      ],
    );

    expect(
      projection.filter((message) => message.role === "user"),
    ).toHaveLength(1);
    expect(projection[0]?.id).toBe(clientTurnId);
    expect(projection[0]?.cards?.[0]?.data?.input?.[0]?.content).toEqual(
      expect.arrayContaining([expect.objectContaining({ type: "file" })]),
    );
  });

  it("reconciles an attachment turn after durable history loses the client identity", () => {
    const optimistic = {
      id: "user-optimistic-attachment",
      role: "user" as const,
      metadata: { client_turn_id: "user-optimistic-attachment" },
      cards: [
        {
          code: "RequestCard",
          data: {
            client_turn_id: "user-optimistic-attachment",
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  { type: "text", text: "这个太逗了" },
                  {
                    type: "image",
                    file_name: "image.png",
                    image_url: "blob:http://localhost/local-preview",
                  },
                  {
                    type: "text",
                    text: '<lantai_attachment_roles>\n[{"name":"image.png","role":"reference","label":"参考资料"}]\n</lantai_attachment_roles>',
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    const persisted = {
      id: "msg-persisted-attachment",
      role: "user" as const,
      metadata: {
        client_turn_id: "msg-persisted-attachment",
        client_turn_id_origin: "message_id_fallback",
      },
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  { type: "text", text: "这个太逗了" },
                  {
                    type: "image",
                    source: {
                      type: "url",
                      url: "/workspace/media/42e295cc117741d4baad06ea3f7ca814_image.png",
                    },
                  },
                  {
                    type: "text",
                    text: '<lantai_attachment_roles>\n[{"name":"image.png","role":"reference","label":"参考资料"}]\n</lantai_attachment_roles>',
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    const completedAssistant = (id: string) => ({
      id,
      role: "assistant" as const,
      cards: [],
    });

    const reconciled = chooseActiveSessionMessages(
      [optimistic, completedAssistant("live-answer")],
      [persisted, completedAssistant("history-answer")],
    );

    expect(
      reconciled.filter((message) => message.role === "user"),
    ).toHaveLength(1);
    expect(reconciled[0]?.id).toBe("msg-persisted-attachment");
  });

  it("preserves two deliberate repeats of the same attachment turn", () => {
    const user = (
      id: string,
      clientTurnId: string,
      image: Record<string, unknown>,
    ) => ({
      id,
      role: "user" as const,
      metadata: { client_turn_id: clientTurnId },
      cards: [
        {
          code: "RequestCard",
          data: {
            client_turn_id: clientTurnId,
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  { type: "text", text: "这个太逗了" },
                  image,
                  {
                    type: "text",
                    text: '<lantai_attachment_roles>\n[{"name":"image.png","role":"reference","label":"参考资料"}]\n</lantai_attachment_roles>',
                  },
                ],
              },
            ],
          },
        },
      ],
    });
    const completedAssistant = (id: string) => ({
      id,
      role: "assistant" as const,
      cards: [],
    });
    const current = [
      user("optimistic-1", "local-1", {
        type: "image",
        file_name: "image.png",
        image_url: "blob:http://localhost/preview-1",
      }),
      completedAssistant("live-answer-1"),
      user("optimistic-2", "local-2", {
        type: "image",
        file_name: "image.png",
        image_url: "blob:http://localhost/preview-2",
      }),
      completedAssistant("live-answer-2"),
    ];
    const incoming = [
      user("persisted-1", "local-1", {
        type: "image",
        source: {
          type: "url",
          url: "/workspace/media/11111111111111111111111111111111_image.png",
        },
      }),
      completedAssistant("history-answer-1"),
      user("persisted-2", "local-2", {
        type: "image",
        source: {
          type: "url",
          url: "/workspace/media/22222222222222222222222222222222_image.png",
        },
      }),
      completedAssistant("history-answer-2"),
    ];

    const reconciled = chooseActiveSessionMessages(current, incoming);

    expect(
      reconciled.filter((message) => message.role === "user"),
    ).toHaveLength(2);
  });

  it("does not replace complete history with a user-only session snapshot", async () => {
    const historyMessages = [
      {
        id: "history-user",
        role: "user",
        cards: [],
      },
      {
        id: "history-assistant",
        role: "assistant",
        msgStatus: "finished",
        cards: [
          {
            code: responseCardCode,
            data: {
              id: "history-response",
              object: "response",
              status: "completed",
              output: [
                {
                  id: "history-answer",
                  type: "message",
                  status: "completed",
                  role: "assistant",
                  content: [{ type: "text", text: "稳定保留的回复" }],
                },
              ],
            },
          },
        ],
      },
    ];

    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <SessionMetadataPatchButton /> },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: historyMessages },
              ],
              getSession: async () => ({
                id: "session-1",
                messages: historyMessages,
              }),
              updateSession: async () => [
                { id: "session-1", messages: historyMessages },
              ],
            },
          },
        }}
      />,
    );

    expect(await screen.findByText("稳定保留的回复")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Apply metadata patch" }),
    );
    expect(screen.getByText("稳定保留的回复")).toBeInTheDocument();
  });

  it("does not replace a runtime-rescued assistant message with a user-only history refresh", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <RuntimeRescuePatchButtons /> },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Apply runtime rescue" }),
    );
    expect(
      await screen.findByText("runtime rescued answer"),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Apply user-only history" }),
    );
    expect(screen.getByText("runtime rescued answer")).toBeInTheDocument();
  });

  it("merges adjacent assistant response cards into one visible assistant block", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <SplitAssistantPatchButton /> },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Apply split assistant patch",
      }),
    );

    await waitFor(() => expect(screen.getByText("先判断")).toBeInTheDocument());
    expect(screen.getByText("然后回答")).toBeInTheDocument();
    expect(screen.getAllByTestId("stream-output")).toHaveLength(1);
    expect(document.querySelectorAll(".lantai-bubble-assistant")).toHaveLength(
      1,
    );
  });

  it("merges completed reasoning and answer cards into one assistant block", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <CompletedReasoningAnswerPatchButton /> },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Apply completed reasoning answer patch",
      }),
    );

    await waitFor(() =>
      expect(screen.getByText("先想清楚")).toBeInTheDocument(),
    );
    expect(screen.getByText("再回答")).toBeInTheDocument();
    expect(screen.getAllByTestId("stream-output")).toHaveLength(1);
    expect(document.querySelectorAll(".lantai-bubble-assistant")).toHaveLength(
      1,
    );
  });

  it("merges adjacent reasoning and answer phases even when transport ids differ", () => {
    const assistant = (
      id: string,
      turnId: string,
      type: "reasoning" | "message",
      text: string,
    ) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata: { native_runtime_turn_id: turnId },
            output: [
              {
                id: `${id}-output`,
                type,
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text, status: "completed" }],
                metadata: { native_runtime_turn_id: turnId },
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages([
      assistant("reasoning-phase", "reasoning-run", "reasoning", "先想清楚"),
      assistant("answer-phase", "answer-run", "message", "再回答"),
    ]);
    const response = messages[0]?.cards?.[0]?.data as {
      output?: Array<{ type?: string }>;
    };

    expect(messages).toHaveLength(1);
    expect(response.output?.map((item) => item.type)).toEqual([
      "reasoning",
      "message",
    ]);
  });

  it("merges a legacy tool-only fragment back into its shared run turn", () => {
    const assistant = (
      id: string,
      metadata: Record<string, unknown>,
      type: "message" | "plugin_call",
      text: string,
    ) => ({
      id,
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata,
            output: [
              {
                id: `${id}-output`,
                type,
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text, status: "completed" }],
                metadata,
              },
            ],
          },
        },
      ],
    });

    const messages = chooseActiveSessionMessages([
      assistant(
        "answer-fragment",
        { client_turn_id: "client-1", run_id: "shared-run" },
        "message",
        "主体回答",
      ),
      assistant(
        "legacy-tool-fragment",
        { run_id: "shared-run" },
        "plugin_call",
        "guanlan_search",
      ),
    ]);

    expect(messages).toHaveLength(1);
    expect(messages[0]?.cards?.[0]?.data?.output).toHaveLength(2);
  });

  it("keeps adjacent assistant cards separate when their turn ids differ", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <DistinctTurnPatchButton /> },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Apply distinct turns" }),
    );

    expect(await screen.findByText("第一轮回复")).toBeInTheDocument();
    expect(screen.getByText("第二轮回复")).toBeInTheDocument();
    expect(screen.getAllByTestId("stream-output")).toHaveLength(2);
    expect(document.querySelectorAll(".lantai-bubble-assistant")).toHaveLength(
      2,
    );
  });

  it("does not replace a runtime-rescued assistant with user-only getSession detail", async () => {
    const rescuedMessages = [
      {
        id: "history-user-only",
        role: "user",
        cards: [],
      },
      {
        id: "runtime-rescued-assistant",
        role: "assistant",
        msgStatus: "finished",
        cards: [
          {
            code: responseCardCode,
            data: {
              id: "runtime-rescued-response",
              object: "response",
              status: "completed",
              output: [
                {
                  id: "runtime-rescued-output",
                  type: "message",
                  status: "completed",
                  role: "assistant",
                  content: [
                    {
                      type: "text",
                      text: "runtime rescued answer",
                    },
                  ],
                  metadata: { native_runtime_stream: true },
                },
              ],
            },
          },
        ],
      },
    ];
    const getSession = vi.fn(async () => ({
      id: "session-1",
      messages: [
        {
          id: "history-user-only",
          role: "user",
          cards: [],
        },
      ],
      generating: false,
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: rescuedMessages },
              ],
              getSession,
              updateSession: async () => [
                { id: "session-1", messages: rescuedMessages },
              ],
            },
          },
        }}
      />,
    );

    await waitFor(() => expect(getSession).toHaveBeenCalledWith("session-1"));
    expect(screen.getByText("runtime rescued answer")).toBeInTheDocument();
  });

  it("keeps the explicitly routed session authoritative while history initializes", async () => {
    let resolveSessionList:
      | ((sessions: Array<{ id: string; messages: never[] }>) => void)
      | undefined;
    const getSessionList = vi.fn(
      () =>
        new Promise<Array<{ id: string; messages: never[] }>>((resolve) => {
          resolveSessionList = resolve;
        }),
    );
    const getSession = vi.fn(async (sessionId: string) => ({
      id: sessionId,
      messages: [],
    }));
    const activateSession = vi.fn();

    render(
      <LantaiRuntimeWebUI
        options={{
          theme: { rightHeader: <CurrentSessionProbe /> },
          session: {
            initialSessionId: "routed-session",
            initialSessionStrategy: "first",
            api: {
              activateSession,
              getSessionList,
              getSession,
            },
          },
        }}
      />,
    );

    expect(screen.getByTestId("current-session-id")).toHaveTextContent(
      "routed-session",
    );
    await waitFor(() =>
      expect(activateSession).toHaveBeenCalledWith("routed-session"),
    );
    await waitFor(() =>
      expect(getSession).toHaveBeenCalledWith("routed-session"),
    );

    await act(async () => {
      resolveSessionList?.([
        { id: "newest-unrelated-session", messages: [] },
        { id: "routed-session", messages: [] },
      ]);
    });

    expect(screen.getByTestId("current-session-id")).toHaveTextContent(
      "routed-session",
    );
    expect(getSession).not.toHaveBeenCalledWith("newest-unrelated-session");
  });

  it("creates a fresh session instead of selecting history when requested", async () => {
    const getSession = vi.fn(async (sessionId: string) => ({
      id: sessionId,
      messages: [],
    }));
    const createSession = vi.fn(async () => [
      { id: "fresh-session", messages: [] },
      { id: "history-session", messages: [] },
    ]);

    render(
      <LantaiRuntimeWebUI
        options={{
          theme: { rightHeader: <CurrentSessionProbe /> },
          session: {
            initialSessionStrategy: "new",
            api: {
              getSessionList: async () => [
                { id: "history-session", messages: [] },
              ],
              getSession,
              createSession,
            },
          },
        }}
      />,
    );

    await waitFor(() => expect(createSession).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId("current-session-id")).toHaveTextContent(
        "fresh-session",
      ),
    );
    expect(getSession).not.toHaveBeenCalledWith("history-session");
  });

  it("coalesces overlapping new-session requests into one created chat", async () => {
    let resolveCreate:
      | ((sessions: Array<{ id: string; messages: never[] }>) => void)
      | undefined;
    const createSession = vi.fn(
      () =>
        new Promise<Array<{ id: string; messages: never[] }>>((resolve) => {
          resolveCreate = resolve;
        }),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          theme: {
            rightHeader: (
              <>
                <CurrentSessionProbe />
                <DoubleCreateSessionButton />
              </>
            ),
          },
          session: {
            initialSessionStrategy: "first",
            api: {
              getSessionList: async () => [
                { id: "history-session", messages: [] },
              ],
              getSession: async (sessionId: string) => ({
                id: sessionId,
                messages: [],
              }),
              createSession,
            },
          },
        }}
      />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("current-session-id")).toHaveTextContent(
        "history-session",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create twice" }));

    expect(createSession).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveCreate?.([{ id: "fresh-session", messages: [] }]);
    });
    await waitFor(() =>
      expect(screen.getByTestId("current-session-id")).toHaveTextContent(
        "fresh-session",
      ),
    );
  });

  it("waits for session initialization before sending the first message", async () => {
    let resolveSessions: (
      sessions: Array<{ id: string; sessionId: string; messages: never[] }>,
    ) => void = () => {};
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const getSessionList = vi.fn(
      () =>
        new Promise<
          Array<{ id: string; sessionId: string; messages: never[] }>
        >((resolve) => {
          resolveSessions = resolve;
        }),
    );
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          theme: { rightHeader: <PrimaryStreamOwnerProbe /> },
          session: {
            api: {
              getSessionList,
              getSession: async (sessionId) => ({
                id: sessionId,
                messages: [],
              }),
              updateSession: async () => [],
            },
          },
        }}
      />,
    );

    const textarea = screen.getByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "初始化期间发送" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    expect(fetchStream).not.toHaveBeenCalled();

    act(() => {
      resolveSessions([
        {
          id: "session-1",
          sessionId: "backend-session-1",
          messages: [],
        },
      ]);
    });

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId("primary-stream-owner")).toHaveTextContent(
        "true",
      ),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Simulate session refresh" }),
    );
    expect(screen.getByTestId("primary-stream-owner")).toHaveTextContent(
      "true",
    );
    expect(fetchStream).toHaveBeenCalledWith(
      expect.objectContaining({
        input: [
          expect.objectContaining({
            session: expect.objectContaining({
              session_id: "backend-session-1",
            }),
          }),
        ],
      }),
    );

    act(() => {
      streamController!.enqueue(
        sse({ object: "response", id: "response-1", status: "completed" }),
      );
      streamController!.close();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
    expect(screen.getByTestId("primary-stream-owner")).toHaveTextContent(
      "false",
    );
  });

  it("opens a newly selected historical session at its latest turn", async () => {
    const historicalMessages = [
      {
        id: "session-2-assistant",
        role: "assistant",
        msgStatus: "finished",
        cards: [
          {
            code: responseCardCode,
            data: {
              id: "session-2-response",
              object: "response",
              status: "completed",
              output: [
                {
                  id: "session-2-output",
                  type: "message",
                  status: "completed",
                  role: "assistant",
                  content: [{ type: "text", text: "会话二最后一轮" }],
                },
              ],
            },
          },
        ],
      },
    ];
    const getSession = vi.fn(async (sessionId: string) => ({
      id: sessionId,
      messages: sessionId === "session-2" ? historicalMessages : [],
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <SessionSwitchButton /> },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: [] },
                { id: "session-2", messages: historicalMessages },
              ],
              getSession,
            },
          },
        }}
      />,
    );

    await waitFor(() => {
      expect(getSession).toHaveBeenCalledWith("session-1");
    });

    const messageList = document.querySelector(
      ".lantai-chat-anywhere-message-list",
    ) as HTMLDivElement;
    const scrollTo = vi.fn();
    Object.defineProperties(messageList, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 1_800 },
      scrollTop: { configurable: true, writable: true, value: 0 },
      scrollTo: { configurable: true, value: scrollTo },
    });
    scrollTo.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "Switch session" }));

    await screen.findByText("会话二最后一轮");
    await waitFor(() => {
      expect(scrollTo).toHaveBeenCalledWith({
        top: 1_800,
        behavior: "auto",
      });
    });
  });

  it("keeps an independent unsent text draft for each session", async () => {
    const drafts = new Map<string, string>();
    const getSession = vi.fn(async (sessionId: string) => ({
      id: sessionId,
      messages: [],
    }));
    const draftApi = {
      getSessionList: async () => [
        { id: "session-1", messages: [] },
        { id: "session-2", messages: [] },
      ],
      getSession,
      loadDraftText: vi.fn((sessionId: string) => drafts.get(sessionId) || ""),
      saveDraftText: vi.fn((sessionId: string, text: string) => {
        if (text) drafts.set(sessionId, text);
        else drafts.delete(sessionId);
      }),
      clearDraftText: vi.fn((sessionId: string) => {
        drafts.delete(sessionId);
      }),
    };

    render(
      <LantaiRuntimeWebUI
        options={{
          theme: { rightHeader: <SessionDraftSwitchButtons /> },
          sender: { placeholder: "按会话保留草稿" },
          session: { api: draftApi as any },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("按会话保留草稿");
    await waitFor(() => expect(getSession).toHaveBeenCalledWith("session-1"));
    fireEvent.change(textarea, { target: { value: "会话一草稿" } });

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session two" }),
    );
    await waitFor(() => expect(getSession).toHaveBeenCalledWith("session-2"));
    expect(textarea).toHaveValue("");

    fireEvent.change(textarea, { target: { value: "会话二草稿" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session one" }),
    );

    await waitFor(() => expect(textarea).toHaveValue("会话一草稿"));
  });

  it("restores an unsent draft after leaving and returning to chat", async () => {
    const drafts = new Map<string, string>();
    const draftApi = {
      getSessionList: async () => [{ id: "session-1", messages: [] }],
      getSession: async () => ({ id: "session-1", messages: [] }),
      loadDraftText: (sessionId: string) => drafts.get(sessionId) || "",
      saveDraftText: (sessionId: string, text: string) => {
        if (text) drafts.set(sessionId, text);
        else drafts.delete(sessionId);
      },
      clearDraftText: (sessionId: string) => {
        drafts.delete(sessionId);
      },
    };
    const options = {
      sender: { placeholder: "离开后恢复草稿" },
      session: { api: draftApi as any },
    };

    const view = render(<LantaiRuntimeWebUI options={options} />);
    const textarea = await screen.findByPlaceholderText("离开后恢复草稿");
    fireEvent.change(textarea, { target: { value: "尚未发送的内容" } });

    view.unmount();
    render(<LantaiRuntimeWebUI options={options} />);

    expect(await screen.findByPlaceholderText("离开后恢复草稿")).toHaveValue(
      "尚未发送的内容",
    );
  });

  it("drops an orphaned waiting shell when route hydration already contains the terminal answer", async () => {
    const user = {
      id: "route-return-user",
      role: "user" as const,
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [
              {
                content: [
                  { type: "text", text: "从设置返回后不要重新连接模型" },
                ],
              },
            ],
          },
        },
      ],
    };
    const completedAssistant = {
      id: "route-return-answer",
      role: "assistant" as const,
      msgStatus: "finished",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: "route-return-response",
            object: "response",
            status: "completed",
            output: [
              {
                id: "route-return-output",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: "这条回答已经完成。" }],
              },
            ],
          },
        },
      ],
    };
    const orphanedWaitingAssistant = {
      id: "route-return-orphaned-waiting",
      role: "assistant" as const,
      msgStatus: "loading",
      cards: [
        {
          code: responseCardCode,
          data: {
            id: "route-return-waiting-response",
            object: "response",
            status: "in_progress",
            metadata: { optimistic_pending: true },
            output: [],
          },
        },
      ],
    };
    const staleProjection = {
      id: "session-route-return",
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        can_reconnect: true,
        can_stop: true,
      },
      messages: [user, completedAssistant, orphanedWaitingAssistant],
    };
    const updateSession = vi.fn(async () => []);

    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "设置返回后的输入框" },
          session: {
            api: {
              getSessionList: async () => [staleProjection],
              getSession: async () => ({
                ...staleProjection,
                messages: [user, completedAssistant],
              }),
              updateSession,
            },
          },
        }}
      />,
    );

    expect(await screen.findByText("这条回答已经完成。")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
    expect(screen.queryByText("正在连接模型")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /停\s*止/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("设置返回后的输入框")).toBeEnabled();
    await waitFor(() =>
      expect(updateSession).toHaveBeenCalledWith(
        expect.objectContaining({
          id: "session-route-return",
          generating: false,
          status: "idle",
          runtimeState: expect.objectContaining({
            status: "completed",
            can_reconnect: false,
            can_stop: false,
          }),
          messages: [user, completedAssistant],
        }),
      ),
    );
  });

  it("keeps unsent attachments isolated to the session where they were added", async () => {
    const uploads: Array<{
      file: File;
      onSuccess: (body: { url?: string; thumbUrl?: string }) => void;
    }> = [];
    const customRequest = vi.fn((request) => {
      uploads.push(request);
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          theme: { rightHeader: <SessionDraftSwitchButtons /> },
          sender: {
            placeholder: "会话附件草稿",
            attachments: { customRequest },
          },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: [] },
                { id: "session-2", messages: [] },
              ],
              getSession: async (sessionId) => ({
                id: sessionId,
                messages: [],
              }),
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("会话附件草稿");
    const file = new File(["draft"], "session-one.pdf", {
      type: "application/pdf",
    });
    fireEvent.paste(textarea, {
      clipboardData: {
        items: [{ kind: "file", getAsFile: () => file }],
        files: [file],
      },
    });
    await waitFor(() => expect(customRequest).toHaveBeenCalledOnce());
    act(() => {
      uploads[0].onSuccess({ url: "/files/preview/session-one.pdf" });
    });
    expect(screen.getByText("session-one.pdf")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session two" }),
    );
    await waitFor(() =>
      expect(screen.queryByText("session-one.pdf")).not.toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session one" }),
    );
    expect(await screen.findByText("session-one.pdf")).toBeInTheDocument();
  });

  it("persists an old session stream after switching sessions", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const getSessionList = vi.fn(async () => [
      { id: "session-1", messages: [] },
      { id: "session-2", messages: [] },
    ]);
    const updateSession = vi.fn(async () => [
      { id: "session-1", messages: [] },
      { id: "session-2", messages: [] },
    ]);
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <SessionDraftSwitchButtons /> },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList,
              getSession: async (sessionId) => {
                if (sessionId === "session-2") {
                  return new Promise(() => {});
                }
                return { id: sessionId, messages: [] };
              },
              updateSession,
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "会话一的问题" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    const refreshesBeforeOldResponse = getSessionList.mock.calls.length;

    act(() => {
      screen.getByRole("button", { name: "Switch to session two" }).click();
      streamController!.enqueue(
        sse({
          object: "message",
          id: "session-1-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "只属于会话一" }],
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "session-1-response",
          status: "completed",
        }),
      );
      streamController!.close();
    });

    await waitFor(() =>
      expect(getSessionList.mock.calls.length).toBeGreaterThan(
        refreshesBeforeOldResponse,
      ),
    );
    await waitFor(() =>
      expect(updateSession).toHaveBeenCalledWith(
        expect.objectContaining({
          id: "session-1",
          messages: expect.arrayContaining([
            expect.objectContaining({
              role: "assistant",
            }),
          ]),
        }),
      ),
    );
    expect(screen.queryByText("只属于会话一")).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session one" }),
    );
    expect(await screen.findByText("只属于会话一")).toBeInTheDocument();
  });

  it("keeps distinct conversations isolated when their backend aliases overlap", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const sessionTwoMessages = [
      {
        id: "session-2-user",
        role: "user",
        cards: [
          {
            code: "LantaiRuntimeRequestCard",
            data: {
              input: [
                {
                  role: "user",
                  type: "message",
                  content: [
                    {
                      type: "text",
                      text: "这两个图，哪个30天消耗量更大？",
                    },
                  ],
                },
              ],
            },
          },
        ],
      },
      {
        id: "session-2-assistant",
        role: "assistant",
        cards: [
          {
            code: responseCardCode,
            data: {
              id: "session-2-response",
              object: "response",
              status: "completed",
              output: [
                {
                  id: "session-2-output",
                  type: "message",
                  status: "completed",
                  role: "assistant",
                  content: [{ type: "text", text: "第二张图消耗量更大。" }],
                },
              ],
            },
          },
        ],
      },
    ];
    const refreshedSessions = [
      {
        id: "session-1",
        realId: "chat-snake",
        sessionId: "console:shared-workspace",
        messages: [],
      },
      {
        id: "session-2",
        realId: "chat-token-usage",
        sessionId: "console:shared-workspace",
        messages: sessionTwoMessages,
      },
    ];
    const getSessionList = vi.fn(async () => refreshedSessions as any);
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              // Keep session one running while the session list refreshes.
              streamController = controller;
            },
          }),
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "发送会话隔离回归" },
          theme: {
            rightHeader: (
              <>
                <SessionDraftSwitchButtons />
                <ReplaceSessionsButton sessions={refreshedSessions} />
              </>
            ),
          },
          session: {
            api: {
              getSessionList,
              getSession: async (sessionId) =>
                (refreshedSessions.find((item) => item.id === sessionId) || {
                  id: sessionId,
                  messages: [],
                }) as any,
              updateSession: async () => refreshedSessions as any,
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("发送会话隔离回归");
    fireEvent.change(textarea, { target: { value: "做一个贪吃蛇小游戏" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: "Replace sessions" }));
    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session two" }),
    );

    expect(await screen.findByText("第二张图消耗量更大。")).toBeInTheDocument();
    expect(screen.queryByText("做一个贪吃蛇小游戏")).not.toBeInTheDocument();
    expect(screen.getAllByText("这两个图，哪个30天消耗量更大？")).toHaveLength(
      1,
    );
    expect(screen.getAllByText("第二张图消耗量更大。")).toHaveLength(1);

    const refreshesBeforeCompletion = getSessionList.mock.calls.length;
    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "session-1-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "贪吃蛇已经做好了。" }],
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "session-1-response",
          status: "completed",
        }),
      );
      streamController!.close();
    });
    await waitFor(() =>
      expect(getSessionList.mock.calls.length).toBeGreaterThan(
        refreshesBeforeCompletion,
      ),
    );
    expect(screen.getAllByText("这两个图，哪个30天消耗量更大？")).toHaveLength(
      1,
    );
    expect(screen.getAllByText("第二张图消耗量更大。")).toHaveLength(1);
    expect(screen.queryByText("贪吃蛇已经做好了。")).not.toBeInTheDocument();
  });

  it("restores the pending assistant after an inactive running session is refreshed", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
        ),
    );
    const getSession = vi.fn(async (sessionId: string) => ({
      id: sessionId,
      messages: [],
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: PendingAwareStreamCard },
          theme: {
            rightHeader: (
              <>
                <SessionDraftSwitchButtons />
                <RefreshSessionsWithoutPendingAssistantButton />
              </>
            ),
          },
          sender: { placeholder: "切回运行会话仍需反馈" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: [] },
                { id: "session-2", messages: [] },
              ],
              getSession,
              updateSession: async () => [
                { id: "session-1", messages: [] },
                { id: "session-2", messages: [] },
              ],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("切回运行会话仍需反馈");
    fireEvent.change(textarea, { target: { value: "慢首字节任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("assistant-pending")).toHaveTextContent(
      "正在思考",
    );
    expect(screen.getByTestId("assistant-pending")).toHaveAttribute(
      "data-optimistic-pending",
      "true",
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session two" }),
    );
    await waitFor(() => expect(getSession).toHaveBeenCalledWith("session-2"));
    fireEvent.click(
      screen.getByRole("button", {
        name: "Refresh sessions without pending assistant",
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session one" }),
    );

    expect(
      await screen.findByRole("button", { name: /停\s*止/ }),
    ).toBeEnabled();
    expect(screen.getByTestId("assistant-pending")).toHaveTextContent(
      "正在思考",
    );

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "slow-first-byte-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "任务最终完成" }],
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "slow-first-byte-response",
          status: "completed",
        }),
      );
      streamController!.close();
    });
    expect(await screen.findByText("任务最终完成")).toBeInTheDocument();
  });

  it("keeps the new session busy when an old session stream finishes", async () => {
    const streamControllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamControllers.push(controller);
            },
          }),
        ),
    );
    const getSession = vi.fn(async (sessionId: string) => ({
      id: sessionId,
      messages: [],
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <SessionSwitchButton /> },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: [] },
                { id: "session-2", messages: [] },
              ],
              getSession,
              updateSession: async () => [
                { id: "session-1", messages: [] },
                { id: "session-2", messages: [] },
              ],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    await waitFor(() => expect(getSession).toHaveBeenCalledWith("session-1"));
    fireEvent.change(textarea, { target: { value: "会话一的问题" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Switch session" }));
    await waitFor(() => expect(getSession).toHaveBeenCalledWith("session-2"));
    fireEvent.change(textarea, { target: { value: "会话二的问题" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));

    act(() => {
      streamControllers[0]!.enqueue(
        sse({
          object: "message",
          id: "session-1-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "不应进入会话二" }],
        }),
      );
      streamControllers[0]!.enqueue(
        sse({
          object: "response",
          id: "session-1-response",
          status: "completed",
        }),
      );
      streamControllers[0]!.close();
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /停\s*止/ })).toBeEnabled(),
    );
    expect(screen.queryByText("不应进入会话二")).not.toBeInTheDocument();

    act(() => {
      streamControllers[1]!.enqueue(
        sse({
          object: "message",
          id: "session-2-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "只属于会话二" }],
        }),
      );
      streamControllers[1]!.enqueue(
        sse({
          object: "response",
          id: "session-2-response",
          status: "completed",
        }),
      );
      streamControllers[1]!.close();
    });

    expect(await screen.findByText("只属于会话二")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
  });

  it("creates and runs a new session while the previous session is still streaming", async () => {
    const streamControllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const requestSessionIds: string[] = [];
    const fetchStream = vi.fn(
      async (request: {
        input?: Array<{ session?: { session_id?: string } }>;
      }) => {
        requestSessionIds.push(
          String(request.input?.[0]?.session?.session_id || ""),
        );
        return new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamControllers.push(controller);
            },
          }),
        );
      },
    );
    const createSession = vi.fn(async () => [
      { id: "session-2", sessionId: "runtime-session-2", messages: [] },
      { id: "session-1", sessionId: "runtime-session-1", messages: [] },
    ]);

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          theme: {
            rightHeader: (
              <>
                <CurrentSessionProbe />
                <CreateSessionButton />
              </>
            ),
          },
          sender: { placeholder: "并行新建会话" },
          session: {
            api: {
              getSessionList: async () => [
                {
                  id: "session-1",
                  sessionId: "runtime-session-1",
                  messages: [],
                },
              ],
              getSession: async (sessionId) => ({
                id: sessionId,
                sessionId:
                  sessionId === "session-1"
                    ? "runtime-session-1"
                    : "runtime-session-2",
                messages: [],
              }),
              createSession,
              updateSession: async () => [
                {
                  id: "session-2",
                  sessionId: "runtime-session-2",
                  messages: [],
                },
                {
                  id: "session-1",
                  sessionId: "runtime-session-1",
                  messages: [],
                },
              ],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("并行新建会话");
    fireEvent.change(textarea, { target: { value: "会话一仍在运行" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Create new session" }));
    await waitFor(() =>
      expect(screen.getByTestId("current-session-id")).toHaveTextContent(
        "session-2",
      ),
    );
    expect(streamControllers[0]).toBeDefined();

    fireEvent.change(textarea, { target: { value: "会话二立即开始" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));

    expect(requestSessionIds).toEqual([
      "runtime-session-1",
      "runtime-session-2",
    ]);
    expect(streamControllers).toHaveLength(2);

    act(() => {
      for (const controller of streamControllers) {
        controller.enqueue(sse({ object: "response", status: "completed" }));
        controller.close();
      }
    });
  });

  it("stops only the active session while another session keeps streaming", async () => {
    const streamControllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const requestSignals: AbortSignal[] = [];
    const fetchStream = vi.fn(async (request: { signal?: AbortSignal }) => {
      if (request.signal) requestSignals.push(request.signal);
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            streamControllers.push(controller);
          },
        }),
      );
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          theme: { rightHeader: <SessionDraftSwitchButtons /> },
          sender: { placeholder: "并发停止隔离" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: [] },
                { id: "session-2", messages: [] },
              ],
              getSession: async (sessionId) => ({
                id: sessionId,
                messages: [],
              }),
              updateSession: async () => [
                { id: "session-1", messages: [] },
                { id: "session-2", messages: [] },
              ],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("并发停止隔离");
    fireEvent.change(textarea, { target: { value: "会话一继续运行" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session two" }),
    );
    fireEvent.change(textarea, { target: { value: "会话二继续运行" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));

    expect(requestSignals[0]?.aborted).toBe(false);
    expect(requestSignals[1]?.aborted).toBe(false);

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session one" }),
    );
    fireEvent.click(screen.getByRole("button", { name: /停\s*止/ }));

    await waitFor(() => expect(requestSignals[0]?.aborted).toBe(true));
    expect(requestSignals[1]?.aborted).toBe(false);

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session two" }),
    );
    expect(
      await screen.findByRole("button", { name: /停\s*止/ }),
    ).toBeEnabled();

    act(() => {
      streamControllers[1]!.enqueue(
        sse({
          object: "message",
          id: "session-2-isolated-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "会话二未被中止" }],
        }),
      );
      streamControllers[1]!.enqueue(
        sse({
          object: "response",
          id: "session-2-isolated-response",
          status: "completed",
        }),
      );
      streamControllers[1]!.close();
    });

    expect(await screen.findByText("会话二未被中止")).toBeInTheDocument();
  });

  it("does not revive a stale persisted generating flag without a live run", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          sender: { placeholder: "旧状态不可复活" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: [], generating: true },
              ],
              getSession: async () => ({
                id: "session-1",
                messages: [],
                generating: true,
              }),
            },
          },
        }}
      />,
    );

    expect(
      await screen.findByRole("button", { name: /发\s*送/ }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: /停\s*止/ }),
    ).not.toBeInTheDocument();
  });

  it("renders thinking and answer deltas before the response is completed", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: MemoizedStreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试流式" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "thinking-1",
          type: "reasoning",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "thinking-1",
          delta: true,
          text: "先判断",
          status: "in_progress",
        }),
      );
    });

    await waitFor(() => {
      const output = screen.getByTestId("stream-output");
      expect(output).toHaveTextContent("先判断");
      expect(output.querySelector('[data-kind="reasoning"]')).toHaveTextContent(
        "先判断",
      );
    });

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-1",
          type: "message",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "answer-1",
          delta: true,
          text: "然后回答",
          status: "in_progress",
        }),
      );
    });

    await waitFor(() => {
      const output = screen.getByTestId("stream-output");
      expect(output).toHaveTextContent("先判断");
      expect(output).toHaveTextContent("然后回答");
      expect(output.querySelector('[data-kind="reasoning"]')).toHaveTextContent(
        "先判断",
      );
      expect(output.querySelector('[data-kind="message"]')).toHaveTextContent(
        "然后回答",
      );
    });

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "thinking-1",
          type: "reasoning",
          status: "completed",
          role: "assistant",
          content: [
            {
              object: "content",
              status: "completed",
              type: "text",
              index: 0,
              text: "先判断",
            },
          ],
        }),
      );
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-1",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            {
              object: "content",
              status: "completed",
              type: "text",
              index: 0,
              text: "然后回答",
            },
          ],
        }),
      );
      streamController!.enqueue(
        sse({ object: "response", id: "response-1", status: "completed" }),
      );
      streamController!.close();
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
  });

  it("preserves message-start identity on the first visible content delta", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "流身份边界" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("流身份边界"), {
      target: { value: "验证首帧身份" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "provider-answer:message",
          type: "message",
          status: "in_progress",
          role: "assistant",
          content: [],
          metadata: {
            source_message_id: "provider-answer:message",
            client_turn_id: "client-turn-1",
          },
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "provider-answer:message",
          type: "text",
          delta: true,
          text: "FIRST_VISIBLE_TOKEN",
          status: "in_progress",
          metadata: {
            source_message_id: "provider-answer:message",
            client_turn_id: "client-turn-1",
          },
        }),
      );
    });

    const answer = await screen.findByText("FIRST_VISIBLE_TOKEN");
    expect(answer).toHaveAttribute(
      "data-source-message-id",
      "provider-answer:message",
    );
    expect(answer).toHaveAttribute("data-client-turn-id", "client-turn-1");
  });

  it("coalesces a burst of text deltas before syncing the active session", async () => {
    const updateSession = vi.fn(async () => [
      { id: "session-1", messages: [] },
    ]);
    const text = Array.from({ length: 48 }, (_, index) =>
      String.fromCharCode(0x4e00 + index),
    ).join("");
    const frames = [
      {
        object: "message",
        id: "answer-burst",
        type: "message",
        status: "in_progress",
        role: "assistant",
      },
      ...Array.from(text).map((character) => ({
        object: "content",
        msg_id: "answer-burst",
        delta: true,
        text: character,
        status: "in_progress",
      })),
      {
        object: "message",
        id: "answer-burst",
        type: "message",
        status: "completed",
        role: "assistant",
        content: [{ type: "text", text }],
      },
      { object: "response", id: "response-burst", status: "completed" },
    ];
    const fetchStream = vi.fn(
      async () =>
        new Response(
          frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession,
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "突发流测试" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(text),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );

    // The renderer/session store must follow presentation frames, not raw
    // provider chunks. This stays well below one update per character while
    // still committing several intermediate frames for a smooth reveal.
    expect(updateSession.mock.calls.length).toBeLessThan(10);
  });

  it("shows assistant feedback before the first response byte and upgrades it in place", async () => {
    let resolveFetch!: (response: Response) => void;
    const fetchStream = vi.fn(() => {
      expect(screen.getByTestId("assistant-pending")).toHaveTextContent(
        "正在思考",
      );
      return new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      });
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: PendingAwareStreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "第一字节之前也要有反馈" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("assistant-pending")).toHaveTextContent(
      "正在思考",
    );

    act(() => {
      const messageEvent = {
        object: "message",
        id: "answer-after-wait",
        type: "message",
        status: "completed",
        role: "assistant",
        content: [{ type: "text", text: "现在开始回答" }],
      };
      const responseEvent = {
        object: "response",
        id: "response-after-wait",
        status: "completed",
      };
      resolveFetch(
        new Response(
          `data: ${JSON.stringify(messageEvent)}\n\ndata: ${JSON.stringify(responseEvent)}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );
    });

    expect(await screen.findByText("现在开始回答")).toBeInTheDocument();
    expect(screen.queryByTestId("assistant-pending")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("stream-output")).toHaveLength(1);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
  });

  it("commits a second turn's pending session state with its optimistic card", async () => {
    let resolveSecondFetch!: (response: Response) => void;
    const fetchStream = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          [
            {
              object: "message",
              id: "first-answer",
              type: "message",
              status: "completed",
              role: "assistant",
              content: [{ type: "text", text: "第一轮完成" }],
            },
            {
              object: "response",
              id: "first-response",
              status: "completed",
            },
          ]
            .map((frame) => `data: ${JSON.stringify(frame)}\n\n`)
            .join(""),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      )
      .mockImplementationOnce(() => {
        expect(screen.getByTestId("session-aware-pending")).toHaveTextContent(
          "已接收请求",
        );
        return new Promise<Response>((resolve) => {
          resolveSecondFetch = resolve;
        });
      });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: SessionAwarePendingStreamCard },
          sender: { placeholder: "连续两轮不能空白" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("连续两轮不能空白");
    fireEvent.change(textarea, { target: { value: "第一轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    expect(await screen.findByText("第一轮完成")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );

    fireEvent.change(textarea, { target: { value: "第二轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("session-aware-pending")).toHaveTextContent(
      "已接收请求",
    );

    act(() => {
      resolveSecondFetch(
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "second-response",
            status: "completed",
            output: [
              {
                id: "second-answer",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: "第二轮完成" }],
              },
            ],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );
    });
    expect(await screen.findByText("第二轮完成")).toBeInTheDocument();
  });

  it("replaces the optimistic receipt when an identified turn completes", async () => {
    const answer = "回答已经交付，不应再显示正在启动任务。";
    const fetchStream = vi.fn(async (request: any) => {
      const clientTurnId = String(
        request?.biz_params?.meta?.client_turn_id || "",
      );
      expect(clientTurnId).not.toBe("");
      const frames = [
        {
          object: "message",
          id: "identified-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: answer }],
          metadata: { client_turn_id: clientTurnId },
        },
        {
          object: "response",
          id: "identified-response",
          status: "completed",
          metadata: { client_turn_id: clientTurnId },
        },
      ];
      return new Response(
        frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: PendingAwareStreamCard },
          sender: { placeholder: "测试轮次身份" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("测试轮次身份"), {
      target: { value: "完成后不要冒出假任务" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    expect(await screen.findByText(answer)).toBeInTheDocument();
    expect(screen.queryByTestId("assistant-pending")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("stream-output")).toHaveLength(1);
  });

  it.each([false, true])(
    "shows a visible pending assistant state for a live response envelope (control identity: %s)",
    async (controlIdentity) => {
      let streamController: ReadableStreamDefaultController<Uint8Array>;
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
        },
      });
      const fetchStream = vi.fn(async () => new Response(stream));

      render(
        <LantaiRuntimeWebUI
          options={{
            api: { fetch: fetchStream },
            cards: { [responseCardCode]: PendingAwareStreamCard },
            sender: { placeholder: "说点什么" },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({ id: "session-1", messages: [] }),
                updateSession: async () => [{ id: "session-1", messages: [] }],
              },
            },
          }}
        />,
      );

      fireEvent.change(screen.getByPlaceholderText("说点什么"), {
        target: { value: "不要空卡" },
      });
      fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

      act(() => {
        streamController!.enqueue(
          sse({
            object: "response",
            status: "in_progress",
            output: [],
            metadata: {
              run_id: "run-live-envelope",
              ...(controlIdentity
                ? {
                    initial_status: true,
                    turn_receipt: {
                      run_id: "run-live-envelope",
                      attempt_id: "run-live-envelope:attempt-1",
                    },
                  }
                : {}),
            },
          }),
        );
      });

      await waitFor(() =>
        expect(screen.getByTestId("assistant-pending")).toHaveTextContent(
          "正在思考",
        ),
      );
      await waitFor(() =>
        expect(screen.getByTestId("assistant-pending")).toHaveAttribute(
          "data-optimistic-pending",
          "false",
        ),
      );
      expect(screen.queryByTestId("stream-output")).not.toBeInTheDocument();

      act(() => {
        streamController!.enqueue(
          sse({
            object: "message",
            id: "thinking-1",
            type: "reasoning",
            status: "in_progress",
            role: "assistant",
            content: [],
            metadata: { run_id: "run-live-envelope" },
          }),
        );
      });

      await waitFor(() =>
        expect(screen.getByTestId("assistant-pending")).toHaveTextContent(
          "正在思考",
        ),
      );
      const thinkingOutput = screen.queryByTestId("stream-output");
      if (thinkingOutput) {
        expect(
          thinkingOutput.querySelector(
            '[data-kind="reasoning"][data-status="in_progress"]',
          ),
        ).toBeInTheDocument();
        expect(thinkingOutput).not.toHaveTextContent("先思考");
      }
      expect(screen.queryByText("先思考")).not.toBeInTheDocument();

      act(() => {
        streamController!.enqueue(
          sse({
            object: "content",
            msg_id: "thinking-1",
            delta: true,
            text: "先思考",
            status: "in_progress",
            metadata: { run_id: "run-live-envelope" },
          }),
        );
      });

      await waitFor(() => {
        const output = screen.getByTestId("stream-output");
        expect(output).toHaveTextContent("先思考");
        expect(
          output.querySelector('[data-kind="reasoning"]'),
        ).toHaveTextContent("先思考");
      });
    },
  );

  it("keeps the welcome as an exit layer while the first optimistic turn enters", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: PendingAwareStreamCard },
          welcome: { greeting: "今天想聊点什么？" },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    const greeting = await screen.findByText("今天想聊点什么？");
    fireEvent.change(textarea, { target: { value: "首轮过渡" } });

    expect(greeting).toBeVisible();
    expect(
      greeting.closest(".lantai-chat-anywhere-message-list-welcome"),
    ).not.toHaveAttribute("data-lantai-welcome-exiting");

    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    const exitingWelcome = screen
      .getByText("今天想聊点什么？")
      .closest(".lantai-chat-anywhere-message-list-welcome");
    expect(exitingWelcome).toHaveAttribute("data-lantai-welcome-exiting", "1");
    expect(screen.getByText("首轮过渡")).toBeInTheDocument();
    expect(screen.getByTestId("assistant-pending")).toBeInTheDocument();

    await waitFor(
      () =>
        expect(screen.queryByText("今天想聊点什么？")).not.toBeInTheDocument(),
      { timeout: 1000 },
    );

    act(() => streamController!.close());
  });

  it("keeps one assistant turn across completed text, control receipts, and later tools", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(
      async () =>
        new Response(stream, {
          headers: { "Content-Type": "text/event-stream" },
        }),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          theme: { rightHeader: <PrimaryStreamOwnerProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "检查并继续修改 snake" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    const identity = {
      run_id: "run-snake",
      client_turn_id: "client-turn-snake",
    };
    act(() => {
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-snake",
          status: "in_progress",
          metadata: identity,
        }),
      );
      streamController!.enqueue(
        sse({
          object: "message",
          id: "reasoning-before-tool",
          type: "reasoning",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "先检查项目", status: "completed" }],
          metadata: identity,
        }),
      );
      streamController!.enqueue(
        sse({
          object: "message",
          id: "intermediate-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            {
              type: "text",
              text: "我先列出问题，然后继续执行",
              status: "completed",
            },
          ],
          metadata: identity,
        }),
      );
      streamController!.enqueue(
        sse({
          object: "turn_receipt",
          status: "running",
          receipt: identity,
          metadata: identity,
        }),
      );
      streamController!.enqueue(
        sse({
          object: "message",
          id: "tool-after-answer",
          type: "plugin_call",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "read_file", status: "completed" }],
          metadata: identity,
        }),
      );
    });

    await waitFor(() => {
      expect(screen.getAllByTestId("stream-output")).toHaveLength(1);
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "我先列出问题，然后继续执行",
      );
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "read_file",
      );
    });
    expect(screen.queryByText("响应事件格式无效")).not.toBeInTheDocument();
    expect(screen.getByTestId("primary-stream-owner")).toHaveTextContent(
      "true",
    );
    const stopButton = screen.getByRole("button", { name: "停止当前任务" });
    expect(stopButton).toBeVisible();
    expect(stopButton.querySelector("svg.lucide-square")).toBeInTheDocument();

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "final-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            { type: "text", text: "全部修改完成", status: "completed" },
          ],
          metadata: identity,
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-snake",
          status: "completed",
          metadata: identity,
        }),
      );
      streamController!.close();
    });

    expect(await screen.findByText("全部修改完成")).toBeInTheDocument();
    expect(screen.getAllByTestId("stream-output")).toHaveLength(1);
    await waitFor(() =>
      expect(screen.getByTestId("primary-stream-owner")).toHaveTextContent(
        "false",
      ),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("removes stop as soon as a terminal response arrives before transport EOF", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    let fetchCall = 0;
    const fetchStream = vi.fn(async () => {
      fetchCall += 1;
      return fetchCall === 1
        ? new Response(stream, {
            headers: { "Content-Type": "text/event-stream" },
          })
        : new Response("", {
            headers: { "Content-Type": "text/event-stream" },
          });
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          theme: { rightHeader: <PrimaryStreamOwnerProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "终态后立即恢复输入" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    expect(screen.getByRole("button", { name: "停止当前任务" })).toBeVisible();
    expect(screen.getByTestId("primary-stream-owner")).toHaveTextContent(
      "true",
    );

    act(() => {
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-terminal-before-eof",
          status: "completed",
          metadata: {
            run_id: "run-terminal-before-eof",
            client_turn_id: "turn-terminal-before-eof",
          },
        }),
      );
    });

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
      expect(screen.getByTestId("primary-stream-owner")).toHaveTextContent(
        "false",
      );
    });

    const recoveredComposer = screen.getByPlaceholderText("说点什么");
    fireEvent.change(recoveredComposer, {
      target: { value: "旧连接未关闭也要开始新任务" },
    });
    await waitFor(() =>
      expect(recoveredComposer).toHaveValue("旧连接未关闭也要开始新任务"),
    );
    fireEvent.keyDown(recoveredComposer, {
      key: "Enter",
      code: "Enter",
      shiftKey: false,
    });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
  });

  it("never exposes the same completed answer twice before the turn settles", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(
      async () =>
        new Response(stream, {
          headers: { "Content-Type": "text/event-stream" },
        }),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "不要闪两份回答" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    const identity = {
      run_id: "run-transient-duplicate",
      client_turn_id: "client-transient-duplicate",
    };
    const answer = "行，那就没事。想说了随时喊我。";
    act(() => {
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-transient-duplicate",
          status: "in_progress",
          metadata: identity,
        }),
      );
      // The low-latency stream and the durable runtime mirror can briefly
      // describe the same logical answer with different provider ids. The
      // user must never see both rows while the Ququ post-pass is still busy.
      for (const id of ["direct-answer", "ledger-answer-alias"]) {
        streamController!.enqueue(
          sse({
            object: "message",
            id,
            type: "message",
            status: "completed",
            role: "assistant",
            content: [{ type: "text", text: answer, status: "completed" }],
            metadata: {
              ...identity,
              source_message_id: "transient-logical-answer",
            },
          }),
        );
      }
    });

    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(answer),
    );
    expect(screen.getAllByText(answer)).toHaveLength(1);

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "ququ-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            {
              type: "text",
              text: "~>[echo] 我刚才确实停了一下。",
              status: "completed",
            },
          ],
          metadata: {
            ...identity,
            source_message_id: "transient-ququ-answer",
          },
        }),
      );
      streamController!.enqueue(
        sse({
          object: "message",
          id: "ququ-identical-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            {
              type: "text",
              text: `~>[echo] ${answer}`,
              status: "completed",
            },
          ],
          metadata: {
            ...identity,
            source_message_id: "transient-ququ-identical-answer",
          },
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-transient-duplicate",
          status: "completed",
          output: [],
          metadata: identity,
        }),
      );
      streamController!.close();
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
    expect(screen.getAllByText(answer)).toHaveLength(1);
    expect(
      screen.getByText("~>[echo] 我刚才确实停了一下。"),
    ).toBeInTheDocument();
  });

  it("stops without an error toast and sends a fresh turn through the real submit path", async () => {
    const streamControllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchStream = vi.fn(async () => {
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          streamControllers.push(controller);
        },
      });
      return new Response(stream, {
        headers: { "Content-Type": "text/event-stream" },
      });
    });

    const cancel = vi.fn().mockResolvedValue({ stopped: true });
    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          theme: { rightHeader: <RuntimeLifecycleProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "先写一份简报" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    expect(screen.getByText("正在停止，已生成内容会保留…")).toBeInTheDocument();
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("生成已停止，内容已保留"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("已停止生成").length).toBeGreaterThan(0);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
    const recoveredComposer = screen.getByPlaceholderText("说点什么");
    fireEvent.change(recoveredComposer, {
      target: { value: "现在重写第二版" },
    });
    fireEvent.keyDown(recoveredComposer, {
      key: "Enter",
      code: "Enter",
      shiftKey: false,
    });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(JSON.stringify(fetchStream.mock.calls[1]?.[0]?.input)).toContain(
      "现在重写第二版",
    );
    expect(screen.queryByText("等待当前任务结束后发送")).toBeNull();

    act(() => {
      streamControllers.forEach((controller) => controller.close());
    });
  });

  it("settles a stopped run when the active route uses a stable session alias", async () => {
    const cancel = vi.fn().mockResolvedValue({ stopped: true });
    const runningSession = {
      id: "session-1",
      realId: "runtime-session-1",
      messages: [],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        run_id: "run-session-alias-stop",
        can_stop: true,
      },
    };

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { cancel },
          sender: { placeholder: "别名停止竞态" },
          theme: {
            rightHeader: (
              <>
                <CurrentSessionProbe />
                <SessionAliasSwitchButton />
              </>
            ),
          },
          session: {
            api: {
              getSessionList: async () => [runningSession],
              getSession: async () => runningSession,
              updateSession: async () => [runningSession],
            },
          },
        }}
      />,
    );

    await screen.findByRole("button", { name: "停止当前任务" });
    fireEvent.click(
      screen.getByRole("button", { name: "Switch to runtime alias" }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("current-session-id")).toHaveTextContent(
        "runtime-session-1",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("aborts the in-flight controller after the active route switches to a stable session alias", async () => {
    let requestSignal: AbortSignal | undefined;
    const fetchStream = vi.fn(async ({ signal }: { signal?: AbortSignal }) => {
      requestSignal = signal;
      return new Response(
        new ReadableStream<Uint8Array>({
          start() {
            // The provider intentionally stays open until Stop aborts it.
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });
    const cancel = vi.fn().mockResolvedValue({ stopped: true });
    const session = {
      id: "session-1",
      realId: "runtime-session-1",
      messages: [],
    };

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "运行中别名停止" },
          theme: { rightHeader: <SessionAliasSwitchButton /> },
          session: {
            api: {
              getSessionList: async () => [session],
              getSession: async () => session,
              updateSession: async () => [session],
            },
          },
        }}
      />,
    );

    fireEvent.change(await screen.findByPlaceholderText("运行中别名停止"), {
      target: { value: "发送后切换别名再停止" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to runtime alias" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));

    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(requestSignal?.aborted).toBe(true);
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("keeps one run state when a temporary session becomes a backend id and then a runtime alias", async () => {
    let requestSignal: AbortSignal | undefined;
    const fetchStream = vi.fn(async ({ signal }: { signal?: AbortSignal }) => {
      requestSignal = signal;
      return new Response(
        new ReadableStream<Uint8Array>({
          start() {
            // The provider intentionally stays open until Stop aborts it.
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });
    const cancel = vi.fn().mockResolvedValue({ stopped: true });
    const session = {
      id: "session-1",
      sessionId: "session-1",
      messages: [],
    };

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "多段身份停止" },
          theme: { rightHeader: <MultiStageSessionIdentitySwitchButton /> },
          session: {
            api: {
              getSessionList: async () => [session],
              getSession: async () => session,
              updateSession: async () => [session],
            },
          },
        }}
      />,
    );

    fireEvent.change(await screen.findByPlaceholderText("多段身份停止"), {
      target: { value: "身份迁移后停止原请求" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByRole("button", { name: "Switch through backend identity" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "停止当前任务" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));

    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(requestSignal?.aborted).toBe(true);
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("keeps a confirmed stop when an empty backend state later absorbs the stopped temporary identity", async () => {
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start() {
              // The provider intentionally stays open until Stop aborts it.
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const cancel = vi.fn().mockResolvedValue({
      stopped: true,
      run_id: "run-stopped-before-alias-merge",
    });
    const backendSession = {
      id: "backend-session-1",
      messages: [],
    };

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止后合并身份" },
          theme: {
            rightHeader: (
              <>
                <SwitchToTemporarySessionButton />
                <MergeStoppedSessionIdentityButton />
              </>
            ),
          },
          session: {
            api: {
              getSessionList: async () => [backendSession],
              getSession: async () => backendSession,
              updateSession: async () => [backendSession],
            },
          },
        }}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", {
        name: "Switch to temporary identity",
      }),
    );
    fireEvent.change(await screen.findByPlaceholderText("停止后合并身份"), {
      target: { value: "先停止，再接收迟到的运行快照" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Merge stopped session identity" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("does not resurrect a stopped run when the terminal event beats the cancel acknowledgement", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    let resolveCancel: ((value: { stopped: true }) => void) | undefined;
    const cancel = vi.fn(
      () =>
        new Promise<{ stopped: true }>((resolve) => {
          resolveCancel = resolve;
        }),
    );
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止竞态" },
          theme: { rightHeader: <RuntimeLifecycleProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(await screen.findByPlaceholderText("停止竞态"), {
      target: { value: "先结束后确认" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime interrupted" }),
    );
    act(() => streamController?.close());
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );

    await act(async () => {
      resolveCancel?.({ stopped: true });
      await Promise.resolve();
    });

    expect(
      screen.queryByRole("button", { name: "停止当前任务" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("settles a pending stop when cancellation is persisted as a failed runtime terminal", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    let resolveCancel: ((value: { stopped: true }) => void) | undefined;
    const cancel = vi.fn(
      () =>
        new Promise<{ stopped: true }>((resolve) => {
          resolveCancel = resolve;
        }),
    );
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "失败终态停止竞态" },
          theme: { rightHeader: <RuntimeLifecycleProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(await screen.findByPlaceholderText("失败终态停止竞态"), {
      target: { value: "取消后后端记为 failed" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime failed" }),
    );
    act(() => streamController?.close());
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
    });
    expect(screen.getAllByText("已停止生成").length).toBeGreaterThan(0);

    await act(async () => {
      resolveCancel?.({ stopped: true });
      await Promise.resolve();
    });

    expect(
      screen.queryByRole("button", { name: "停止当前任务" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("does not resurrect a confirmed stop from a stale post-turn session refresh", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    let returnStaleRunningSnapshot = false;
    const staleSession = () => ({
      id: "session-1",
      messages: [],
      generating: returnStaleRunningSnapshot,
      status: returnStaleRunningSnapshot ? "running" : "idle",
      runtimeState: returnStaleRunningSnapshot
        ? {
            status: "running",
            run_id: "run-stopped-refresh-race",
            can_stop: true,
          }
        : undefined,
    });
    const getSessionList = vi.fn(async () => [staleSession()]);
    const getSession = vi.fn(async () => staleSession());
    const cancel = vi.fn(async () => {
      returnStaleRunningSnapshot = true;
      return { stopped: true };
    });
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止后刷新竞态" },
          session: {
            api: {
              getSessionList,
              getSession,
              updateSession: async () => [staleSession()],
            },
          },
        }}
      />,
    );

    fireEvent.change(await screen.findByPlaceholderText("停止后刷新竞态"), {
      target: { value: "停止后不要复活" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("生成已停止，内容已保留"),
    ).toBeInTheDocument();

    act(() => streamController?.close());
    await waitFor(() => expect(getSession).toHaveBeenCalled());
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("does not resurrect a confirmed stop when delayed route hydration returns stale running state", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    let resolveInitialHydration:
      | ((value: {
          id: string;
          messages: never[];
          generating: boolean;
          status: string;
          runtimeState: {
            status: string;
            run_id: string;
            can_stop: boolean;
          };
        }) => void)
      | undefined;
    const staleRunningSession = () => ({
      id: "session-1",
      messages: [],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        run_id: "run-delayed-route-hydration-race",
        can_stop: true,
      },
    });
    const initialHydration = new Promise<
      ReturnType<typeof staleRunningSession>
    >((resolve) => {
      resolveInitialHydration = resolve;
    });
    const getSession = vi
      .fn()
      .mockImplementationOnce(() => initialHydration)
      .mockImplementation(async () => staleRunningSession());
    const cancel = vi.fn().mockResolvedValue({ stopped: true });
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止后路由回灌竞态" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession,
              updateSession: async () => [staleRunningSession()],
            },
          },
        }}
      />,
    );

    fireEvent.change(await screen.findByPlaceholderText("停止后路由回灌竞态"), {
      target: { value: "已停止任务不得被延迟路由快照复活" },
    });
    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("生成已停止，内容已保留"),
    ).toBeInTheDocument();

    act(() => streamController?.close());
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );

    await act(async () => {
      resolveInitialHydration?.(staleRunningSession());
      await initialHydration;
    });

    expect(
      screen.queryByRole("button", { name: "停止当前任务" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("does not resurrect a confirmed stop from a stale host session bridge update", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const cancel = vi.fn().mockResolvedValue({ stopped: true });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止后宿主桥接竞态" },
          theme: { rightHeader: <RuntimeLifecycleProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(await screen.findByPlaceholderText("停止后宿主桥接竞态"), {
      target: { value: "停止后宿主不能用旧 running 状态复活" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("生成已停止，内容已保留"),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime active" }),
    );

    expect(
      screen.queryByRole("button", { name: "停止当前任务" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();

    act(() => streamController?.close());
  });

  it("does not resurrect a confirmed stop when a stale bridge keeps only an alternate turn identity", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const cancel = vi.fn().mockResolvedValue({ stopped: true });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止后运行身份别名竞态" },
          theme: { rightHeader: <RuntimeIdentityAliasProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(
      await screen.findByPlaceholderText("停止后运行身份别名竞态"),
      {
        target: { value: "同一运行切换身份字段也不得复活" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime aliases" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Replay alternate runtime alias" }),
    );

    expect(
      screen.queryByRole("button", { name: "停止当前任务" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();

    act(() => streamController?.close());
  });

  it("does not resurrect a confirmed stop when a stale bridge rotates every runtime identity", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const cancel = vi.fn().mockResolvedValue({ stopped: true });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止后运行身份完全轮换竞态" },
          theme: { rightHeader: <RuntimeIdentityAliasProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(
      await screen.findByPlaceholderText("停止后运行身份完全轮换竞态"),
      { target: { value: "同一运行完全轮换身份字段也不得复活" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime aliases" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Replay rotated runtime aliases" }),
    );

    expect(
      screen.queryByRole("button", { name: "停止当前任务" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();

    act(() => streamController?.close());
  });

  it("does not treat a different attempt id as newer without monotonic authority", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const cancel = vi.fn().mockResolvedValue({ stopped: true });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止后延迟不同 Attempt" },
          theme: { rightHeader: <RuntimeIdentityAliasProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(
      await screen.findByPlaceholderText("停止后延迟不同 Attempt"),
      {
        target: { value: "不同 Attempt 不能代表更新运行" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime aliases" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Replay newer runtime attempt" }),
    );

    expect(
      screen.queryByRole("button", { name: "停止当前任务" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();

    act(() => streamController?.close());
  });

  it("does not treat an attempt id first seen after stop as proof of a newer attempt", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const cancel = vi.fn().mockResolvedValue({ stopped: true });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "旧运行停止后补发 Attempt" },
          theme: { rightHeader: <RuntimeIdentityAliasProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(
      await screen.findByPlaceholderText("旧运行停止后补发 Attempt"),
      {
        target: { value: "字段升级不能复活已停止的同一运行" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    fireEvent.click(
      screen.getByRole("button", { name: "Mark legacy runtime identity" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );

    fireEvent.click(
      screen.getByRole("button", {
        name: "Replay legacy runtime with attempt",
      }),
    );

    expect(
      screen.queryByRole("button", { name: "停止当前任务" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();

    act(() => streamController?.close());
  });

  it("does not announce a successful stop before the runtime confirms it", async () => {
    let streamController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const cancel = vi.fn().mockResolvedValue({ stopped: false });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止确认" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("停止确认");
    fireEvent.change(textarea, { target: { value: "长任务" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    expect(screen.getByText("正在停止，已生成内容会保留…")).toBeInTheDocument();
    expect(
      await screen.findByText("未能确认停止，已生成内容仍已保留"),
    ).toBeInTheDocument();
    expect(screen.queryByText("已停止生成")).not.toBeInTheDocument();

    act(() => streamController?.close());
  });

  it("keeps the previous result visible and prepares retry without executing it", async () => {
    const fetchStream = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "response-first-failure",
            status: "failed",
            error: { message: "第一版失败，但已有结果必须保留" },
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "response-retry-success",
            status: "completed",
            output: [
              {
                id: "retry-answer",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: "第二版成功" }],
              },
            ],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: RecoveryStreamCard },
          sender: { placeholder: "非破坏重试" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async (session) => [session as any],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("非破坏重试");
    fireEvent.change(textarea, { target: { value: "原始问题" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    expect(
      (await screen.findAllByText("第一版失败，但已有结果必须保留")).length,
    ).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "重新生成" }));
    expect(textarea).toHaveValue("原始问题");
    expect(fetchStream).toHaveBeenCalledTimes(1);
    expect(
      screen.getAllByText("第一版失败，但已有结果必须保留").length,
    ).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    expect(await screen.findByText("第二版成功")).toBeInTheDocument();
    expect(
      screen.getAllByText("第一版失败，但已有结果必须保留").length,
    ).toBeGreaterThan(0);
    expect(fetchStream).toHaveBeenCalledTimes(2);
  });

  it("keeps a queue-next input dormant when runtime ownership is still unresolved", async () => {
    const fetchStream = vi.fn(
      async () =>
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "response-runtime-outlives-stream",
            status: "completed",
            metadata: { run_id: "run-runtime-outlives-stream" },
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          theme: { rightHeader: <RuntimeLifecycleProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "先给出阶段结果" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    await screen.findByRole("button", { name: /发\s*送/ });

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime active" }),
    );

    expect(
      await screen.findByRole("button", { name: "停止当前任务" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /发\s*送/ }),
    ).not.toBeInTheDocument();

    fireEvent.change(textarea, { target: { value: "这条先排队" } });
    await selectActiveRuntimeIntent("排队下一任务");
    expect(await screen.findByText("这条先排队")).toBeInTheDocument();
    expect(fetchStream).toHaveBeenCalledTimes(1);

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime complete" }),
    );
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByRole("button", { name: /发\s*送/ }),
    ).toBeEnabled();
    expect(screen.getByText("这条先排队")).toBeInTheDocument();
  });

  it("renders one timestamp avatar and action group for a tool-heavy turn", async () => {
    const identity = {
      run_id: "run-visual-contract",
      client_turn_id: "client-visual-contract",
    };
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                sse({
                  object: "response",
                  id: "response-visual-contract",
                  status: "in_progress",
                  metadata: identity,
                }),
              );
              controller.enqueue(
                sse({
                  object: "message",
                  id: "reasoning-visual-contract",
                  type: "reasoning",
                  status: "completed",
                  role: "assistant",
                  content: [
                    { type: "text", text: "先判断", status: "completed" },
                  ],
                  metadata: identity,
                }),
              );
              for (let index = 1; index <= 3; index += 1) {
                controller.enqueue(
                  sse({
                    object: "message",
                    id: `tool-visual-contract-${index}`,
                    type: "plugin_call",
                    status: "completed",
                    role: "assistant",
                    content: [
                      {
                        type: "text",
                        text: `tool-${index}`,
                        status: "completed",
                      },
                    ],
                    metadata: identity,
                  }),
                );
              }
              controller.enqueue(
                sse({
                  object: "message",
                  id: "answer-visual-contract",
                  type: "message",
                  status: "completed",
                  role: "assistant",
                  content: [
                    { type: "text", text: "完整回答", status: "completed" },
                  ],
                  metadata: identity,
                }),
              );
              controller.enqueue(
                sse({
                  object: "response",
                  id: "response-visual-contract",
                  status: "completed",
                  metadata: identity,
                }),
              );
              controller.close();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: TurnBoundaryContractCard },
          actions: {
            list: [{ render: () => <button type="button">复制</button> }],
          },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "执行一个多工具任务" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    expect(await screen.findByText("完整回答")).toBeInTheDocument();
    expect(document.querySelectorAll(".lantai-bubble-assistant")).toHaveLength(
      1,
    );
    expect(
      document.querySelectorAll(".lantai-conversation-time-anchor"),
    ).toHaveLength(1);
    const timeAnchor = document.querySelector(
      ".lantai-conversation-time-anchor",
    );
    expect(timeAnchor?.nextElementSibling).toHaveClass("lantai-bubble-user");
    expect(
      document.querySelectorAll('[data-lantai-response-avatar="1"]'),
    ).toHaveLength(1);
    expect(
      document.querySelectorAll('[data-lantai-response-actions="1"]'),
    ).toHaveLength(1);
  });

  it("opens every restored turn with its user timestamp boundary", async () => {
    const user = (id: string, text: string, createdAt: string) => ({
      id,
      role: "user" as const,
      createdAt,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [{ type: "text", text }],
              },
            ],
          },
        },
      ],
    });
    const assistant = (id: string, text: string, turnId: string) => ({
      id,
      role: "assistant" as const,
      cards: [
        {
          code: responseCardCode,
          data: {
            id: `${id}-response`,
            object: "response",
            status: "completed",
            metadata: { client_turn_id: turnId },
            output: [
              {
                id: `${id}-answer`,
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text }],
                metadata: { client_turn_id: turnId },
              },
            ],
          },
        },
      ],
    });
    const restored = [
      user("user-1", "第一条输入", "2026-08-25T05:01:00Z"),
      assistant("assistant-1", "第一条回复", "turn-1"),
      user("user-2", "第二条输入", "2026-08-25T05:02:00Z"),
      assistant("assistant-2", "第二条回复", "turn-2"),
    ];

    render(
      <LantaiRuntimeWebUI
        options={{
          cards: { [responseCardCode]: StreamCard },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: restored },
              ],
              getSession: async () => ({
                id: "session-1",
                messages: restored,
              }),
            },
          },
        }}
      />,
    );

    expect(await screen.findByText("第二条回复")).toBeInTheDocument();
    const rows = Array.from(
      document.querySelectorAll(".lantai-bubble-list > *"),
    );
    expect(
      rows.map((row) =>
        row.classList.contains("lantai-conversation-time-anchor")
          ? "time"
          : row.classList.contains("lantai-bubble-user")
            ? "user"
            : row.classList.contains("lantai-bubble-assistant")
              ? "assistant"
              : "other",
      ),
    ).toEqual(["time", "user", "assistant", "time", "user", "assistant"]);
  });

  it("keeps a long tool turn alive across structured runtime progress frames", async () => {
    let finishStream: (() => void) | null = null;
    const fetchStream = vi.fn(async () => {
      const identity = {
        run_id: "run-long-tool",
        client_turn_id: "client-turn-long-tool",
      };
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(
              sse({
                object: "response",
                id: "response-long-tool",
                status: "in_progress",
                metadata: identity,
              }),
            );
            for (const object of sharedConsoleStreamControlObjects()) {
              const repetitions = object === "runtime_progress" ? 5 : 1;
              for (let index = 0; index < repetitions; index += 1) {
                controller.enqueue(
                  sse({
                    object,
                    id: `${object}_chat-1_${index}`,
                    status: "running",
                    type:
                      object === "runtime_progress"
                        ? "runtime.progress"
                        : "turn.receipt",
                    summary: "Runtime is active.",
                    receipt: identity,
                    metadata: {
                      ...identity,
                      structured_keepalive: object === "runtime_progress",
                    },
                  }),
                );
              }
            }
            finishStream = () => {
              controller.enqueue(
                sse({
                  object: "message",
                  id: "final-answer-long-tool",
                  type: "message",
                  status: "completed",
                  role: "assistant",
                  content: [
                    {
                      type: "text",
                      text: "Long search complete",
                      status: "completed",
                    },
                  ],
                  metadata: identity,
                }),
              );
              controller.enqueue(
                sse({
                  object: "response",
                  id: "response-long-tool",
                  status: "completed",
                  metadata: identity,
                }),
              );
              controller.close();
            };
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "message" },
          theme: { rightHeader: <RuntimeTransportProbe /> },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("message"), {
      target: { value: "run a long search" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => {
      expect(screen.getByTestId("runtime-transport-status")).toHaveTextContent(
        "connected",
      );
    });
    await act(async () => finishStream?.());
    expect(await screen.findByText("Long search complete")).toBeInTheDocument();
    expect(
      screen.queryByText("\u54cd\u5e94\u4e8b\u4ef6\u683c\u5f0f\u65e0\u6548"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("后端连接已结束但没有返回完成事件"),
    ).not.toBeInTheDocument();
    expect(screen.getAllByTestId("stream-output")).toHaveLength(1);
  });

  it("logs rejected stream schemas without logging payload content", async () => {
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                sse({
                  object: "provider_ping",
                  type: "provider.ping",
                  status: "running",
                  text: "SECRET-STREAM-CONTENT",
                }),
              );
              controller.close();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "diagnostic message" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("diagnostic message"), {
      target: { value: "trigger protocol diagnostics" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    expect(
      (await screen.findAllByText("响应事件格式无效")).length,
    ).toBeGreaterThan(0);
    await waitFor(() => expect(consoleError).toHaveBeenCalled());
    const diagnostics = consoleError.mock.calls.flat().join(" ");
    expect(diagnostics).toContain("provider_ping");
    expect(diagnostics).toContain("object,status,text,type");
    expect(diagnostics).not.toContain("SECRET-STREAM-CONTENT");
    consoleError.mockRestore();
  });

  it("commits direct Thinking before a coalesced answer payload", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "同包流式" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "thinking-coalesced",
          type: "reasoning",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "thinking-coalesced",
          delta: true,
          text: "先展示思考",
          status: "in_progress",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-coalesced",
          type: "message",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "answer-coalesced",
          delta: true,
          text: "再展示回答",
          status: "in_progress",
        }),
      );
    });

    await waitFor(() => {
      const output = screen.getByTestId("stream-output");
      expect(output.querySelector('[data-kind="reasoning"]')).toHaveTextContent(
        "先展示思考",
      );
      expect(output).not.toHaveTextContent("再展示回答");
    });

    await waitFor(
      () =>
        expect(screen.getByTestId("stream-output")).toHaveTextContent(
          "再展示回答",
        ),
      { timeout: 1500 },
    );
  });

  it("buffers the first answer until a delayed Thinking node is committed", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <>
        <section data-chat-thinking="1" data-status="completed">
          上一轮已经完成的思考
        </section>
        <LantaiRuntimeWebUI
          options={{
            api: { fetch: fetchStream },
            cards: { [responseCardCode]: DelayedThinkingStreamCard },
            sender: { placeholder: "说点什么" },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({ id: "session-1", messages: [] }),
                updateSession: async () => [{ id: "session-1", messages: [] }],
              },
            },
          }}
        />
      </>,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "延迟提交思考" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "thinking-delayed",
          type: "reasoning",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "thinking-delayed",
          delta: true,
          text: "延迟出现的思考",
          status: "in_progress",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-delayed",
          type: "message",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "answer-delayed",
          delta: true,
          text: "不能提前泄露的回答",
          status: "in_progress",
        }),
      );
    });

    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 560));
    });
    expect(screen.getByTestId("delayed-stream-output")).not.toHaveTextContent(
      "不能提前泄露的回答",
    );

    await waitFor(
      () =>
        expect(
          screen
            .getByTestId("delayed-stream-output")
            .querySelector('[data-chat-thinking="1"]'),
        ).toHaveTextContent("延迟出现的思考"),
      { timeout: 1200 },
    );
    expect(screen.getByTestId("delayed-stream-output")).not.toHaveTextContent(
      "不能提前泄露的回答",
    );

    await waitFor(
      () =>
        expect(screen.getByTestId("delayed-stream-output")).toHaveTextContent(
          "不能提前泄露的回答",
        ),
      { timeout: 1200 },
    );
  });

  it("keeps the direct model stream as the visible transcript writer even with a legacy canonical setting", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(
      async () =>
        new Response(stream, {
          headers: { "Content-Type": "text/event-stream" },
        }),
    );
    let refreshMayReturnBackendHistory = false;
    const getSessionList = vi.fn(async () => [
      {
        id: "session-1",
        messages: refreshMayReturnBackendHistory
          ? [
              {
                id: "backend-user",
                role: "user",
                cards: [],
              },
              {
                id: "backend-final-answer",
                role: "assistant",
                cards: [
                  {
                    code: responseCardCode,
                    data: {
                      id: "backend-final-response",
                      status: "completed",
                      output: [
                        {
                          id: "backend-final-output",
                          type: "message",
                          status: "completed",
                          role: "assistant",
                          content: [
                            {
                              type: "text",
                              text: "backend history must not bypass runtime",
                            },
                          ],
                        },
                      ],
                    },
                  },
                ],
              },
            ]
          : [],
      },
    ]);

    render(
      <LantaiRuntimeWebUI
        options={{
          api: {
            fetch: fetchStream,
            messageSource: "canonical-runtime",
          },
          cards: { [responseCardCode]: StreamCard },
          theme: {
            rightHeader: (
              <>
                <PrimaryStreamOwnerProbe />
                <RuntimeRescuePatchButtons />
              </>
            ),
          },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList,
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "canonical request" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledOnce());
    refreshMayReturnBackendHistory = true;

    expect(screen.getByTestId("primary-stream-owner")).toHaveTextContent(
      "true",
    );
    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "direct-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "direct answer stays visible" }],
        }),
      );
      streamController!.enqueue(
        sse({ object: "response", id: "direct-response", status: "completed" }),
      );
      streamController!.close();
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
    await waitFor(() => expect(getSessionList).toHaveBeenCalledTimes(2));
    expect(screen.getByText("direct answer stays visible")).toBeInTheDocument();
    expect(
      screen.queryByText("backend history must not bypass runtime"),
    ).toBeNull();

    fireEvent.click(
      screen.getByRole("button", { name: "Apply runtime rescue" }),
    );
    expect(
      await screen.findByText("runtime rescued answer"),
    ).toBeInTheDocument();
  });

  it("reconciles a completed direct stream to the durable chronological timeline", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    let turnCompleted = false;
    const turnMetadata = { client_turn_id: "timeline-turn" };
    const output = (id: string, type: string, text: string) => ({
      id,
      object: "message",
      type,
      role: type === "plugin_call_output" ? "tool" : "assistant",
      status: "completed",
      content: [{ type: "text", text }],
      metadata: { ...turnMetadata, source_message_id: id },
    });
    const canonicalOutput = [
      output("timeline-thinking", "reasoning", "TIMELINE_THINKING"),
      output("timeline-tool-call", "plugin_call", "TIMELINE_TOOL_CALL"),
      output(
        "timeline-tool-result",
        "plugin_call_output",
        "TIMELINE_TOOL_RESULT",
      ),
      output("timeline-answer", "message", "TIMELINE_FINAL_ANSWER"),
      output("timeline-echo", "message", "~>[echo] TIMELINE_QUQU_ECHO"),
    ];
    const groupedLiveOutput = [
      canonicalOutput[0],
      canonicalOutput[3],
      canonicalOutput[1],
      canonicalOutput[2],
      canonicalOutput[4],
    ];
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              streamController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const getSession = vi.fn(async () => ({
      id: "session-1",
      messages: turnCompleted
        ? [
            {
              id: "persisted-timeline-user",
              role: "user" as const,
              cards: [
                {
                  code: "RequestCard",
                  data: {
                    input: [
                      {
                        role: "user",
                        type: "message",
                        content: [{ type: "text", text: "恢复时间线" }],
                      },
                    ],
                  },
                },
              ],
            },
            {
              id: "persisted-timeline-assistant",
              role: "assistant" as const,
              msgStatus: "finished",
              cards: [
                {
                  code: responseCardCode,
                  data: {
                    id: "persisted-timeline-response",
                    object: "response",
                    status: "completed",
                    metadata: {
                      ...turnMetadata,
                      canonical_history_timeline: true,
                    },
                    output: canonicalOutput,
                  },
                },
              ],
            },
          ]
        : [],
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "恢复时间线" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession,
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("恢复时间线");
    fireEvent.change(textarea, { target: { value: "恢复时间线" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledOnce());

    act(() => {
      streamController!.enqueue(
        sse({
          object: "response",
          id: "live-timeline-response",
          status: "completed",
          metadata: turnMetadata,
          output: groupedLiveOutput,
        }),
      );
      turnCompleted = true;
      streamController!.close();
    });

    await waitFor(() =>
      expect(getSession.mock.calls.length).toBeGreaterThan(1),
    );
    const thinking = await screen.findByText("TIMELINE_THINKING");
    const toolCall = screen.getByText("TIMELINE_TOOL_CALL");
    const toolResult = screen.getByText("TIMELINE_TOOL_RESULT");
    const answer = screen.getByText("TIMELINE_FINAL_ANSWER");
    const echo = screen.getByText("~>[echo] TIMELINE_QUQU_ECHO");
    const ordered = [thinking, toolCall, toolResult, answer, echo];
    ordered.slice(0, -1).forEach((node, index) => {
      expect(
        node.compareDocumentPosition(ordered[index + 1]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    });
  });

  it("ignores an empty completed response for an already deduped client turn", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "网络重试去重" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-deduped",
          status: "completed",
          output: [],
          metadata: {
            deduped_client_turn: true,
            duplicate_of_run_id: "run-1",
          },
        }),
      );
      streamController!.close();
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled(),
    );
    expect(screen.queryByTestId("stream-output")).not.toBeInTheDocument();
    expect(
      screen.queryByText("后端完成了请求但没有返回可显示内容"),
    ).not.toBeInTheDocument();
  });

  it("fences rapid submits while beforeSubmit is still pending", async () => {
    let releaseBeforeSubmit: (value?: boolean | void) => void = () => {};
    const beforeSubmit = vi.fn(
      () =>
        new Promise<boolean | void>((resolve) => {
          releaseBeforeSubmit = resolve;
        }),
    );
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(
      async () =>
        new Response(stream, {
          headers: { "Content-Type": "text/event-stream" },
        }),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么", beforeSubmit },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async (patch) => [
                {
                  id: "session-1",
                  messages: (patch as any).messages || [],
                },
              ],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "快速连发" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(beforeSubmit).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /停\s*止/ })).toBeEnabled(),
    );
    expect(fetchStream).not.toHaveBeenCalled();

    act(() => releaseBeforeSubmit());

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getAllByText("快速连发")).toHaveLength(1),
    );

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-rapid-submit",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            {
              object: "content",
              status: "completed",
              type: "text",
              index: 0,
              text: "已收到",
            },
          ],
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-rapid-submit",
          status: "completed",
        }),
      );
      streamController!.close();
    });

    await waitFor(() => expect(screen.getByText("已收到")).toBeInTheDocument());
    expect(fetchStream).toHaveBeenCalledTimes(1);
  });

  it("keeps streamed text when the completed response carries empty output", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试 final output" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-1",
          type: "message",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "answer-1",
          delta: true,
          text: "已经流出来的正文",
          status: "in_progress",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-1",
          status: "completed",
          output: [],
        }),
      );
      streamController!.close();
    });

    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "已经流出来的正文",
      ),
    );
    expect(
      screen
        .getByTestId("stream-output")
        .querySelector('[data-kind="message"]'),
    ).toHaveAttribute("data-status", "completed");
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("keeps visible thinking when a response snapshot adds tool calls", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "先思考再使用工具" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    const toolOutput = {
      id: "tool-call-1",
      object: "message",
      type: "plugin_call",
      status: "completed",
      role: "assistant",
      content: [
        {
          type: "function_call",
          data: { call_id: "call-1", name: "read_file" },
        },
      ],
    };
    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "thinking-before-tool",
          type: "reasoning",
          status: "completed",
          role: "assistant",
          metadata: { run_id: "turn-thinking-tool" },
          content: [{ type: "text", text: "这段思考必须一直保留" }],
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-with-tool",
          status: "in_progress",
          metadata: { run_id: "turn-thinking-tool" },
          output: [toolOutput],
        }),
      );
      streamController!.enqueue(
        sse({
          object: "response",
          id: "response-with-tool",
          status: "completed",
          metadata: { run_id: "turn-thinking-tool" },
          output: [toolOutput],
        }),
      );
      streamController!.close();
    });

    await waitFor(() =>
      expect(screen.getByText("这段思考必须一直保留")).toBeInTheDocument(),
    );
    expect(
      screen
        .getByTestId("stream-output")
        .querySelector('[data-kind="plugin_call"]'),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("renders a visible failure when the completed response has no output", async () => {
    const fetchEmptyCompleted = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                sse({
                  object: "response",
                  id: "response-empty-completed",
                  status: "completed",
                  output: [],
                }),
              );
              controller.close();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchEmptyCompleted },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试空 completed" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(fetchEmptyCompleted).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "后端完成了请求但没有返回可显示内容",
      ),
    );
  });

  it("renders a non-SSE JSON failed response instead of silently dropping it", async () => {
    const fetchJsonError = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            id: "response-error-json",
            object: "response",
            status: "failed",
            type: "response.failed",
            output: [],
            error: { message: "Please configure a model first" },
          }),
          { status: 400, headers: { "Content-Type": "application/json" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchJsonError },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试错误" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "Please configure a model first",
      ),
    );
  });

  it("does not let an empty backend refresh erase a just-submitted local failure", async () => {
    let resolveSession: (session: any) => void = () => {};
    const getSessionList = vi
      .fn()
      .mockResolvedValueOnce([{ id: "session-1", messages: [] }])
      .mockResolvedValue([]);
    const getSession = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveSession = resolve;
        }),
    );
    const fetchJsonError = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            id: "response-error-json",
            object: "response",
            status: "failed",
            type: "response.failed",
            output: [],
            error: { message: "Please configure a model first" },
          }),
          { status: 400, headers: { "Content-Type": "application/json" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchJsonError },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList,
              getSession,
              updateSession: async () => [],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "刚发送的本地消息" } });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(screen.getByText("刚发送的本地消息")).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "Please configure a model first",
      ),
    );
    await waitFor(() => expect(getSessionList).toHaveBeenCalledTimes(2));

    await act(async () => {
      resolveSession({ id: "session-1", messages: [] });
    });

    expect(screen.getByText("刚发送的本地消息")).toBeInTheDocument();
    expect(screen.getByTestId("stream-output")).toHaveTextContent(
      "Please configure a model first",
    );
  });

  it("renders a visible failed response when a 200 stream is empty", async () => {
    const fetchEmptyStream = vi.fn(
      async () =>
        new Response("", {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchEmptyStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试空流" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "后端没有返回任何响应事件",
      ),
    );
  });

  it("renders a visible failure when a stream closes without a terminal response", async () => {
    const fetchNoTerminalResponse = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(
                sse({
                  object: "message",
                  id: "answer-no-terminal",
                  type: "message",
                  status: "in_progress",
                  role: "assistant",
                }),
              );
              controller.enqueue(
                sse({
                  object: "content",
                  msg_id: "answer-no-terminal",
                  delta: true,
                  text: "已经返回一点内容",
                  status: "in_progress",
                }),
              );
              controller.close();
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchNoTerminalResponse },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试缺少 terminal" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => {
      const output = screen.getByTestId("stream-output");
      expect(output).toHaveTextContent("已经返回一点内容");
      expect(output).toHaveTextContent("后端连接已结束但没有返回完成事件");
    });
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("renders a visible failed response when stream parsing fails", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("data: not-json\n\n"));
        controller.close();
      },
    });
    const fetchMalformedStream = vi.fn(async () => new Response(stream));
    const parseStreamPayload = vi.fn(() => {
      throw new Error("bad stream payload");
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: {
            fetch: fetchMalformedStream,
            responseParser: parseStreamPayload,
          },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试坏流" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(parseStreamPayload).toHaveBeenCalledTimes(2));
    expect(parseStreamPayload.mock.calls[0]?.[0]).toBe("not-json");
    expect(parseStreamPayload.mock.calls[1]?.[0]).toContain("response.failed");
    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "bad stream payload",
      ),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("renders a visible failed response when stream parser returns no payload", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(sse({ object: "message", id: "message-1" }));
        controller.close();
      },
    });
    const fetchInvalidPayload = vi.fn(async () => new Response(stream));
    const parseStreamPayload = vi.fn(() => undefined);

    render(
      <LantaiRuntimeWebUI
        options={{
          api: {
            fetch: fetchInvalidPayload,
            responseParser: parseStreamPayload,
          },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试空 payload" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(parseStreamPayload).toHaveBeenCalledTimes(2));
    expect(parseStreamPayload.mock.calls[1]?.[0]).toContain("response.failed");
    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "响应事件格式无效",
      ),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("keeps partial text and renders an error when a stream fails mid-response", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchPartialThenFails = vi.fn(async () => new Response(stream));
    const parseStreamPayload = vi.fn((text: string) => {
      if (text === "stream-broken") {
        throw new Error("stream exploded");
      }
      return JSON.parse(text);
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: {
            fetch: fetchPartialThenFails,
            responseParser: parseStreamPayload,
          },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "测试半截失败" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(fetchPartialThenFails).toHaveBeenCalledTimes(1));

    act(() => {
      streamController!.enqueue(
        sse({
          object: "message",
          id: "answer-1",
          type: "message",
          status: "in_progress",
          role: "assistant",
        }),
      );
      streamController!.enqueue(
        sse({
          object: "content",
          msg_id: "answer-1",
          delta: true,
          text: "半截正文",
          status: "in_progress",
        }),
      );
      streamController!.enqueue(
        new TextEncoder().encode("data: stream-broken\n\n"),
      );
      streamController!.close();
    });

    await waitFor(() => {
      const output = screen.getByTestId("stream-output");
      expect(output).toHaveTextContent("半截正文");
      expect(output).toHaveTextContent("stream exploded");
    });
    expect(
      screen
        .getByTestId("stream-output")
        .querySelector('[data-kind="message"]'),
    ).toHaveAttribute("data-status", "failed");
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("preserves a delivered artifact when a later step fails", async () => {
    const fetchPartialSuccess = vi.fn(async () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            object: "response",
            id: "response-partial-artifact",
            status: "failed",
            output: [
              {
                id: "file-1",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [
                  {
                    type: "file",
                    file_url: "/files/resume.pdf",
                    file_name: "简历.pdf",
                    status: "completed",
                  },
                ],
              },
            ],
            error: { message: "PDF 文本校验脚本失败" },
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchPartialSuccess },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "生成简历 PDF" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => {
      const output = screen.getByTestId("stream-output");
      expect(output).toHaveTextContent("部分完成：已生成的产物已保留");
      expect(output).toHaveTextContent("PDF 文本校验脚本失败");
      expect(output.querySelector('[data-kind="message"]')).toHaveAttribute(
        "data-status",
        "completed",
      );
    });
  });

  it("does not label a partial delivery as a completed response", async () => {
    const fetchPartialDelivery = vi.fn(async () =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            object: "response",
            id: "response-partial-delivery",
            status: "completed",
            type: "response.completed",
            output: [
              {
                id: "file-partial-delivery",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [
                  {
                    type: "file",
                    file_url: "/files/deck.html",
                    file_name: "deck.html",
                    status: "completed",
                  },
                ],
              },
            ],
            metadata: {
              outcome_status: "incomplete",
              delivery_status: "partial",
              partial_success: true,
              completion_gate_warning:
                "文件已生成并保留，但质量校验尚未全部通过。",
            },
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchPartialDelivery },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("说点什么"), {
      target: { value: "生成并验证 PPT" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => {
      const output = screen.getByTestId("stream-output");
      expect(output).toHaveAttribute("data-response-status", "failed");
      expect(output.querySelector('[data-kind="message"]')).toHaveAttribute(
        "data-status",
        "completed",
      );
    });
  });

  it("submits the current textarea value when Enter is pressed", async () => {
    const fetchSend = vi.fn(async () => new Response("", { status: 200 }));
    const getRequestBizParams = vi.fn(() => ({
      deep_thinking: { enabled: true, effort: "high" },
      meta: { request_snapshot: "visible-controls" },
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchSend, getRequestBizParams },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, {
      target: { value: "来自 Enter 的发送" },
    });
    fireEvent.keyDown(textarea, {
      key: "Enter",
      code: "Enter",
      shiftKey: false,
    });

    await waitFor(() => expect(fetchSend).toHaveBeenCalledTimes(1));
    expect(getRequestBizParams).toHaveBeenCalledTimes(1);
    const request = fetchSend.mock.calls[0]?.[0] as any;
    expect(request.input[0]).toMatchObject({
      role: "user",
      type: "message",
      content: [{ type: "text", text: "来自 Enter 的发送" }],
    });
    expect(request.biz_params).toMatchObject({
      deep_thinking: { enabled: true, effort: "high" },
      meta: {
        request_snapshot: "visible-controls",
        client_turn_id: expect.any(String),
      },
    });
  });

  it("keeps prior context visible above a newly submitted message after several turns", async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        streamController = controller;
      },
    });
    const fetchStream = vi.fn(async () => new Response(stream));
    const history = Array.from({ length: 8 }, (_, index) => ({
      id: `history-${index}`,
      role: index % 2 === 0 ? ("user" as const) : ("assistant" as const),
      cards: [],
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", messages: history },
              ],
              getSession: async () => ({
                id: "session-1",
                messages: history,
              }),
              updateSession: async () => [
                { id: "session-1", messages: history },
              ],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    const messageList = document.querySelector<HTMLDivElement>(
      ".lantai-chat-anywhere-message-list",
    )!;
    const composer = document.querySelector<HTMLDivElement>(
      ".lantai-chat-anywhere-input",
    )!;
    const scrollTo = vi.fn();
    Object.defineProperties(messageList, {
      clientHeight: { configurable: true, value: 700 },
      scrollHeight: { configurable: true, value: 1_800 },
      scrollTop: { configurable: true, writable: true, value: 1_100 },
      scrollTo: { configurable: true, value: scrollTo },
    });

    const rect = (top: number, bottom: number, width = 900): DOMRect =>
      ({
        x: 0,
        y: top,
        top,
        right: width,
        bottom,
        left: 0,
        width,
        height: bottom - top,
        toJSON: () => ({}),
      }) as DOMRect;
    const originalGetBoundingClientRect =
      HTMLElement.prototype.getBoundingClientRect;
    const getBoundingClientRect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function () {
        if (this === messageList) return rect(0, 700);
        if (this === composer) return rect(600, 700);
        if (
          this instanceof HTMLElement &&
          this.dataset.lantaiMessageId &&
          this.textContent?.includes("新的多轮问题")
        ) {
          const top = 900 - messageList.scrollTop;
          return rect(top, top + 40, 720);
        }
        return originalGetBoundingClientRect.call(this);
      });

    try {
      fireEvent.change(textarea, {
        target: { value: "新的多轮问题" },
      });
      fireEvent.keyDown(textarea, {
        key: "Enter",
        code: "Enter",
        shiftKey: false,
      });

      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
      await act(
        () =>
          new Promise<void>((resolve) =>
            window.requestAnimationFrame(() =>
              window.requestAnimationFrame(() => resolve()),
            ),
          ),
      );

      const submittedMessage = Array.from(
        messageList.querySelectorAll<HTMLElement>("[data-lantai-message-id]"),
      ).find((node) => node.textContent?.includes("新的多轮问题"));
      expect(submittedMessage).toBeDefined();
      const submittedTop = submittedMessage!.getBoundingClientRect().top;
      expect(submittedTop).toBeGreaterThanOrEqual(280);
      expect(submittedTop).toBeLessThanOrEqual(420);

      // Repositioning the submitted message can move an existing multi-turn
      // conversation upward. That programmatic scroll must not be mistaken for
      // the user leaving the live tail, otherwise thinking/answer updates land
      // below the composer until the user scrolls manually.
      fireEvent.scroll(messageList);
      scrollTo.mockClear();
      act(() => {
        streamController!.enqueue(
          sse({
            object: "message",
            id: "multiturn-answer",
            type: "reasoning",
            status: "in_progress",
            role: "assistant",
          }),
        );
        streamController!.enqueue(
          sse({
            object: "content",
            msg_id: "multiturn-answer",
            delta: true,
            thinking: "正在理解第二轮问题",
            status: "in_progress",
          }),
        );
      });
      await waitFor(() =>
        expect(screen.getByTestId("stream-output")).toHaveTextContent(
          "正在理解第二轮问题",
        ),
      );
      await act(
        () =>
          new Promise<void>((resolve) =>
            window.requestAnimationFrame(() => resolve()),
          ),
      );
      expect(scrollTo).toHaveBeenCalledWith({
        top: 1_800,
        behavior: "auto",
      });
    } finally {
      getBoundingClientRect.mockRestore();
      act(() => {
        streamController!.close();
      });
    }
  });

  it("renders a visible failed response when fetch throws before streaming", async () => {
    const fetchThrows = vi.fn(async () => {
      throw new Error("backend refused");
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchThrows },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, {
      target: { value: "测试失败可见性" },
    });
    await waitFor(() => expect(textarea).toHaveValue("测试失败可见性"));
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(fetchThrows).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "backend refused",
      ),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("renders a visible failed response when beforeSubmit throws", async () => {
    const fetchStream = vi.fn();
    const responseParser = vi.fn((chunk: string) => JSON.parse(chunk));
    const beforeSubmit = vi.fn(async () => {
      throw new Error("发送前检查失败");
    });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, responseParser },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么", beforeSubmit },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, {
      target: { value: "测试发送前异常" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(beforeSubmit).toHaveBeenCalledTimes(1));
    expect(fetchStream).not.toHaveBeenCalled();
    expect(responseParser).toHaveBeenCalledOnce();
    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "发送前检查失败",
      ),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("renders a visible failed response when fetch returns no Response", async () => {
    const fetchNoResponse = vi.fn(async () => undefined);

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchNoResponse },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, {
      target: { value: "测试无响应" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    await waitFor(() => expect(fetchNoResponse).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId("stream-output")).toHaveTextContent(
        "请求没有返回可读取的响应",
      ),
    );
    expect(screen.getByRole("button", { name: /发\s*送/ })).toBeEnabled();
  });

  it("defaults Enter during generation to supplementing the active task", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(
                  sse({
                    object: "turn_receipt",
                    status: "pending",
                    receipt: {
                      schema_version: 2,
                      command_id: "control-input-2",
                      operation: "steer",
                      admission_status: "admitted",
                      execution_status: "pending",
                    },
                  }),
                );
                // Keep the transport open. Admission must render from the
                // first complete receipt frame instead of waiting for EOF.
              },
            }),
          ),
      );
    let resolveControlStatus:
      | ((value: { execution_status: string }) => void)
      | undefined;
    const getControlInputStatus = vi.fn(
      () =>
        new Promise<{ execution_status: string }>((resolve) => {
          resolveControlStatus = resolve;
        }),
    );
    const getRequestBizParams = vi.fn(() => ({
      deep_thinking: { enabled: true, effort: "high" },
    }));
    const beforeSubmit = vi.fn(async () => true);

    render(
      <LantaiRuntimeWebUI
        options={{
          api: {
            fetch: fetchStream,
            getRequestBizParams,
            getControlInputStatus,
          },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么", beforeSubmit },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", name: "已生成标题", messages: [] },
              ],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "第一轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(beforeSubmit).toHaveBeenCalledTimes(1);
    expect(
      screen.queryByRole("button", { name: "选择运行中输入方式" }),
    ).not.toBeInTheDocument();
    expect(textarea).toHaveAttribute(
      "aria-keyshortcuts",
      "Enter Meta+Enter Control+Enter Alt+Enter",
    );

    fireEvent.change(textarea, { target: { value: "第二轮" } });
    expect(
      screen.getByRole("button", { name: "选择运行中输入方式" }),
    ).toBeVisible();
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(textarea).toHaveValue("");
    expect(beforeSubmit).toHaveBeenCalledTimes(1);
    expect(fetchStream.mock.calls[1]?.[0].input[0]).toMatchObject({
      content: [{ type: "text", text: "第二轮" }],
    });
    expect(fetchStream.mock.calls[1]?.[0].biz_params).toMatchObject({
      operation: "steer",
      deep_thinking: { enabled: true, effort: "high" },
      meta: {
        client_turn_id: expect.any(String),
        control_intent: "supplement",
      },
    });
    expect(getRequestBizParams).toHaveBeenCalledTimes(2);
    expect(
      await screen.findByText("已接收 · 等待当前任务安全边界"),
    ).toBeInTheDocument();
    expect(screen.queryByText("已调整当前方向")).not.toBeInTheDocument();
    expect(getControlInputStatus).toHaveBeenCalledWith({
      session_id: "session-1",
      chat_id: "session-1",
      command_id: expect.any(String),
    });
    await act(async () => {
      resolveControlStatus?.({ execution_status: "applied" });
      await Promise.resolve();
    });
    expect(await screen.findByText("已应用到模型请求")).toBeInTheDocument();

    act(() => {
      activeController!.enqueue(
        sse({
          object: "message",
          id: "answer-1",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            { type: "text", text: "已结合追加要求", status: "completed" },
          ],
        }),
      );
      activeController!.enqueue(
        sse({ object: "response", id: "response-1", status: "completed" }),
      );
      activeController!.close();
    });

    expect(await screen.findByText("已结合追加要求")).toBeInTheDocument();
  });

  it("parses a fragmented multi-line SSE receipt before the stream closes", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const encoder = new TextEncoder();
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(
                  encoder.encode(
                    'data: {"object":"turn_receipt",\r\ndata: "receipt":{"schema_',
                  ),
                );
                controller.enqueue(
                  encoder.encode(
                    'version":2,"command_id":"fragmented-command",\r\n',
                  ),
                );
                controller.enqueue(
                  encoder.encode(
                    'data: "admission_status":"admitted","execution_status":"pending",\r\n',
                  ),
                );
                controller.enqueue(
                  encoder.encode('data: "unknown_future_field":true}}\r\n\r\n'),
                );
                // A complete receipt must be enough; this stream deliberately
                // remains open to catch readers that wait for EOF.
              },
            }),
          ),
      );
    const getControlInputStatus = vi.fn(
      () => new Promise<Record<string, unknown>>(() => undefined),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, getControlInputStatus },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "分片多行回执" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("分片多行回执");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "分片引导" } });
    await selectActiveRuntimeIntent("立即调整方向");

    expect(
      await screen.findByText("已接收 · 等待安全让步"),
    ).toBeInTheDocument();
    expect(getControlInputStatus).toHaveBeenCalledWith({
      session_id: "session-1",
      chat_id: "session-1",
      command_id: "fragmented-command",
    });
    expect(activeController).not.toBeNull();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("bounds a silent control receipt stream and reconciles without stopping the primary run", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const cancelSilentReceipt = vi.fn();
    const beforeSubmit = vi.fn(async () => true);
    const getControlInputStatus = vi.fn(async () => {
      throw new TypeError("status temporarily unavailable");
    });
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              cancel() {
                cancelSilentReceipt();
              },
            }),
          ),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: {
            fetch: fetchStream,
            getControlInputStatus,
            turnReceiptTimeoutMs: 20,
          },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "静默回执超时", beforeSubmit },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("静默回执超时");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "静默连接中的改向" } });
    await selectActiveRuntimeIntent("立即调整方向");

    const guidance = await screen.findByRole("article", {
      name: "引导 1：静默连接中的改向",
    });
    await waitFor(() =>
      expect(guidance).toHaveAttribute("data-guidance-status", "uncertain"),
    );
    expect(screen.getByText("接收待确认 · 可安全重试")).toBeInTheDocument();
    expect(cancelSilentReceipt).toHaveBeenCalledTimes(1);
    expect(getControlInputStatus).toHaveBeenCalledWith({
      session_id: "session-1",
      chat_id: "session-1",
      command_id: expect.any(String),
    });
    expect(fetchStream).toHaveBeenCalledTimes(2);
    expect(beforeSubmit).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "停止当前任务" })).toBeVisible();
    expect(activeController).not.toBeNull();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("fails an unknown receipt schema without falling back to normal submit", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const beforeSubmit = vi.fn(async () => true);
    const getControlInputStatus = vi.fn(async () => {
      throw new Error("receipt does not exist");
    });
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 99,
              command_id: "unsupported-command",
              admission_status: "admitted",
              execution_status: "pending",
            },
          }),
        ),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, getControlInputStatus },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "未知回执版本", beforeSubmit },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("未知回执版本");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "不能降级的引导" } });
    await selectActiveRuntimeIntent("立即调整方向");

    const guidance = await screen.findByRole("article", {
      name: "引导 1：不能降级的引导",
    });
    await waitFor(() =>
      expect(guidance).toHaveAttribute("data-guidance-status", "failed"),
    );
    expect(screen.getByText("未插入 · 仍排队")).toBeInTheDocument();
    expect(fetchStream).toHaveBeenCalledTimes(2);
    expect(beforeSubmit).toHaveBeenCalledTimes(1);
    expect(getControlInputStatus).toHaveBeenCalledTimes(1);
    expect(activeController).not.toBeNull();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("treats queue-full as a final control rejection without blind retries", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const beforeSubmit = vi.fn(async () => true);
    const getControlInputStatus = vi.fn(async () => {
      throw new Error("receipt does not exist");
    });
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockRejectedValueOnce(new Error("HTTP 429: queue_full"));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, getControlInputStatus },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "队列容量", beforeSubmit },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("队列容量");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "超过容量的引导" } });
    await selectActiveRuntimeIntent("立即调整方向");

    const guidance = await screen.findByRole("article", {
      name: "引导 1：超过容量的引导",
    });
    await waitFor(() =>
      expect(guidance).toHaveAttribute("data-guidance-status", "failed"),
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 350));
    });
    expect(fetchStream).toHaveBeenCalledTimes(2);
    expect(beforeSubmit).toHaveBeenCalledTimes(1);
    expect(getControlInputStatus).toHaveBeenCalledTimes(1);
    expect(activeController).not.toBeNull();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it.each([
    {
      composerAction: null,
      intent: "supplement",
      waitingReason: "awaiting_model_boundary",
      statusCopy: "已接收 · 等待当前任务安全边界",
    },
    {
      composerAction: "立即调整方向" as const,
      intent: "redirect",
      waitingReason: "awaiting_model_boundary",
      statusCopy: "已接收 · 等待安全让步",
    },
    {
      composerAction: "排队下一任务" as const,
      intent: "queue_next",
      waitingReason: "awaiting_next_turn",
      statusCopy: "已接收 · 等待下一任务",
    },
  ])(
    "persists the explicit $intent intent without opening a second primary run",
    async ({ composerAction, intent, waitingReason, statusCopy }) => {
      let activeController: ReadableStreamDefaultController<Uint8Array> | null =
        null;
      const fetchStream = vi
        .fn()
        .mockImplementationOnce(
          async () =>
            new Response(
              new ReadableStream<Uint8Array>({
                start(controller) {
                  activeController = controller;
                  controller.enqueue(runningTurnIdentitySse());
                },
              }),
            ),
        )
        .mockResolvedValueOnce(
          new Response(
            sse({
              object: "turn_receipt",
              receipt: {
                schema_version: 2,
                command_id: `server-control-${intent}`,
                operation: "steer",
                admission_status: "admitted",
                execution_status: "pending",
                next_waiting_reason: waitingReason,
              },
            }),
          ),
        );
      const getControlInputStatus = vi.fn(
        () => new Promise<Record<string, unknown>>(() => undefined),
      );

      render(
        <LantaiRuntimeWebUI
          options={{
            api: { fetch: fetchStream, getControlInputStatus },
            cards: { [responseCardCode]: StreamCard },
            sender: { placeholder: `运行中三意图-${intent}` },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({ id: "session-1", messages: [] }),
                updateSession: async () => [{ id: "session-1", messages: [] }],
              },
            },
          }}
        />,
      );

      const textarea = await screen.findByPlaceholderText(
        `运行中三意图-${intent}`,
      );
      fireEvent.change(textarea, { target: { value: "主任务" } });
      fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

      fireEvent.change(textarea, {
        target: { value: "补一条运行中要求" },
      });
      if (composerAction) {
        await selectActiveRuntimeIntent(composerAction);
      } else {
        fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
      }

      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
      expect(fetchStream.mock.calls[1]?.[0].biz_params).toMatchObject({
        operation: "steer",
        meta: {
          client_turn_id: expect.any(String),
          control_intent: intent,
        },
      });
      expect(await screen.findByText(statusCopy)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "停止当前任务" }),
      ).toBeVisible();
      expect(activeController).not.toBeNull();

      act(() => {
        activeController!.enqueue(
          sse({ object: "response", id: "primary-done", status: "completed" }),
        );
        activeController!.close();
      });
    },
  );

  it("cancels an admitted control input without interrupting the primary run", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "server-control-cancel",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "pending",
            },
          }),
        ),
      );
    const getControlInputStatus = vi.fn(
      () => new Promise<Record<string, unknown>>(() => undefined),
    );
    const cancelControlInput = vi.fn(async () => ({
      command_id: "server-control-cancel",
      execution_status: "cancelled",
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: {
            fetch: fetchStream,
            getControlInputStatus,
            cancelControlInput,
          },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "取消运行中引导" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("取消运行中引导");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "可取消的引导" } });
    await selectActiveRuntimeIntent("立即调整方向");
    expect(
      await screen.findByText("已接收 · 等待安全让步"),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", {
        name: "删除引导：可取消的引导",
      }),
    );

    await waitFor(() =>
      expect(cancelControlInput).toHaveBeenCalledWith({
        session_id: "session-1",
        chat_id: "session-1",
        command_id: "server-control-cancel",
      }),
    );
    expect(await screen.findByText("未应用 · 已取消")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停止当前任务" })).toBeVisible();
    expect(activeController).not.toBeNull();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("replaces an admitted control input through the same composer", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const admittedReceipt = (commandId: string) =>
      new Response(
        sse({
          object: "turn_receipt",
          receipt: {
            schema_version: 2,
            command_id: commandId,
            operation: "steer",
            admission_status: "admitted",
            execution_status: "pending",
          },
        }),
      );
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockResolvedValueOnce(admittedReceipt("server-control-original"))
      .mockResolvedValueOnce(admittedReceipt("server-control-replacement"));
    const getControlInputStatus = vi.fn(
      () => new Promise<Record<string, unknown>>(() => undefined),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, getControlInputStatus },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "修改运行中引导" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("修改运行中引导");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "原引导" } });
    await selectActiveRuntimeIntent("立即调整方向");
    expect(
      await screen.findByText("已接收 · 等待安全让步"),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "更多引导操作：原引导" }),
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: "编辑" }));
    expect(textarea).toHaveValue("原引导");
    fireEvent.change(textarea, { target: { value: "替换后的引导" } });
    await selectActiveRuntimeIntent("立即调整方向");

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(3));
    expect(fetchStream.mock.calls[2]?.[0].biz_params).toMatchObject({
      operation: "steer",
      meta: {
        client_turn_id: expect.any(String),
        replaces_command_id: "server-control-original",
      },
    });
    expect(
      await screen.findByText("未应用 · 已被后续改向取代"),
    ).toBeInTheDocument();
    expect(screen.getByText("替换后的引导")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停止当前任务" })).toBeVisible();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it.each([
    {
      status: 400,
      code: "control_attachment_not_trusted",
      message: "The attachment was not accepted by the upload boundary.",
      expectedCopy: "附件未被上传边界接受 · 请编辑并重新上传",
      responseShape: "nested-error",
    },
    {
      status: 409,
      code: "target_ended",
      message: "The current run has reached its terminal boundary.",
      expectedCopy: "当前任务已结束 · 内容已保留，未自动启动新任务",
      responseShape: "nested-error",
    },
    {
      status: 401,
      code: "authentication_required",
      message: "Sign in again before sending this control input.",
      expectedCopy: "登录已失效 · 请重新登录后再发送",
      responseShape: "nested-error",
    },
    {
      status: 401,
      code: "http_401",
      message: "Not authenticated",
      expectedCopy: "登录已失效 · 请重新登录后再发送",
      responseShape: "detail",
    },
    {
      status: 403,
      code: "control_attachment_grant_revoked",
      message: "The attachment grant has expired or been revoked.",
      expectedCopy: "附件授权已失效 · 请重新授权或上传",
      responseShape: "nested-error",
    },
    {
      status: 403,
      code: "desktop_token_required",
      message: "Browser pairing required",
      expectedCopy: "桌面连接授权已失效 · 请重新连接后再发送",
      responseShape: "detail-code",
    },
    {
      status: 403,
      code: "http_403",
      message: "Forbidden",
      expectedCopy: "当前授权不允许发送 · 请重新授权后再发送",
      responseShape: "detail",
    },
  ])(
    "keeps the primary run active and the draft recoverable for HTTP $status/$code",
    async ({ status, code, message, expectedCopy, responseShape }) => {
      let activeController: ReadableStreamDefaultController<Uint8Array> | null =
        null;
      const fetchStream = vi
        .fn()
        .mockImplementationOnce(
          async () =>
            new Response(
              new ReadableStream<Uint8Array>({
                start(controller) {
                  activeController = controller;
                  controller.enqueue(runningTurnIdentitySse());
                },
              }),
            ),
        )
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify(
              responseShape === "nested-error"
                ? {
                    object: "response",
                    status: "failed",
                    error: { code, message },
                  }
                : responseShape === "detail-code"
                  ? { detail: message, code }
                  : { detail: message },
            ),
            {
              status,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );

      render(
        <LantaiRuntimeWebUI
          options={{
            api: { fetch: fetchStream },
            cards: { [responseCardCode]: StreamCard },
            sender: { placeholder: "运行中引导" },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({ id: "session-1", messages: [] }),
                updateSession: async () => [{ id: "session-1", messages: [] }],
              },
            },
          }}
        />,
      );

      const textarea = await screen.findByPlaceholderText("运行中引导");
      fireEvent.change(textarea, { target: { value: "主任务" } });
      fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

      fireEvent.change(textarea, { target: { value: "失败的改向" } });
      await selectActiveRuntimeIntent("立即调整方向");

      expect(await screen.findByText(expectedCopy)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "停止当前任务" }),
      ).toBeVisible();
      expect(activeController).not.toBeNull();

      act(() => {
        activeController!.enqueue(
          sse({ object: "response", id: "primary-done", status: "completed" }),
        );
        activeController!.close();
      });

      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(fetchStream).toHaveBeenCalledTimes(2);
      expect(screen.getByText("失败的改向")).toBeInTheDocument();

      fireEvent.click(
        screen.getByRole("button", {
          name: "更多引导操作：失败的改向",
        }),
      );
      fireEvent.click(await screen.findByRole("menuitem", { name: "编辑" }));
      expect(textarea).toHaveValue("失败的改向");
      expect(fetchStream).toHaveBeenCalledTimes(2);
    },
  );

  it("does not auto-start a rejected redirect or an explicit queue-next input", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            object: "response",
            status: "failed",
            error: {
              code: "target_ended",
              message: "The current run has reached its terminal boundary.",
            },
          }),
          {
            status: 409,
            headers: { "Content-Type": "application/json" },
          },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "server-control-queue-next",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "pending",
              next_waiting_reason: "awaiting_next_turn",
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "失败改向与后续任务" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("失败改向与后续任务");
    fireEvent.change(textarea, { target: { value: "健康主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "失败的改向" } });
    await selectActiveRuntimeIntent("立即调整方向");
    expect(
      await screen.findByText("当前任务已结束 · 内容已保留，未自动启动新任务"),
    ).toBeInTheDocument();

    fireEvent.change(textarea, { target: { value: "明确排队的独立后续任务" } });
    await selectActiveRuntimeIntent("排队下一任务");
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(3));
    expect(fetchStream.mock.calls[2]?.[0].biz_params).toMatchObject({
      operation: "steer",
      meta: { control_intent: "queue_next" },
    });

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(3));
    expect(screen.getByText("失败的改向")).toBeInTheDocument();
    expect(
      screen.getByText("当前任务已结束 · 内容已保留，未自动启动新任务"),
    ).toBeInTheDocument();
  });

  it("reconciles a persisted admitted control input after session hydration", async () => {
    const persistedControlMessage = {
      id: "persisted-control-message",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                content: [{ type: "text", text: "刷新后继续核实的改向" }],
              },
            ],
            runtime_queue_status: "admitted",
            runtime_control_command_id: "persisted-control-command",
            runtime_control_intent: "redirect",
          },
        },
      ],
    };
    const persistedSession = {
      id: "chat-owner-1",
      sessionId: "console:shared-transport",
      messages: [persistedControlMessage],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        run_id: "run-1",
        attempt_id: "attempt-1",
        can_reconnect: true,
        can_stop: true,
      },
    };
    let resolveControlStatus:
      | ((value: { execution_status: string }) => void)
      | undefined;
    const getControlInputStatus = vi.fn(
      () =>
        new Promise<{ execution_status: string }>((resolve) => {
          resolveControlStatus = resolve;
        }),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { getControlInputStatus },
          session: {
            api: {
              getSessionList: async () => [persistedSession],
              getSession: async () => persistedSession,
              updateSession: async () => [persistedSession],
            },
          },
        }}
      />,
    );

    expect(
      await screen.findByText("已接收 · 等待安全让步"),
    ).toBeInTheDocument();
    await act(async () => {
      resolveControlStatus?.({ execution_status: "applied" });
      await Promise.resolve();
    });
    expect(await screen.findByText("已应用到模型请求")).toBeInTheDocument();
    expect(getControlInputStatus).toHaveBeenCalledTimes(1);
    expect(getControlInputStatus).toHaveBeenCalledWith({
      session_id: "console:shared-transport",
      chat_id: "chat-owner-1",
      command_id: "persisted-control-command",
    });
  });

  it("returns from the editor with one page-bound guidance and does not resubmit it", async () => {
    const artifactTarget = {
      schema_version: 1,
      chat_id: "chat-editor-return",
      asset_id: "asset-deck",
      work_id: "work-deck-v1",
      edit_path: "/tmp/lantai/topic/quarterly-review.pptx",
      base_revision: {
        version: 1,
        digest: "sha256:deck-v1",
      },
      selection: { kind: "page", page: 3 },
    };
    const persistedSession = {
      id: "session-editor-return",
      messages: [
        {
          id: "persisted-editor-guidance",
          role: "user" as const,
          cards: [
            {
              code: "LantaiRuntimeRequestCard",
              data: {
                input: [
                  {
                    content: [
                      {
                        type: "text",
                        text: "只修改已选中的第三页，其他页面保持不变",
                      },
                    ],
                  },
                ],
                biz_params: {
                  operation: "steer",
                  meta: { artifact_edit_target: artifactTarget },
                },
                runtime_queue_status: "admitted",
                runtime_control_command_id: "editor-return-command",
                runtime_control_intent: "supplement",
              },
            },
          ],
        },
      ],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        run_id: "run-editor-return",
        attempt_id: "run-editor-return:chat-attempt-1",
        can_reconnect: true,
        can_stop: true,
      },
    };
    const fetchStream = vi.fn();
    const getControlInputStatus = vi.fn(async () => ({
      execution_status: "pending",
      next_waiting_reason: "awaiting_model_boundary",
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, getControlInputStatus },
          session: {
            api: {
              getSessionList: async () => [persistedSession],
              getSession: async () => persistedSession,
              updateSession: async () => [persistedSession],
            },
          },
        }}
      />,
    );

    expect(
      await screen.findByText("只修改已选中的第三页，其他页面保持不变"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("已接收 · 等待当前任务安全边界"),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(getControlInputStatus).toHaveBeenCalledWith({
        session_id: "session-editor-return",
        chat_id: "session-editor-return",
        command_id: "editor-return-command",
      }),
    );
    expect(fetchStream).not.toHaveBeenCalled();
    expect(
      persistedSession.messages[0].cards[0].data.biz_params.meta
        .artifact_edit_target,
    ).toEqual(artifactTarget);
  });

  it("binds a queued guidance draft to the run attempt observed at enqueue", async () => {
    const activeSession = {
      id: "session-1",
      messages: [],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        run_id: "run-owned-at-enqueue",
        attempt_id: "run-owned-at-enqueue:chat-attempt-1",
        can_reconnect: true,
        can_stop: true,
      },
    };
    const fetchStream = vi.fn(
      async () =>
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "server-bound-control",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "applied",
            },
          }),
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          sender: { placeholder: "Attempt 归属围栏" },
          session: {
            api: {
              getSessionList: async () => [activeSession],
              getSession: async () => activeSession,
              updateSession: async () => [activeSession],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("Attempt 归属围栏");
    fireEvent.change(textarea, { target: { value: "只属于当前 Attempt" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(fetchStream.mock.calls[0]?.[0].biz_params).toMatchObject({
      operation: "steer",
      meta: {
        expected_run_id: "run-owned-at-enqueue",
        expected_attempt_id: "run-owned-at-enqueue:chat-attempt-1",
      },
    });
  });

  it("waits for the first run identity and binds a selected guidance intent exactly once", async () => {
    const activeSession = {
      id: "session-1",
      messages: [],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        can_reconnect: true,
        can_stop: true,
      },
    };
    const fetchStream = vi.fn(
      async () =>
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "first-run-bound-control",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "applied",
            },
          }),
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          sender: { placeholder: "首轮身份绑定" },
          theme: { rightHeader: <RuntimeIdentityAliasProbe /> },
          session: {
            api: {
              getSessionList: async () => [activeSession],
              getSession: async () => activeSession,
              updateSession: async () => [activeSession],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("首轮身份绑定");
    fireEvent.change(textarea, { target: { value: "首轮启动后立即补充" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    expect(await screen.findByText("等待 Run 身份绑定")).toBeInTheDocument();
    expect(fetchStream).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime aliases" }),
    );

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(fetchStream.mock.calls[0]?.[0].biz_params).toMatchObject({
      operation: "steer",
      meta: {
        expected_run_id: "run-identity-alias-race",
        expected_attempt_id: "attempt-old-identity-alias-race",
      },
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime aliases" }),
    );
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
  });

  it("keeps the streamed run identity when delayed route hydration lacks identity and activity markers", async () => {
    let primaryController:
      | ReadableStreamDefaultController<Uint8Array>
      | undefined;
    let resolveInitialHydration:
      | ((value: {
          id: string;
          messages: never[];
          runtimeState: {
            can_reconnect: boolean;
            can_stop: boolean;
          };
        }) => void)
      | undefined;
    const staleActiveSession = () => ({
      id: "session-1",
      messages: [],
      runtimeState: {
        can_reconnect: false,
        can_stop: false,
      },
    });
    const initialHydration = new Promise<ReturnType<typeof staleActiveSession>>(
      (resolve) => {
        resolveInitialHydration = resolve;
      },
    );
    const getSession = vi
      .fn()
      .mockImplementationOnce(() => initialHydration)
      .mockImplementation(async () => staleActiveSession());
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                primaryController = controller;
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "hydration-race-control",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "applied",
            },
          }),
        ),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "运行身份水合竞态" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession,
              updateSession: async () => [staleActiveSession()],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("运行身份水合竞态");
    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(1));
    fireEvent.change(textarea, { target: { value: "启动主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      primaryController!.enqueue(
        runningTurnIdentitySse(
          "run-hydration-race",
          "run-hydration-race:chat-attempt-1",
        ),
      );
    });
    await act(async () => {
      resolveInitialHydration?.(staleActiveSession());
      await initialHydration;
    });

    fireEvent.change(textarea, {
      target: { value: "身份到达后立即补充" },
    });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(fetchStream.mock.calls[1]?.[0].biz_params).toMatchObject({
      operation: "steer",
      meta: {
        expected_run_id: "run-hydration-race",
        expected_attempt_id: "run-hydration-race:chat-attempt-1",
      },
    });

    act(() => {
      primaryController!.enqueue(
        sse({
          object: "response",
          id: "hydration-race-done",
          status: "completed",
        }),
      );
      primaryController!.close();
    });
  });

  it("does not reuse the previous run identity while a new primary turn is starting", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const previousSession = {
      id: "session-1",
      messages: [],
      generating: false,
      status: "idle",
      runtimeState: {
        status: "completed",
        run_id: "run-previous",
        attempt_id: "run-previous:chat-attempt-1",
        can_reconnect: false,
        can_stop: false,
      },
    };
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
              },
            }),
          ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "new-run-bound-control",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "applied",
            },
          }),
        ),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "新一轮身份隔离" },
          session: {
            api: {
              getSessionList: async () => [previousSession],
              getSession: async () => previousSession,
              updateSession: async () => [previousSession],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("新一轮身份隔离");
    fireEvent.change(textarea, { target: { value: "启动新一轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "只补充新一轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    expect(await screen.findByText("等待 Run 身份绑定")).toBeInTheDocument();
    expect(fetchStream).toHaveBeenCalledTimes(1);

    act(() => {
      activeController!.enqueue(
        runningTurnIdentitySse("run-new", "run-new:chat-attempt-1"),
      );
    });

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(fetchStream.mock.calls[1]?.[0].biz_params).toMatchObject({
      operation: "steer",
      meta: {
        expected_run_id: "run-new",
        expected_attempt_id: "run-new:chat-attempt-1",
      },
    });

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "new-run-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("restores a guidance draft waiting for first-run identity and sends it once", async () => {
    const persistedMessage = {
      id: "persisted-first-run-guidance",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [{ type: "text", text: "刷新后绑定到首轮身份" }],
              },
            ],
            runtime_queue_status: "queued",
            runtime_control_intent: "supplement",
            runtime_control_waiting_reason: "awaiting_run_identity",
          },
        },
      ],
    };
    const persistedSession = {
      id: "session-1",
      messages: [persistedMessage],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        can_reconnect: true,
        can_stop: true,
      },
    };
    const fetchStream = vi.fn(
      async () =>
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "restored-first-run-bound-control",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "applied",
            },
          }),
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          theme: { rightHeader: <RuntimeIdentityAliasProbe /> },
          session: {
            api: {
              getSessionList: async () => [persistedSession],
              getSession: async () => persistedSession,
              updateSession: async () => [persistedSession],
            },
          },
        }}
      />,
    );

    expect(await screen.findByText("等待 Run 身份绑定")).toBeInTheDocument();
    expect(fetchStream).not.toHaveBeenCalled();

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime aliases" }),
    );

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(fetchStream.mock.calls[0]?.[0].biz_params).toMatchObject({
      operation: "steer",
      meta: {
        expected_run_id: "run-identity-alias-race",
        expected_attempt_id: "attempt-old-identity-alias-race",
      },
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Mark runtime aliases" }),
    );
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
  });

  it("keeps hydrated control receipt reconciliation owned by the selected session", async () => {
    const persistedSession = (sessionId: string, commandId: string) => ({
      id: sessionId,
      messages: [
        {
          id: `${sessionId}-control-message`,
          role: "user" as const,
          cards: [
            {
              code: "LantaiRuntimeRequestCard",
              data: {
                input: [
                  {
                    content: [
                      { type: "text", text: `${sessionId} 的待核实改向` },
                    ],
                  },
                ],
                runtime_queue_status: "admitted",
                runtime_control_command_id: commandId,
                runtime_control_intent: "redirect",
              },
            },
          ],
        },
      ],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        run_id: `${sessionId}-run`,
        attempt_id: `${sessionId}-attempt`,
        can_reconnect: true,
        can_stop: true,
      },
    });
    const sessionOne = persistedSession("session-1", "command-1");
    const sessionTwo = persistedSession("session-2", "command-2");
    const getSession = vi.fn(async (sessionId: string) =>
      sessionId === "session-2" ? sessionTwo : sessionOne,
    );
    const getControlInputStatus = vi.fn(
      () => new Promise<{ execution_status: string }>(() => undefined),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { getControlInputStatus },
          theme: { rightHeader: <SessionDraftSwitchButtons /> },
          session: {
            api: {
              getSessionList: async () => [sessionOne, sessionTwo],
              getSession,
              updateSession: async () => [sessionOne, sessionTwo],
            },
          },
        }}
      />,
    );

    expect(
      await screen.findByText("session-1 的待核实改向"),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(getControlInputStatus).toHaveBeenCalledWith({
        session_id: "session-1",
        chat_id: "session-1",
        command_id: "command-1",
      }),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Switch to session two" }),
    );

    expect(
      await screen.findByText("session-2 的待核实改向"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("session-1 的待核实改向"),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(getControlInputStatus).toHaveBeenCalledWith({
        session_id: "session-2",
        chat_id: "session-2",
        command_id: "command-2",
      }),
    );
    expect(getControlInputStatus.mock.calls).not.toContainEqual([
      {
        session_id: "session-1",
        chat_id: "session-1",
        command_id: "command-2",
      },
    ]);
    expect(getControlInputStatus.mock.calls).not.toContainEqual([
      {
        session_id: "session-2",
        chat_id: "session-2",
        command_id: "command-1",
      },
    ]);
  });

  it("marks a persisted sending control input uncertain before receipt lookup finishes", async () => {
    const persistedSession = {
      id: "session-1",
      messages: [
        {
          id: "persisted-sending-message",
          role: "user" as const,
          cards: [
            {
              code: "LantaiRuntimeRequestCard",
              data: {
                input: [
                  {
                    content: [{ type: "text", text: "断线前正在发送的改向" }],
                  },
                ],
                runtime_queue_status: "sending",
                runtime_control_command_id: "persisted-sending-command",
                runtime_control_intent: "redirect",
              },
            },
          ],
        },
      ],
      generating: true,
      status: "running",
      runtimeState: {
        status: "running",
        run_id: "run-1",
        attempt_id: "attempt-1",
        can_reconnect: true,
        can_stop: true,
      },
    };
    const getControlInputStatus = vi.fn(
      () => new Promise<Record<string, unknown>>(() => undefined),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { getControlInputStatus },
          session: {
            api: {
              getSessionList: async () => [persistedSession],
              getSession: async () => persistedSession,
              updateSession: async () => [persistedSession],
            },
          },
        }}
      />,
    );

    expect(
      await screen.findByText("接收待确认 · 可安全重试"),
    ).toBeInTheDocument();
    expect(getControlInputStatus).toHaveBeenCalledWith({
      session_id: "session-1",
      chat_id: "session-1",
      command_id: "persisted-sending-command",
    });
  });

  it("recovers a lost steer response from the durable receipt without stopping the primary run", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockRejectedValueOnce(new TypeError("network disconnected"));
    const getControlInputStatus = vi.fn(async () => ({
      execution_status: "applied",
    }));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, getControlInputStatus },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "断网恢复" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("断网恢复");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "断网中的改向" } });
    await selectActiveRuntimeIntent("立即调整方向");

    expect(await screen.findByText("已应用到模型请求")).toBeInTheDocument();
    expect(getControlInputStatus).toHaveBeenCalledWith({
      session_id: "session-1",
      chat_id: "session-1",
      command_id: expect.any(String),
    });
    expect(screen.getByRole("button", { name: "停止当前任务" })).toBeVisible();
    expect(fetchStream).toHaveBeenCalledTimes(2);

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("retries an uncertain control input with the same command and payload only", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockRejectedValueOnce(new TypeError("network disconnected"))
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "server-control-retry",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "pending",
            },
          }),
        ),
      );
    const getControlInputStatus = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("status unavailable"))
      .mockImplementation(
        () => new Promise<Record<string, unknown>>(() => undefined),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, getControlInputStatus },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "未知回执精确重试" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("未知回执精确重试");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "必须保持原样的改向" } });
    await selectActiveRuntimeIntent("立即调整方向");

    expect(
      await screen.findByText("接收待确认 · 可安全重试"),
    ).toBeInTheDocument();
    const retryButton = screen.getByRole("button", {
      name: "调整方向：必须保持原样的改向",
    });
    expect(retryButton).toBeEnabled();
    expect(retryButton).toHaveTextContent("重试调整方向");
    expect(
      screen.getByRole("button", {
        name: "更多引导操作：必须保持原样的改向",
      }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", {
        name: "删除引导：必须保持原样的改向",
      }),
    ).toBeDisabled();

    fireEvent.click(retryButton);
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(3));
    const firstAttempt = fetchStream.mock.calls[1]?.[0];
    const retryAttempt = fetchStream.mock.calls[2]?.[0];
    expect(retryAttempt.biz_params.meta.client_turn_id).toBe(
      firstAttempt.biz_params.meta.client_turn_id,
    );
    expect(retryAttempt.biz_params.meta.control_intent).toBe("redirect");
    expect(retryAttempt.input).toEqual(firstAttempt.input);
    expect(
      await screen.findByText("已接收 · 等待安全让步"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停止当前任务" })).toBeVisible();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("keeps reconciling a durable receipt after the first status query is unavailable", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockRejectedValueOnce(new TypeError("response lost"));
    const getControlInputStatus = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("temporarily offline"))
      .mockResolvedValueOnce({ execution_status: "applied" });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, getControlInputStatus },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "延迟核实回执" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("延迟核实回执");
    fireEvent.change(textarea, { target: { value: "主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "稍后查到的改向" } });
    await selectActiveRuntimeIntent("立即调整方向");

    expect(
      await screen.findByText("接收待确认 · 可安全重试"),
    ).toBeInTheDocument();
    expect(await screen.findByText("已应用到模型请求")).toBeInTheDocument();
    expect(getControlInputStatus).toHaveBeenCalledTimes(2);
    expect(fetchStream).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "停止当前任务" })).toBeVisible();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "primary-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("starts a new turn when the visible answer settled before the stream closed", async () => {
    const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controllers.push(controller);
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "第一轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      controllers[0].enqueue(
        sse({
          object: "message",
          id: "answer-before-close-1",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            { type: "text", text: "第一轮已完成", status: "completed" },
          ],
        }),
      );
      controllers[0].enqueue(
        sse({
          object: "response",
          id: "response-before-close-1",
          status: "completed",
        }),
      );
    });
    expect(await screen.findByText("第一轮已完成")).toBeInTheDocument();

    fireEvent.change(textarea, { target: { value: "第二轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("等待当前任务")).not.toBeInTheDocument();

    act(() => {
      controllers[1].enqueue(
        sse({
          object: "message",
          id: "answer-before-close-2",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [
            { type: "text", text: "第二轮已完成", status: "completed" },
          ],
        }),
      );
      controllers[1].enqueue(
        sse({
          object: "response",
          id: "response-before-close-2",
          status: "completed",
        }),
      );
      controllers[1].close();
    });

    expect(await screen.findByText("第二轮已完成")).toBeInTheDocument();
    expect(screen.getAllByTestId("stream-output")).toHaveLength(2);
    expect(screen.queryByText("等待当前任务")).not.toBeInTheDocument();
  });

  it.each(["hover", "focus"])(
    "explains composer icon actions on %s without submitting or stopping",
    async (trigger) => {
      let controller: ReadableStreamDefaultController<Uint8Array> | undefined;
      const fetchStream = vi.fn(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(activeController) {
                controller = activeController;
                activeController.enqueue(runningTurnIdentitySse());
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      );
      const cancel = vi.fn();
      render(
        <LantaiRuntimeWebUI
          options={{
            api: { fetch: fetchStream, cancel },
            cards: { [responseCardCode]: StreamCard },
            sender: { placeholder: "图标操作说明" },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({ id: "session-1", messages: [] }),
                updateSession: async () => [{ id: "session-1", messages: [] }],
              },
            },
          }}
        />,
      );
      const textarea = await screen.findByPlaceholderText("图标操作说明");
      const expectHint = async (label: string, hint: string) => {
        const button = screen.getByRole("button", { name: label });
        if (trigger === "hover") fireEvent.mouseEnter(button);
        else fireEvent.focus(button);
        expect(
          await screen.findByText(hint, { selector: '[role="tooltip"]' }),
        ).toBeInTheDocument();
        // JSDOM has no layout for rc-trigger's popup alignment; visibility is
        // verified in the browser smoke, while this locks the event and text.
        if (trigger === "hover") fireEvent.mouseLeave(button);
        else fireEvent.blur(button);
      };

      await expectHint("发送", "发送消息（Enter）");
      expect(fetchStream).not.toHaveBeenCalled();
      fireEvent.change(textarea, { target: { value: "开始任务" } });
      fireEvent.click(screen.getByRole("button", { name: "发送" }));
      await screen.findByRole("button", { name: "停止当前任务" });
      expect(
        screen.getByRole("button", { name: "停止当前任务" }),
      ).toHaveTextContent("停止");
      expect(
        screen.queryByRole("button", { name: "补充当前任务" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "选择运行中输入方式" }),
      ).not.toBeInTheDocument();
      fireEvent.change(textarea, { target: { value: "  \n " } });
      expect(
        screen.queryByRole("button", { name: "补充当前任务" }),
      ).not.toBeInTheDocument();
      fireEvent.change(textarea, { target: { value: "增加一张总结页" } });
      expect(
        screen.getByRole("button", { name: "补充当前任务" }),
      ).toHaveTextContent("补充要求");
      expect(
        screen.getByRole("button", { name: "选择运行中输入方式" }),
      ).toHaveTextContent("其他方式");
      await expectHint(
        "补充当前任务",
        "补充当前任务：发送新要求，继续当前任务（Enter）",
      );
      await expectHint(
        "选择运行中输入方式",
        "更多发送方式：立即调整方向或排队下一任务",
      );
      await expectHint("停止当前任务", "停止当前任务：结束正在进行的执行");
      expect(fetchStream).toHaveBeenCalledTimes(1);
      expect(cancel).not.toHaveBeenCalled();
      expect(textarea).toHaveValue("增加一张总结页");
      fireEvent.change(textarea, { target: { value: "" } });
      expect(
        screen.queryByRole("button", { name: "补充当前任务" }),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "停止当前任务" }),
      ).toBeEnabled();
      act(() => {
        controller!.enqueue(sse({ object: "response", status: "completed" }));
        controller!.close();
      });
    },
  );

  it("keeps Stop available while the primary button supplements and other intents stay explicit", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "server-control-light-strip",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "pending",
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "server-control-light-strip-redirect",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "pending",
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "运行中仍可发送" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("运行中仍可发送");
    fireEvent.change(textarea, { target: { value: "先继续做游戏" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    const stopButton = screen.getByRole("button", { name: "停止当前任务" });
    expect(stopButton).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "发送" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "补充当前任务" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "加入引导队列" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "选择运行中输入方式" }),
    ).not.toBeInTheDocument();
    expect(document.querySelector(".lantai-active-control-rail")).toBeNull();
    const activeActions = stopButton.closest(".lantai-sender-actions");
    expect(activeActions).not.toBeNull();
    expect(
      within(activeActions as HTMLElement).getAllByRole("button"),
    ).toHaveLength(1);
    fireEvent.change(textarea, { target: { value: "画面改成像素风" } });
    expect(screen.getByRole("button", { name: "补充当前任务" })).toBeEnabled();
    expect(
      within(activeActions as HTMLElement).getAllByRole("button"),
    ).toHaveLength(3);
    expect(
      within(activeActions as HTMLElement).getByRole("button", {
        name: "选择运行中输入方式",
      }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "选择运行中输入方式" }));
    expect(
      await screen.findByRole("menuitem", { name: "立即调整方向" }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("menuitem", { name: "排队下一任务" }).length,
    ).toBeGreaterThanOrEqual(1);
    fireEvent.click(screen.getByRole("button", { name: "选择运行中输入方式" }));

    expect(screen.getByRole("button", { name: "停止当前任务" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "补充当前任务" }));

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(fetchStream.mock.calls[1]?.[0].input[0]).toMatchObject({
      content: [{ type: "text", text: "画面改成像素风" }],
    });
    expect(fetchStream.mock.calls[1]?.[0].biz_params.meta).toMatchObject({
      control_intent: "supplement",
    });
    const strip = await screen.findByRole("article", {
      name: "引导 1：画面改成像素风",
    });
    expect(strip).toHaveClass("lantai-guidance-card");
    expect(strip.closest(".lantai-sender-content")).toBeNull();
    expect(strip.parentElement).toHaveClass("lantai-guidance-queue");
    expect(strip.parentElement?.nextElementSibling).toHaveClass(
      "lantai-sender",
    );
    expect(within(strip).getByText("画面改成像素风")).toBeVisible();
    expect(
      within(strip).getByRole("button", {
        name: "调整方向：画面改成像素风",
      }),
    ).toBeEnabled();
    expect(
      within(strip).getByRole("button", {
        name: "删除引导：画面改成像素风",
      }),
    ).toBeEnabled();
    const moreButton = within(strip).getByRole("button", {
      name: "更多引导操作：画面改成像素风",
    });
    expect(moreButton).toBeEnabled();
    expect(
      within(strip).queryByRole("button", {
        name: "补充当前任务：画面改成像素风",
      }),
    ).not.toBeInTheDocument();
    expect(
      within(strip).queryByRole("button", {
        name: "排队下一任务：画面改成像素风",
      }),
    ).not.toBeInTheDocument();
    fireEvent.click(moreButton);
    expect(
      await screen.findByRole("menuitem", { name: "编辑" }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("menuitem", { name: "排队下一任务" }).length,
    ).toBeGreaterThanOrEqual(1);
    fireEvent.click(
      within(strip).getByRole("button", {
        name: "调整方向：画面改成像素风",
      }),
    );
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(3));
    expect(fetchStream.mock.calls[2]?.[0].biz_params).toMatchObject({
      operation: "steer",
      meta: {
        control_intent: "redirect",
        replaces_command_id: "server-control-light-strip",
      },
    });
    expect(
      await screen.findByRole("article", {
        name: "引导 1：画面改成像素风",
      }),
    ).toHaveTextContent("已调整方向");
    expect(activeController).not.toBeNull();

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "active-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("supplements the active task with an image-only input without synthetic user text", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const previousCreateObjectUrl = URL.createObjectURL;
    const previousRevokeObjectUrl = URL.revokeObjectURL;
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:active-image-preview"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
    const uploads: Array<{
      onSuccess: (body: { url?: string }) => void;
    }> = [];
    const customRequest = vi.fn((request) => uploads.push(request));
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "turn_receipt",
            receipt: {
              schema_version: 2,
              command_id: "active-image-only-control",
              operation: "steer",
              admission_status: "admitted",
              execution_status: "applied",
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );

    try {
      render(
        <LantaiRuntimeWebUI
          options={{
            api: { fetch: fetchStream },
            cards: { [responseCardCode]: StreamCard },
            sender: {
              placeholder: "运行中纯图片补充",
              attachments: { customRequest },
            },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({ id: "session-1", messages: [] }),
                updateSession: async () => [{ id: "session-1", messages: [] }],
              },
            },
          }}
        />,
      );

      const textarea = await screen.findByPlaceholderText("运行中纯图片补充");
      fireEvent.change(textarea, { target: { value: "先开始任务" } });
      fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

      const image = new File(["image"], "image.png", { type: "image/png" });
      fireEvent.paste(textarea, {
        clipboardData: {
          items: [{ kind: "file", getAsFile: () => image }],
          files: [image],
        },
      });
      await waitFor(() => expect(customRequest).toHaveBeenCalledTimes(1));
      act(() => uploads[0].onSuccess({ url: "/files/preview/image.png" }));

      expect(textarea).toHaveValue("");
      expect(
        screen.getByRole("button", { name: "补充当前任务" }),
      ).toBeEnabled();
      expect(
        screen.getByRole("button", { name: "选择运行中输入方式" }),
      ).toBeEnabled();
      expect(
        screen.getByRole("button", { name: "停止当前任务" }),
      ).toBeEnabled();

      fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
      const request = fetchStream.mock.calls[1]?.[0];
      expect(request.biz_params).toMatchObject({
        operation: "steer",
        meta: { control_intent: "supplement" },
      });
      const content = request.input[0].content as Array<
        Record<string, unknown>
      >;
      expect(content).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            type: "image",
            image_url: "/files/preview/image.png",
            filename: "image.png",
          }),
        ]),
      );
      const textBlocks = content.filter((item) => item.type === "text");
      expect(textBlocks).toHaveLength(1);
      expect(textBlocks[0]?.text).toMatch(/^<lantai_attachment_roles>/);
      expect(textBlocks[0]?.text).not.toContain("请查看图片");
      expect(await screen.findByText("已应用到模型请求")).toBeInTheDocument();
      expect(activeController).not.toBeNull();
    } finally {
      Object.defineProperty(URL, "createObjectURL", {
        configurable: true,
        value: previousCreateObjectUrl,
      });
      Object.defineProperty(URL, "revokeObjectURL", {
        configurable: true,
        value: previousRevokeObjectUrl,
      });
    }
  });

  it("keeps queued guidance dormant when the user stops the current task", async () => {
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start() {
                // The primary task remains active until the user stops it.
              },
            }),
            { headers: { "Content-Type": "text/event-stream" } },
          ),
      )
      .mockResolvedValueOnce(
        new Response(
          sse({
            object: "response",
            id: "must-not-start",
            status: "completed",
          }),
        ),
      );
    const cancel = vi.fn().mockResolvedValue({ stopped: true });

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream, cancel },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "停止不启动队列" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("停止不启动队列");
    fireEvent.change(textarea, { target: { value: "健康主任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "停止后仍需用户处置" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    expect(
      await screen.findByRole("button", {
        name: "调整方向：停止后仍需用户处置",
      }),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "停止当前任务" }));
    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "停止当前任务" }),
      ).not.toBeInTheDocument(),
    );
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchStream).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("button", {
        name: "调整方向：停止后仍需用户处置",
      }),
    ).toBeVisible();
  });

  it("lets the user edit or delete a queued guide without sending it", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const fetchStream = vi.fn(async () =>
      Promise.resolve(
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              activeController = controller;
            },
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "先执行原任务" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "稍后补充这一点" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    expect(
      await screen.findByRole("article", {
        name: "引导 1：稍后补充这一点",
      }),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", {
        name: "更多引导操作：稍后补充这一点",
      }),
    );
    fireEvent.click(await screen.findByRole("menuitem", { name: "编辑" }));
    expect(textarea).toHaveValue("稍后补充这一点");
    expect(
      screen.queryByRole("article", {
        name: "引导 1：稍后补充这一点",
      }),
    ).not.toBeInTheDocument();
    expect(fetchStream).toHaveBeenCalledTimes(1);

    fireEvent.change(textarea, { target: { value: "改过的引导" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    expect(
      await screen.findByRole("article", { name: "引导 1：改过的引导" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "删除引导：改过的引导" }),
    );
    expect(
      screen.queryByRole("article", { name: "引导 1：改过的引导" }),
    ).not.toBeInTheDocument();
    expect(fetchStream).toHaveBeenCalledTimes(1);

    act(() => {
      activeController!.enqueue(
        sse({ object: "response", id: "active-done", status: "completed" }),
      );
      activeController!.close();
    });
  });

  it("keeps the second turn visible when a stale first-turn refresh resolves late", async () => {
    const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchStream = vi.fn(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controllers.push(controller);
            },
          }),
        ),
    );
    let resolveFirstRefresh:
      | ((sessions: Array<{ id: string; messages: never[] }>) => void)
      | undefined;
    const getSessionList = vi
      .fn()
      .mockResolvedValueOnce([{ id: "session-1", messages: [] }])
      .mockImplementationOnce(
        () =>
          new Promise<Array<{ id: string; messages: never[] }>>((resolve) => {
            resolveFirstRefresh = resolve;
          }),
      )
      .mockResolvedValue([{ id: "session-1", messages: [] }]);

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList,
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "第一轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    act(() => {
      controllers[0].enqueue(
        sse({
          object: "message",
          id: "answer-1",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "第一条回复", status: "completed" }],
        }),
      );
      controllers[0].enqueue(
        sse({ object: "response", id: "response-1", status: "completed" }),
      );
      controllers[0].close();
    });
    await waitFor(() => expect(getSessionList).toHaveBeenCalledTimes(2));

    fireEvent.change(textarea, { target: { value: "第二轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(2));
    expect(screen.getByText("第二轮")).toBeInTheDocument();

    await act(async () => {
      resolveFirstRefresh?.([{ id: "session-1", messages: [] }]);
      await Promise.resolve();
    });

    expect(screen.getByText("第一条回复")).toBeInTheDocument();
    expect(screen.getByText("第二轮")).toBeInTheDocument();

    act(() => {
      controllers[1].enqueue(
        sse({
          object: "message",
          id: "answer-2",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "第二条回复", status: "completed" }],
        }),
      );
      controllers[1].enqueue(
        sse({ object: "response", id: "response-2", status: "completed" }),
      );
      controllers[1].close();
    });

    await waitFor(() => {
      expect(screen.getByText("第二轮")).toBeInTheDocument();
      expect(screen.getByText("第二条回复")).toBeInTheDocument();
    });
  });

  it("keeps two identical supplements as separate control inputs", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const receiptResponse = () =>
      new Response(
        `data: ${JSON.stringify({
          object: "turn_receipt",
          status: "steered",
          receipt: { operation: "steer", status: "steered" },
        })}\n\n`,
      );
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockImplementation(async () => receiptResponse());

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", name: "队列测试", messages: [] },
              ],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "第一句" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));

    fireEvent.change(textarea, { target: { value: "同一句补充" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    fireEvent.change(textarea, { target: { value: "同一句补充" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    expect(
      await screen.findAllByRole("article", {
        name: /引导 [12]：同一句补充/,
      }),
    ).toHaveLength(2);
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(3));
    expect(fetchStream.mock.calls[1]?.[0].input[0]).toMatchObject({
      content: [{ type: "text", text: "同一句补充" }],
    });
    expect(fetchStream.mock.calls[2]?.[0].input[0]).toMatchObject({
      content: [{ type: "text", text: "同一句补充" }],
    });
    expect(fetchStream.mock.calls[1]?.[0].biz_params.meta).toMatchObject({
      control_intent: "supplement",
    });
    expect(fetchStream.mock.calls[2]?.[0].biz_params.meta).toMatchObject({
      control_intent: "supplement",
    });
    expect(
      fetchStream.mock.calls[2]?.[0].biz_params.meta.client_turn_id,
    ).not.toBe(fetchStream.mock.calls[1]?.[0].biz_params.meta.client_turn_id);
    expect(screen.getAllByText("同一句补充")).toHaveLength(2);
    expect(screen.getAllByText("已补充当前任务")).toHaveLength(2);

    act(() => {
      activeController!.enqueue(
        sse({
          object: "response",
          id: "response-1",
          status: "completed",
        }),
      );
      activeController!.close();
    });
  });

  it("does not replay earlier queued answers into the last card from a cumulative terminal snapshot", async () => {
    let activeController: ReadableStreamDefaultController<Uint8Array> | null =
      null;
    const receiptResponse = (clientTurnId: string) =>
      new Response(
        `data: ${JSON.stringify({
          object: "turn_receipt",
          status: "steered",
          receipt: {
            operation: "steer",
            status: "steered",
            client_turn_id: clientTurnId,
          },
        })}\n\n`,
      );
    const fetchStream = vi
      .fn()
      .mockImplementationOnce(
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                activeController = controller;
                controller.enqueue(runningTurnIdentitySse());
              },
            }),
          ),
      )
      .mockResolvedValueOnce(receiptResponse("client-turn-2"))
      .mockResolvedValueOnce(receiptResponse("client-turn-3"));

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [
                { id: "session-1", name: "累计终态测试", messages: [] },
              ],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    for (const text of ["第一句", "第二句", "第三句"]) {
      fireEvent.change(textarea, { target: { value: text } });
      fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
      if (text !== "第一句") {
        fireEvent.click(
          await screen.findByRole("button", { name: `调整方向：${text}` }),
        );
      }
      await waitFor(() =>
        expect(fetchStream).toHaveBeenCalledTimes(
          text === "第一句" ? 1 : text === "第二句" ? 2 : 3,
        ),
      );
    }

    const answer = (index: number) => ({
      id: `answer-${index}`,
      object: "message",
      type: "message",
      status: "completed",
      role: "assistant",
      content: [
        {
          type: "text",
          text: `第${["一", "二", "三"][index - 1]}条回复`,
          status: "completed",
        },
      ],
      metadata: {
        client_turn_id: `client-turn-${index}`,
        run_id: "shared-native-run",
        turn_id: "shared-native-run",
      },
    });
    const reasoning = (index: number) => ({
      id: `reasoning-${index}`,
      object: "message",
      type: "reasoning",
      status: "completed",
      role: "assistant",
      content: [
        {
          type: "reasoning",
          text: `第${["一", "二", "三"][index - 1]}轮思考`,
          status: "completed",
        },
      ],
      metadata: {
        client_turn_id: `client-turn-${index}`,
        run_id: "shared-native-run",
        turn_id: "shared-native-run",
      },
    });

    act(() => {
      for (const index of [1, 2, 3]) {
        activeController!.enqueue(sse(reasoning(index)));
        activeController!.enqueue(sse(answer(index)));
      }
      // The native runtime may emit a final answer message and then repeat it
      // inside the terminal response snapshot. The renderer must keep one
      // logical card and one visible copy for the third turn.
      activeController!.enqueue(sse(answer(3)));
      activeController!.enqueue(
        sse({
          id: "response-shared-native-run",
          object: "response",
          status: "completed",
          type: "response.completed",
          // Some runtime adapters close one transport stream with a cumulative
          // snapshot. Each output still owns a distinct logical client turn.
          output: [
            reasoning(1),
            answer(1),
            reasoning(2),
            answer(2),
            reasoning(3),
            answer(3),
          ],
          metadata: {
            client_turn_id: "client-turn-3",
            run_id: "shared-native-run",
            turn_id: "shared-native-run",
          },
        }),
      );
      activeController!.close();
    });

    await waitFor(
      () =>
        expect(
          screen
            .getAllByTestId("stream-output")
            .map((output) => output.textContent || "")
            .join(""),
        ).toContain("第三条回复"),
      { timeout: 5_000 },
    );
    expect(
      screen
        .getAllByTestId("stream-output")
        .map((output) => output.textContent?.trim()),
    ).toEqual([
      "第一轮思考第一条回复",
      "第二轮思考第二条回复",
      "第三轮思考第三条回复",
    ]);
    for (const text of ["第一条回复", "第二条回复", "第三条回复"]) {
      expect(screen.getAllByText(text)).toHaveLength(1);
    }
  });

  it("does not overwrite a failed assistant turn with the next reply", async () => {
    const fetchStream = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            object: "response",
            id: "response-failed",
            status: "failed",
            output: [],
            error: { message: "第一轮失败" },
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "response-2",
            status: "completed",
            output: [
              {
                id: "answer-2",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: "第二轮成功" }],
              },
            ],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
      );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "第一轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() =>
      expect(screen.getAllByText("第一轮失败").length).toBeGreaterThan(0),
    );

    fireEvent.change(textarea, { target: { value: "第二轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await waitFor(() => {
      expect(screen.getAllByText("第一轮失败").length).toBeGreaterThan(0);
      expect(screen.getByText("第二轮成功")).toBeInTheDocument();
    });
  });

  it("filters and selects slash-command suggestions from the keyboard", async () => {
    const saveDraftText = vi.fn();
    const clearDraftText = vi.fn();
    render(
      <LantaiRuntimeWebUI
        options={{
          sender: {
            placeholder: "输入消息",
            suggestions: [
              {
                value: "clear",
                command: "/clear",
                description: "清空对话",
                label: <span>/clear 清空对话</span>,
              },
              {
                value: "compact",
                command: "/compact",
                description: "压缩对话上下文",
                label: <span>/compact 压缩对话上下文</span>,
              },
              {
                value: "skills",
                command: "/skills",
                description: "选择技能",
                label: <span>/skills 选择技能</span>,
              },
            ],
          },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              saveDraftText,
              clearDraftText,
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("输入消息");
    fireEvent.change(textarea, { target: { value: "/c" } });

    expect(
      screen.getByRole("listbox", { name: "快捷指令" }),
    ).toBeInTheDocument();
    expect(textarea.closest(".lantai-chat-anywhere-input")).toHaveAttribute(
      "data-command-menu-open",
      "true",
    );
    expect(screen.getByText("/clear 清空对话")).toBeInTheDocument();
    expect(screen.getByText("/compact 压缩对话上下文")).toBeInTheDocument();
    expect(screen.queryByText("/skills 选择技能")).not.toBeInTheDocument();

    fireEvent.keyDown(textarea, { key: "ArrowDown", code: "ArrowDown" });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    expect(textarea).toHaveValue("");
    expect(
      screen.getByRole("group", { name: "已选择快捷指令 /compact" }),
    ).toHaveTextContent("/compact压缩对话上下文");
    expect(textarea.closest(".lantai-chat-anywhere-input")).toHaveAttribute(
      "data-selected-command",
      "/compact",
    );
    expect(saveDraftText).toHaveBeenLastCalledWith("session-1", "/compact");
    expect(
      screen.queryByRole("listbox", { name: "快捷指令" }),
    ).not.toBeInTheDocument();
    expect(textarea.closest(".lantai-chat-anywhere-input")).toHaveAttribute(
      "data-command-menu-open",
      "false",
    );

    fireEvent.change(textarea, { target: { value: "保留最近的重要信息" } });
    expect(textarea).toHaveValue("保留最近的重要信息");
    expect(saveDraftText).toHaveBeenLastCalledWith(
      "session-1",
      "/compact 保留最近的重要信息",
    );
    fireEvent.click(
      screen.getByRole("button", { name: "移除快捷指令 /compact" }),
    );

    expect(
      screen.queryByRole("group", { name: "已选择快捷指令 /compact" }),
    ).not.toBeInTheDocument();
    expect(textarea).toHaveValue("保留最近的重要信息");
    expect(textarea.closest(".lantai-chat-anywhere-input")).not.toHaveAttribute(
      "data-selected-command",
    );
    expect(saveDraftText).toHaveBeenLastCalledWith(
      "session-1",
      "保留最近的重要信息",
    );
    expect(clearDraftText).not.toHaveBeenCalled();
  });

  it("restores a selected slash command as a chip without exposing its raw token", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          sender: {
            placeholder: "恢复快捷指令",
            suggestions: [
              {
                value: "plan",
                command: "/plan",
                description: "先列步骤，再开始执行",
                label: <span>/plan 先列步骤，再开始执行</span>,
              },
            ],
          },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              loadDraftText: () => "/plan 整理这份材料",
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("恢复快捷指令");
    expect(textarea).toHaveValue("整理这份材料");
    expect(
      screen.getByRole("group", { name: "已选择快捷指令 /plan" }),
    ).toHaveTextContent("/plan先列步骤，再开始执行");
  });

  it("removes an empty selected slash command with Backspace", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          sender: {
            placeholder: "撤销快捷指令",
            suggestions: [
              {
                value: "plan",
                command: "/plan",
                description: "先列步骤，再开始执行",
                label: <span>/plan 先列步骤，再开始执行</span>,
              },
            ],
          },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("撤销快捷指令");
    fireEvent.change(textarea, { target: { value: "/" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    expect(
      screen.getByRole("group", { name: "已选择快捷指令 /plan" }),
    ).toBeInTheDocument();

    fireEvent.keyDown(textarea, { key: "Backspace", code: "Backspace" });

    expect(
      screen.queryByRole("group", { name: "已选择快捷指令 /plan" }),
    ).not.toBeInTheDocument();
    expect(textarea).toHaveValue("");
  });

  it("submits the selected slash command and body as the original runtime text", async () => {
    const fetchStream = vi.fn(
      async () =>
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "slash-command-response",
            status: "completed",
            output: [],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: {
            placeholder: "发送快捷指令",
            suggestions: [
              {
                value: "plan",
                command: "/plan",
                description: "先列步骤，再开始执行",
                label: <span>/plan 先列步骤，再开始执行</span>,
              },
            ],
          },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("发送快捷指令");
    fireEvent.change(textarea, { target: { value: "/" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    fireEvent.change(textarea, { target: { value: "整理这份材料" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(fetchStream.mock.calls[0]?.[0].input[0].content).toEqual([
      expect.objectContaining({
        type: "text",
        text: "/plan 整理这份材料",
      }),
    ]);
  });

  it("exposes whether the composer contains a visible draft", async () => {
    render(
      <LantaiRuntimeWebUI
        options={{
          sender: { placeholder: "输入消息" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("输入消息");
    const composer = textarea.closest(".lantai-chat-anywhere-input");

    expect(composer).toHaveAttribute("data-has-draft", "false");

    fireEvent.change(textarea, { target: { value: "一段会撑高输入框的草稿" } });
    expect(composer).toHaveAttribute("data-has-draft", "true");

    fireEvent.change(textarea, { target: { value: "   " } });
    expect(composer).toHaveAttribute("data-has-draft", "false");
  });

  it("does not submit while an IME composition owns Enter", async () => {
    const fetchStream = vi.fn(
      async () =>
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "ime-response",
            status: "completed",
            output: [],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "IME 输入" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("IME 输入");
    fireEvent.change(textarea, { target: { value: "拼音输入中" } });
    fireEvent.compositionStart(textarea);
    fireEvent.keyDown(textarea, {
      key: "Enter",
      code: "Enter",
      isComposing: true,
    });

    expect(fetchStream).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("拼音输入中");

    fireEvent.compositionEnd(textarea);
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(fetchStream.mock.calls[0]?.[0].input[0].content).toEqual([
      expect.objectContaining({ type: "text", text: "拼音输入中" }),
    ]);
  });

  it("routes dropped attachments through the same deduplicated upload chain", async () => {
    const uploads: Array<{
      file: File;
      onSuccess: (body: { url?: string }) => void;
    }> = [];
    const customRequest = vi.fn((request) => uploads.push(request));

    render(
      <LantaiRuntimeWebUI
        options={{
          sender: {
            placeholder: "拖入文件",
            attachments: { customRequest },
          },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
            },
          },
        }}
      />,
    );

    const textarea = await screen.findByPlaceholderText("拖入文件");
    const sender = textarea.closest(".lantai-sender");
    expect(sender).not.toBeNull();
    const file = new File(["draft"], "dropped.txt", { type: "text/plain" });
    const dataTransfer = {
      files: [file],
      types: ["Files"],
      dropEffect: "none",
    };

    expect(fireEvent.dragOver(sender!, { dataTransfer })).toBe(false);
    expect(fireEvent.drop(sender!, { dataTransfer })).toBe(false);
    await waitFor(() => expect(customRequest).toHaveBeenCalledTimes(1));
    expect(uploads[0].file).toBe(file);
    expect(dataTransfer.dropEffect).toBe("copy");

    fireEvent.drop(sender!, { dataTransfer });
    expect(customRequest).toHaveBeenCalledTimes(1);
  });

  it("pastes images and common files with progress, retry, removal, and multimodal payloads", async () => {
    const objectUrl = "blob:clipboard-preview";
    const previousCreateObjectUrl = URL.createObjectURL;
    const previousRevokeObjectUrl = URL.revokeObjectURL;
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => objectUrl),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });

    const uploads: Array<{
      file: File;
      onProgress: (event: { percent?: number }) => void;
      onError: (error: Error) => void;
      onSuccess: (body: { url?: string; thumbUrl?: string }) => void;
    }> = [];
    const customRequest = vi.fn((request) => {
      uploads.push(request);
    });
    const fetchStream = vi.fn(
      async () =>
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "attachment-response",
            status: "completed",
            output: [],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const files = [
      new File(["image"], "screen.png", { type: "image/png" }),
      new File(["document"], "proposal.docx"),
      new File(["plain"], "notes.txt"),
      new File(["markdown"], "README.md"),
      new File(["pdf"], "report.pdf"),
      new File(["archive"], "sources.zip"),
    ];

    try {
      render(
        <LantaiRuntimeWebUI
          options={{
            api: { fetch: fetchStream },
            cards: { [responseCardCode]: StreamCard },
            sender: {
              placeholder: "粘贴文件",
              attachments: { customRequest },
            },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({
                  id: "session-1",
                  messages: [],
                }),
              },
            },
          }}
        />,
      );

      const textarea = await screen.findByPlaceholderText("粘贴文件");
      const pasteDispatched = fireEvent.paste(textarea, {
        clipboardData: {
          items: files.map((file) => ({
            kind: "file",
            getAsFile: () => file,
          })),
          files,
        },
      });

      await waitFor(() => expect(customRequest).toHaveBeenCalledTimes(6));
      expect(pasteDispatched).toBe(false);
      fireEvent.paste(textarea, {
        clipboardData: {
          items: files.map((file) => ({
            kind: "file",
            getAsFile: () => file,
          })),
          files,
        },
      });
      expect(customRequest).toHaveBeenCalledTimes(6);
      expect(screen.getByAltText("", { exact: true })).toHaveAttribute(
        "src",
        objectUrl,
      );
      expect(screen.getByText("proposal.docx")).toBeInTheDocument();
      expect(screen.getByText("notes.txt")).toBeInTheDocument();
      expect(screen.getByText("README.md")).toBeInTheDocument();
      expect(screen.getByText("report.pdf")).toBeInTheDocument();
      expect(screen.getByText("sources.zip")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "附件上传中" })).toBeDisabled();
      expect(uploads[1].file.type).toBe(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      );

      act(() => uploads[0].onProgress({ percent: 45 }));
      expect(screen.getByText("上传中 45%")).toBeInTheDocument();

      act(() => uploads[1].onError(new Error("network down")));
      expect(screen.getByText("上传失败：network down")).toBeInTheDocument();
      expect(
        screen.getByRole("button", {
          name: "请重试或删除上传失败的附件",
        }),
      ).toBeDisabled();

      fireEvent.click(
        screen.getByRole("button", { name: "重试上传 proposal.docx" }),
      );
      await waitFor(() => expect(customRequest).toHaveBeenCalledTimes(7));

      const latestByName = (name: string) =>
        [...uploads].reverse().find((upload) => upload.file.name === name);
      act(() => {
        files.forEach((file) => {
          latestByName(file.name)?.onSuccess({
            url: `/files/preview/${file.name}`,
          });
        });
      });

      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
      fireEvent.click(
        screen.getByRole("button", { name: "移除附件 README.md" }),
      );
      expect(screen.queryByText("README.md")).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "发送" }));
      expect(fetchStream).not.toHaveBeenCalled();
      const attachmentRoleDialog = await screen.findByRole("dialog", {
        name: "确认附件用途",
      });
      expect(attachmentRoleDialog).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "确认并发送" }));
      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
      attachmentRoleDialog.closest(".ant-modal-root")?.remove();
      const content = fetchStream.mock.calls[0]?.[0].input[0].content;
      expect(content).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            type: "image",
            image_url: "/files/preview/screen.png",
            filename: "screen.png",
          }),
          expect.objectContaining({
            type: "file",
            file_url: "/files/preview/proposal.docx",
            filename: "proposal.docx",
          }),
          expect.objectContaining({
            type: "file",
            filename: "notes.txt",
          }),
          expect.objectContaining({
            type: "file",
            filename: "report.pdf",
          }),
          expect.objectContaining({
            type: "file",
            filename: "sources.zip",
          }),
        ]),
      );
      expect(content).not.toEqual(
        expect.arrayContaining([
          expect.objectContaining({ filename: "README.md" }),
        ]),
      );
    } finally {
      Object.defineProperty(URL, "createObjectURL", {
        configurable: true,
        value: previousCreateObjectUrl,
      });
      Object.defineProperty(URL, "revokeObjectURL", {
        configurable: true,
        value: previousRevokeObjectUrl,
      });
    }
  });

  it("recovers a native desktop image when the renderer paste only exposes its filename", async () => {
    const previousElectronApi = window.electronAPI;
    const readClipboardAttachments = vi.fn(async () => ({
      attachments: [
        {
          name: "image.png",
          type: "image/png",
          size: 5,
          dataBase64: btoa("image"),
        },
      ],
    }));
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        isDesktop: true,
        platform: "darwin",
        apiBaseUrl: "",
        readClipboardAttachments,
      },
    });

    const uploads: Array<{
      file: File;
      onSuccess: (body: { url?: string }) => void;
    }> = [];
    const customRequest = vi.fn((request) => {
      uploads.push(request);
    });
    const fetchStream = vi.fn(
      async () =>
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "native-clipboard-response",
            status: "completed",
            output: [],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    try {
      render(
        <LantaiRuntimeWebUI
          options={{
            api: { fetch: fetchStream },
            cards: { [responseCardCode]: StreamCard },
            sender: {
              placeholder: "原生剪贴板",
              attachments: { customRequest },
            },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({
                  id: "session-1",
                  messages: [],
                }),
              },
            },
          }}
        />,
      );

      const textarea = await screen.findByPlaceholderText("原生剪贴板");
      fireEvent.change(textarea, { target: { value: "翻译" } });
      const pasteDispatched = fireEvent.paste(textarea, {
        clipboardData: {
          types: ["text/plain", "image/png"],
          items: [],
          files: [],
          getData: (type: string) => (type === "text/plain" ? "image.png" : ""),
        },
      });

      expect(pasteDispatched).toBe(false);
      await waitFor(() => expect(readClipboardAttachments).toHaveBeenCalled());
      await waitFor(() => expect(customRequest).toHaveBeenCalledTimes(1));
      expect(uploads[0].file.name).toBe("image.png");
      expect(uploads[0].file.type).toBe("image/png");
      expect(await uploads[0].file.text()).toBe("image");
      expect(textarea).toHaveValue("翻译");

      act(() => {
        uploads[0].onSuccess({ url: "/files/preview/image.png" });
      });
      fireEvent.click(screen.getByRole("button", { name: "发送" }));

      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
      expect(fetchStream.mock.calls[0]?.[0].input[0].content).toEqual([
        expect.objectContaining({ type: "text", text: "翻译" }),
        expect.objectContaining({
          type: "image",
          image_url: "/files/preview/image.png",
          filename: "image.png",
        }),
        expect.objectContaining({
          type: "text",
          text: expect.stringContaining("<lantai_attachment_roles>"),
        }),
      ]);
    } finally {
      Object.defineProperty(window, "electronAPI", {
        configurable: true,
        value: previousElectronApi,
      });
    }
  });

  it("turns a synchronous native clipboard upload failure into a retryable attachment", async () => {
    const previousElectronApi = window.electronAPI;
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        isDesktop: true,
        platform: "darwin",
        apiBaseUrl: "",
        readClipboardAttachments: vi.fn(async () => ({
          attachments: [
            {
              name: "clipboard.png",
              type: "image/png",
              size: 5,
              dataBase64: btoa("image"),
            },
          ],
        })),
      },
    });
    const customRequest = vi.fn(
      (request: { onError: (error: Error) => void }) => {
        request.onError(new Error("native upload unavailable"));
      },
    );

    try {
      render(
        <LantaiRuntimeWebUI
          options={{
            sender: {
              placeholder: "同步上传失败",
              attachments: { customRequest },
            },
            session: {
              api: {
                getSessionList: async () => [{ id: "session-1", messages: [] }],
                getSession: async () => ({
                  id: "session-1",
                  messages: [],
                }),
              },
            },
          }}
        />,
      );

      const textarea = await screen.findByPlaceholderText("同步上传失败");
      fireEvent.paste(textarea, {
        clipboardData: {
          types: ["text/plain", "image/png"],
          items: [],
          files: [],
          getData: (type: string) =>
            type === "text/plain" ? "clipboard.png" : "",
        },
      });

      await waitFor(() => expect(customRequest).toHaveBeenCalledTimes(1));
      expect(await screen.findByText("clipboard.png")).toBeInTheDocument();
      expect(
        screen.getByText("上传失败：native upload unavailable"),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", {
          name: "重试上传 clipboard.png",
        }),
      ).toBeEnabled();
      expect(
        screen.getByRole("button", {
          name: "请重试或删除上传失败的附件",
        }),
      ).toBeDisabled();
    } finally {
      Object.defineProperty(window, "electronAPI", {
        configurable: true,
        value: previousElectronApi,
      });
    }
  });

  it("renders a persisted user image as an image preview instead of filename text", async () => {
    const sourceMessage = {
      id: "persisted-user-image",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  { type: "text", text: "翻译" },
                  {
                    type: "image",
                    image_url: "/files/preview/image.png",
                    filename: "image.png",
                  },
                ],
              },
            ],
          },
        },
      ],
    };

    render(
      <LantaiRuntimeWebUI
        options={{
          session: {
            api: {
              getSessionList: async () => [
                {
                  id: "source-chat",
                  messages: [sourceMessage],
                },
              ],
              getSession: async () => ({
                id: "source-chat",
                messages: [sourceMessage],
              }),
            },
          },
        }}
      />,
    );

    expect(await screen.findByText("翻译")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "image.png" })).toHaveAttribute(
      "src",
      "/files/preview/image.png",
    );
    expect(screen.queryByText("image.png")).not.toBeInTheDocument();
  });

  it("renders a persisted user file as a distinct attachment card", async () => {
    const sourceMessage = {
      id: "persisted-user-file",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  { type: "text", text: "把这个转成 PPT" },
                  {
                    type: "file",
                    file_url: "/files/preview/report.pdf",
                    filename: "置身米内.pdf",
                    media_type: "application/pdf",
                    file_size: 8192,
                  },
                ],
              },
            ],
          },
        },
      ],
    };

    render(
      <LantaiRuntimeWebUI
        options={{
          session: {
            api: {
              getSessionList: async () => [
                {
                  id: "source-chat",
                  messages: [sourceMessage],
                },
              ],
              getSession: async () => ({
                id: "source-chat",
                messages: [sourceMessage],
              }),
            },
          },
        }}
      />,
    );

    expect(await screen.findByText("把这个转成 PPT")).toBeInTheDocument();
    const card = screen.getByRole("button", {
      name: "在澜台中编辑：置身米内.pdf",
    });
    expect(within(card).getByText("置身米内.pdf")).toBeInTheDocument();
    expect(within(card).getByText("参考资料 · PDF · 8 KB")).toBeInTheDocument();
    expect(card).toHaveAttribute("data-attachment-kind", "file");
    expect(card).toHaveAttribute("data-office-editable", "true");
    expect(card.closest(".lantai-bubble-content-filled")).toBeNull();
    expect(screen.getByText("把这个转成 PPT")).toHaveClass(
      "lantai-user-message-text",
    );
    expect(
      screen
        .getByText("把这个转成 PPT")
        .closest(".lantai-bubble-content-filled"),
    ).not.toBeNull();
  });

  it("opens a persisted local DOCX attachment in the Lantai editor", async () => {
    const previousElectronApi = window.electronAPI;
    const openDocxEditor = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        platform: "darwin",
        apiBaseUrl: "",
        isDesktop: true,
        openDocxEditor,
      },
    });
    const sourceMessage = {
      id: "persisted-user-docx",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  {
                    type: "file",
                    file_url:
                      "http://127.0.0.1:17654/api/files/preview/Users/example/.lantai/media/investor-notes.docx?token=local",
                    filename: "投资交流纪要.docx",
                  },
                ],
              },
            ],
          },
        },
      ],
    };

    try {
      render(
        <LantaiRuntimeWebUI
          options={{
            session: {
              api: {
                getSessionList: async () => [
                  { id: "source-chat", messages: [sourceMessage] },
                ],
                getSession: async () => ({
                  id: "source-chat",
                  messages: [sourceMessage],
                }),
              },
            },
          }}
        />,
      );

      fireEvent.click(
        await screen.findByRole("button", {
          name: "在澜台中编辑：投资交流纪要.docx",
        }),
      );
      expect(openDocxEditor).toHaveBeenCalledWith(
        "/Users/example/.lantai/media/investor-notes.docx",
        { source_chat_id: "source-chat", agent_id: "default" },
      );
    } finally {
      Object.defineProperty(window, "electronAPI", {
        configurable: true,
        value: previousElectronApi,
      });
    }
  });

  it("opens a persisted local HTML attachment in the sandboxed work window", async () => {
    const previousElectronApi = window.electronAPI;
    const openHtmlWork = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        platform: "darwin",
        apiBaseUrl: "",
        isDesktop: true,
        openHtmlWork,
      },
    });
    const sourceMessage = {
      id: "persisted-user-html",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  {
                    type: "file",
                    file_url:
                      "http://127.0.0.1:17654/api/files/preview/Users/example/.lantai/media/S1%E8%BF%B0%E8%81%8C.html?token=local",
                    filename: "S1述职.html",
                  },
                ],
              },
            ],
          },
        },
      ],
    };

    try {
      render(
        <LantaiRuntimeWebUI
          options={{
            session: {
              api: {
                getSessionList: async () => [
                  { id: "source-chat", messages: [sourceMessage] },
                ],
                getSession: async () => ({
                  id: "source-chat",
                  messages: [sourceMessage],
                }),
              },
            },
          }}
        />,
      );

      fireEvent.click(
        await screen.findByRole("button", {
          name: "打开附件：S1述职.html",
        }),
      );
      expect(openHtmlWork).toHaveBeenCalledWith(
        "/Users/example/.lantai/media/S1述职.html",
        "S1述职.html",
      );
    } finally {
      Object.defineProperty(window, "electronAPI", {
        configurable: true,
        value: previousElectronApi,
      });
    }
  });

  it("opens any other persisted local attachment with the system app", async () => {
    const previousElectronApi = window.electronAPI;
    const openPath = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        platform: "darwin",
        apiBaseUrl: "",
        isDesktop: true,
        openPath,
      },
    });
    const sourceMessage = {
      id: "persisted-user-archive",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  {
                    type: "file",
                    file_url:
                      "http://127.0.0.1:17654/api/files/preview/Users/example/.lantai/media/source-bundle.zip?token=local",
                    filename: "source-bundle.zip",
                  },
                ],
              },
            ],
          },
        },
      ],
    };

    try {
      render(
        <LantaiRuntimeWebUI
          options={{
            session: {
              api: {
                getSessionList: async () => [
                  { id: "source-chat", messages: [sourceMessage] },
                ],
                getSession: async () => ({
                  id: "source-chat",
                  messages: [sourceMessage],
                }),
              },
            },
          }}
        />,
      );

      fireEvent.click(
        await screen.findByRole("button", {
          name: "打开附件：source-bundle.zip",
        }),
      );
      expect(openPath).toHaveBeenCalledWith(
        "/Users/example/.lantai/media/source-bundle.zip",
      );
    } finally {
      Object.defineProperty(window, "electronAPI", {
        configurable: true,
        value: previousElectronApi,
      });
    }
  });

  it("opens a non-local persisted attachment in a new browser tab", async () => {
    const openWindow = vi.spyOn(window, "open").mockImplementation(() => null);
    const sourceMessage = {
      id: "persisted-user-remote-file",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  {
                    type: "file",
                    file_url: "https://files.example/research-notes.txt",
                    filename: "research-notes.txt",
                  },
                ],
              },
            ],
          },
        },
      ],
    };

    try {
      render(
        <LantaiRuntimeWebUI
          options={{
            session: {
              api: {
                getSessionList: async () => [
                  { id: "source-chat", messages: [sourceMessage] },
                ],
                getSession: async () => ({
                  id: "source-chat",
                  messages: [sourceMessage],
                }),
              },
            },
          }}
        />,
      );

      fireEvent.click(
        await screen.findByRole("button", {
          name: "打开附件：research-notes.txt",
        }),
      );
      expect(openWindow).toHaveBeenCalledWith(
        "https://files.example/research-notes.txt",
        "_blank",
        "noopener,noreferrer",
      );
    } finally {
      openWindow.mockRestore();
    }
  });

  it("copies, edits, and submits a persisted user message through a branch", async () => {
    const sourceMessage = {
      id: "persisted-user-2",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [
                  { type: "text", text: "原始问题" },
                  {
                    type: "file",
                    file_url: "/tmp/lantai-media/historical.pdf",
                    file_source_url: "/tmp/lantai-media/historical.pdf",
                    filename: "historical.pdf",
                    stored_name: "historical.pdf",
                    file_size: 42,
                    media_type: "application/pdf",
                    attachment_schema_version: 1,
                    attachment_digest: `sha256:${"a".repeat(64)}`,
                    attachment_version: 1,
                    attachment_source_id: `attachment-source:sha256:${"b".repeat(64)}`,
                    attachment_source_chat_id: "source-chat",
                    attachment_grant_state: "bound",
                    attachment_upload_receipt: null,
                  },
                ],
              },
            ],
          },
        },
      ],
    };
    const clipboardWrite = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    const branchSession = vi.fn(async () => [
      {
        id: "branch-chat",
        sessionId: "console:branch:branch-chat",
        userId: "default",
        channel: "console",
        messages: [],
      },
      {
        id: "source-chat",
        sessionId: "console:source",
        userId: "default",
        channel: "console",
        messages: [sourceMessage],
      },
    ]);
    const fetchStream = vi.fn(
      async () =>
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "edited-response",
            status: "completed",
            output: [
              {
                id: "edited-answer",
                type: "message",
                status: "completed",
                role: "assistant",
                content: [{ type: "text", text: "编辑后的回答" }],
              },
            ],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [
                {
                  id: "source-chat",
                  sessionId: "console:source",
                  userId: "default",
                  channel: "console",
                  messages: [sourceMessage],
                },
              ],
              getSession: async () => ({
                id: "source-chat",
                sessionId: "console:source",
                userId: "default",
                channel: "console",
                messages: [sourceMessage],
              }),
              updateSession: async (session) => [session as any],
              branchSession,
            },
          },
        }}
      />,
    );

    await screen.findByText("原始问题");
    fireEvent.click(screen.getByRole("button", { name: "复制用户消息" }));
    await waitFor(() =>
      expect(clipboardWrite).toHaveBeenCalledWith("原始问题"),
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑用户消息" }));
    const textarea = screen.getByPlaceholderText("说点什么");
    expect(textarea).toHaveValue("原始问题");
    expect(
      screen.getByText("正在编辑历史消息，发送后将创建分支"),
    ).toBeInTheDocument();
    fireEvent.change(textarea, { target: { value: "修改后的问题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() =>
      expect(branchSession).toHaveBeenCalledWith(
        "source-chat",
        "persisted-user-2",
      ),
    );
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    const submittedRequest = fetchStream.mock.calls[0]?.[0].input[0];
    expect(submittedRequest.session).toMatchObject({
      session_id: "console:branch:branch-chat",
    });
    expect(submittedRequest.content[0]).toMatchObject({
      type: "text",
      text: "修改后的问题",
    });
    expect(submittedRequest.content[1]).toMatchObject({
      type: "file",
      file_url: "/tmp/lantai-media/historical.pdf",
      file_source_url: "/tmp/lantai-media/historical.pdf",
      filename: "historical.pdf",
      stored_name: "historical.pdf",
      file_size: 42,
      media_type: "application/pdf",
      attachment_digest: `sha256:${"a".repeat(64)}`,
      attachment_version: 1,
      attachment_source_id: `attachment-source:sha256:${"b".repeat(64)}`,
      attachment_source_chat_id: "source-chat",
    });
    expect(
      submittedRequest.content[1].attachment_upload_receipt,
    ).toBeUndefined();
    expect(await screen.findByText("编辑后的回答")).toBeInTheDocument();
  });

  it("creates a fresh branch from a persisted user message after confirmation", async () => {
    const sourceMessage = {
      id: "persisted-user-resend",
      role: "user" as const,
      cards: [
        {
          code: "LantaiRuntimeRequestCard",
          data: {
            input: [
              {
                role: "user",
                type: "message",
                content: [{ type: "text", text: "再试一次" }],
              },
            ],
          },
        },
      ],
    };
    let resolveBranchSession:
      | ((sessions: Array<Record<string, unknown>>) => void)
      | undefined;
    const branchSession = vi.fn(
      () =>
        new Promise<Array<Record<string, unknown>>>((resolve) => {
          resolveBranchSession = resolve;
        }),
    );
    const fetchStream = vi.fn(
      async () =>
        new Response(
          `data: ${JSON.stringify({
            object: "response",
            id: "resend-response",
            status: "completed",
            output: [],
          })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );

    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { [responseCardCode]: StreamCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList: async () => [
                {
                  id: "source-resend",
                  sessionId: "console:source-resend",
                  messages: [sourceMessage],
                },
              ],
              getSession: async () => ({
                id: "source-resend",
                sessionId: "console:source-resend",
                messages: [sourceMessage],
              }),
              branchSession,
            },
          },
        }}
      />,
    );

    await screen.findByText("再试一次");
    const createBranchButton = screen.getByRole("button", {
      name: "重试用户消息",
    });
    expect(
      createBranchButton.querySelector(".lucide-rotate-ccw"),
    ).toBeInTheDocument();

    fireEvent.click(createBranchButton);
    expect(
      await screen.findByRole("dialog", { name: "从此处创建分支？" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "将保留这条消息之前的内容，并在新会话中重新生成后续回复。原会话不会被修改。",
      ),
    ).toBeInTheDocument();
    expect(branchSession).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /取\s*消/ }));
    expect(branchSession).not.toHaveBeenCalled();

    fireEvent.click(createBranchButton);
    fireEvent.click(screen.getByRole("button", { name: "创建分支" }));
    expect(
      await screen.findByRole("status", {
        name: "正在创建分支并重新生成",
      }),
    ).toBeInTheDocument();

    resolveBranchSession?.([
      {
        id: "branch-resend",
        sessionId: "console:branch:resend",
        userId: "default",
        channel: "console",
        messages: [],
      },
    ]);

    await waitFor(() =>
      expect(branchSession).toHaveBeenCalledWith(
        "source-resend",
        "persisted-user-resend",
      ),
    );
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(fetchStream.mock.calls[0]?.[0].input[0]).toMatchObject({
      content: [{ type: "text", text: "再试一次" }],
      session: { session_id: "console:branch:resend" },
    });
  });
});
