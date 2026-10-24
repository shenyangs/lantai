import { describe, expect, it } from "vitest";

import {
  chooseActiveSessionMessages,
  reconcileActiveSessionProjection,
  type ILantaiChatMessage,
} from "./lantaiChat";
import {
  createTranscriptReconciler,
  durableTranscriptHydrationIsRequired,
} from "./lantaiTranscriptReconciler";

const RESPONSE_CARD_CODE = "LantaiRuntimeResponseCard";

function user(id: string, text: string): ILantaiChatMessage {
  return {
    id,
    role: "user",
    metadata: { client_turn_id: id },
    cards: [
      {
        code: "RequestCard",
        data: {
          client_turn_id: id,
          input: [{ content: [{ type: "text", text }] }],
        },
      },
    ],
  };
}

function assistant(
  id: string,
  clientTurnId: string,
  text: string,
  status: "in_progress" | "completed" = "completed",
): ILantaiChatMessage {
  return {
    id,
    role: "assistant",
    msgStatus: status === "completed" ? "finished" : "loading",
    cards: [
      {
        code: RESPONSE_CARD_CODE,
        data: {
          id: `${id}-response`,
          object: "response",
          status,
          sequence_number: status === "completed" ? 2 : 1,
          metadata: {
            client_turn_id: clientTurnId,
            native_runtime_turn_id: clientTurnId,
          },
          output: text
            ? [
                {
                  id: `${id}-output`,
                  type: "message",
                  role: "assistant",
                  status,
                  content: [{ type: "text", text }],
                  metadata: {
                    client_turn_id: clientTurnId,
                    source_message_id: `${clientTurnId}-answer`,
                  },
                },
              ]
            : [],
        },
      },
    ],
  };
}

function queuedUser(
  id: string,
  text: string,
  status: "sending" | "steered" | "queued" | "failed" = "steered",
): ILantaiChatMessage {
  const message = user(id, text);
  message.cards![0].data.runtime_queue_status = status;
  return message;
}

function persistedUserWithoutClientTurn(
  id: string,
  text: string,
): ILantaiChatMessage {
  return {
    id,
    role: "user",
    cards: [
      {
        code: "RequestCard",
        data: {
          input: [
            {
              content: [{ type: "input_text", text, status: "completed" }],
            },
          ],
        },
      },
    ],
  };
}

describe("Transcript Reconciler contract", () => {
  const repeatedContents = [
    ["text", { type: "text", text: "repeat command submission" }],
    ["image", { type: "image", image_url: "file:///synthetic/shared.png" }],
    ["file", { type: "file", file_url: "file:///synthetic/shared.docx" }],
  ] as const;
  const commandUser = (
    id: string,
    commandId: string,
    content: unknown,
    location = "metadata",
  ): ILantaiChatMessage => {
    const message = persistedUserWithoutClientTurn(id, "");
    const data = message.cards![0].data;
    data.input[0].content = [content];
    if (location === "metadata") message.metadata = { command_id: commandId };
    if (location === "request_data")
      data.runtime_control_command_id = commandId;
    if (location === "input_metadata") {
      data.input[0].metadata = { command_id: commandId };
    }
    return message;
  };

  for (const [kind, content] of repeatedContents) {
    it.each(["metadata", "request_data", "input_metadata"])(
      `keeps distinct command-only ${kind} submissions without row IDs (%s)`,
      (location) => {
        const first = commandUser("", "command-A", content, location);
        const second = commandUser("", "command-B", content, location);
        expect(chooseActiveSessionMessages([first], [second])).toEqual([
          first,
          second,
        ]);
      },
    );
    it.each(["metadata", "request_data", "input_metadata"])(
      `reconciles command-only ${kind} replay without row IDs (%s)`,
      (location) => {
        const first = commandUser("", "command-A", content, location);
        const replay = commandUser("", "command-A", content, location);
        expect(chooseActiveSessionMessages([first], [replay])).toEqual([
          replay,
        ]);
      },
    );
    it.each([false, true])(
      `distinct commands outrank a reused ${kind} row ID (assistant=%s)`,
      (withAssistant) => {
        const first = commandUser("same-row", "command-A", content);
        const second = commandUser("same-row", "command-B", content);
        const current = withAssistant
          ? [first, assistant("reply-A", "command-A", "first answer")]
          : [first];
        const incoming = withAssistant
          ? [second, assistant("reply-B", "command-B", "second answer")]
          : [second];
        expect(
          chooseActiveSessionMessages(current, incoming).filter(
            (message) => message.role === "user",
          ),
        ).toEqual([first, second]);
      },
    );
    it.each([false, true])(
      `one command reconciles changed ${kind} row IDs (assistant=%s)`,
      (withAssistant) => {
        const first = commandUser("optimistic-row", "command-A", content);
        const replay = commandUser("durable-row", "command-A", content);
        const current = withAssistant
          ? [first, assistant("reply-A", "command-A", "answer")]
          : [first];
        const incoming = withAssistant
          ? [replay, assistant("reply-A", "command-A", "answer")]
          : [replay];
        expect(
          chooseActiveSessionMessages(current, incoming).filter(
            (message) => message.role === "user",
          ),
        ).toEqual([replay]);
      },
    );
  }

  it.each([false, true])(
    "preserves conflicting explicit turns with a reused row id (assistant=%s)",
    (withAssistant) => {
      const first = user("reused-row", "same request");
      first.metadata = { client_turn_id: "turn-A" };
      first.cards![0].data.client_turn_id = "turn-A";
      const second = user("reused-row", "same request");
      second.metadata = { client_turn_id: "turn-B" };
      second.cards![0].data.client_turn_id = "turn-B";
      const current = withAssistant
        ? [first, assistant("reply-A", "turn-A", "first answer")]
        : [first];
      const incoming = withAssistant
        ? [second, assistant("reply-B", "turn-B", "second answer")]
        : [second];

      const projection = chooseActiveSessionMessages(current, incoming);

      expect(
        projection
          .filter((message) => message.role === "user")
          .map((message) => message.metadata?.client_turn_id),
      ).toEqual(["turn-A", "turn-B"]);
    },
  );

  it("does not consume a user row with an assistant using the same id", () => {
    const first = user("shared-role-id", "request");
    const reply = assistant("shared-role-id", "shared-role-id", "answer");

    expect(
      chooseActiveSessionMessages([first], [reply]).map(
        (message) => message.role,
      ),
    ).toEqual(["user", "assistant"]);
  });

  it("keeps completed narration at its original timeline position when terminal hydration only adds a failure", () => {
    const clientTurnId = "turn-failed-after-progress";
    const liveTimeline = assistant(
      "assistant-live-timeline",
      clientTurnId,
      "",
      "in_progress",
    );
    liveTimeline.cards![0].data.output = [
      {
        id: "progress-before-tools",
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "text", text: "先声明交付物，然后读取文档。" }],
        metadata: {
          client_turn_id: clientTurnId,
          source_message_id: "progress-before-tools",
        },
      },
      {
        id: "read-source-started",
        type: "plugin_call",
        role: "assistant",
        status: "completed",
        content: [{ type: "text", text: "读取参考材料" }],
        metadata: { client_turn_id: clientTurnId },
      },
      {
        id: "read-source-completed",
        type: "plugin_call_output",
        role: "tool",
        status: "completed",
        content: [{ type: "text", text: "读取完成" }],
        metadata: { client_turn_id: clientTurnId },
      },
      {
        id: "progress-after-tools",
        type: "message",
        role: "assistant",
        status: "completed",
        content: [{ type: "text", text: "文档内容已核对，接着开始搭建。" }],
        metadata: {
          client_turn_id: clientTurnId,
          source_message_id: "progress-after-tools",
        },
      },
    ];

    const durableFailure = assistant(
      "assistant-durable-failure",
      clientTurnId,
      "",
      "completed",
    );
    durableFailure.cards![0].data.status = "failed";
    durableFailure.cards![0].data.metadata.canonical_history_timeline = true;
    durableFailure.cards![0].data.output = [
      {
        id: "terminal-failure",
        type: "error",
        role: "assistant",
        status: "failed",
        content: [{ type: "text", text: "模型输出在正文完成前被截断。" }],
        metadata: { client_turn_id: clientTurnId },
      },
    ];

    let projection = chooseActiveSessionMessages(
      [user(clientTurnId, "帮我做成 PPT"), liveTimeline],
      [user(clientTurnId, "帮我做成 PPT"), durableFailure],
    );
    for (let reconnect = 0; reconnect < 20; reconnect += 1) {
      projection = chooseActiveSessionMessages(projection, [
        user(clientTurnId, "帮我做成 PPT"),
        durableFailure,
      ]);
    }
    const output = projection[1]?.cards?.[0]?.data.output || [];

    expect(output.map((item: { id: string }) => item.id)).toEqual([
      "progress-before-tools",
      "read-source-started",
      "read-source-completed",
      "progress-after-tools",
      "terminal-failure",
    ]);
    expect(
      output
        .filter((item: { type: string }) => item.type === "message")
        .map(
          (item: { content: Array<{ text?: string }> }) =>
            item.content[0]?.text,
        ),
    ).toEqual([
      "先声明交付物，然后读取文档。",
      "文档内容已核对，接着开始搭建。",
    ]);
  });

  it("hydrates terminal turns until history contains a canonical terminal reply", () => {
    const canonical = assistant(
      "canonical-answer",
      "canonical-turn",
      "已写入历史",
    );
    canonical.cards![0].data.metadata.canonical_history_timeline = true;

    expect(
      durableTranscriptHydrationIsRequired({
        messages: [canonical],
        primaryStreamActive: false,
        primaryWasActiveAtTerminal: true,
      }),
    ).toBe(true);
    canonical.cards![0].data.metadata.canonical_terminal_reply = true;
    expect(
      durableTranscriptHydrationIsRequired({
        messages: [canonical],
        primaryStreamActive: false,
        primaryWasActiveAtTerminal: true,
      }),
    ).toBe(false);
    expect(
      durableTranscriptHydrationIsRequired({
        messages: [],
        primaryStreamActive: false,
        primaryWasActiveAtTerminal: true,
      }),
    ).toBe(true);
    expect(
      durableTranscriptHydrationIsRequired({
        messages: [],
        primaryStreamActive: true,
        primaryWasActiveAtTerminal: false,
      }),
    ).toBe(false);
    expect(
      durableTranscriptHydrationIsRequired({
        messages: [canonical, user("new-turn", "下一轮")],
        primaryStreamActive: false,
        primaryWasActiveAtTerminal: true,
      }),
    ).toBe(true);
  });

  it("is idempotent across one hundred reconnect hydrations", () => {
    const incoming = [
      user("turn-1", "第一轮"),
      assistant("answer-1", "turn-1", "第一轮完成"),
      user("turn-2", "第二轮"),
      assistant("answer-2", "turn-2", "第二轮完成"),
    ];
    let projection: ILantaiChatMessage[] = [
      user("turn-1", "第一轮"),
      assistant("answer-1-live", "turn-1", "第一轮完成"),
      user("turn-2", "第二轮"),
      assistant("turn-2-waiting", "turn-2", "", "in_progress"),
    ];

    for (let iteration = 0; iteration < 100; iteration += 1) {
      projection = chooseActiveSessionMessages(projection, incoming);
      expect(projection).toHaveLength(4);
      expect(projection.map((message) => message.role)).toEqual([
        "user",
        "assistant",
        "user",
        "assistant",
      ]);
    }

    expect(projection[3]?.cards?.[0]?.data).toMatchObject({
      status: "completed",
      metadata: { client_turn_id: "turn-2" },
    });
  });

  it("never resurrects a terminal turn from a stale running projection", () => {
    const terminal = assistant(
      "terminal-answer",
      "turn-terminal",
      "已经完成",
      "completed",
    );
    const staleRunning = assistant(
      "stale-running-answer",
      "turn-terminal",
      "已经",
      "in_progress",
    );

    const projection = chooseActiveSessionMessages(
      [user("turn-terminal", "继续"), terminal],
      [user("turn-terminal", "继续"), staleRunning],
    );

    expect(projection[1]?.cards?.[0]?.data).toMatchObject({
      status: "completed",
      output: [
        expect.objectContaining({
          status: "completed",
          content: [expect.objectContaining({ text: "已经完成" })],
        }),
      ],
    });
  });

  it("keeps queued turn receipts in FIFO order with one reply per client turn", () => {
    const durable = [1, 2, 3].flatMap((turn) => [
      user(`turn-${turn}`, `第 ${turn} 轮`),
      assistant(`answer-${turn}`, `turn-${turn}`, `第 ${turn} 轮完成`),
    ]);
    const current = [
      ...durable.slice(0, 4),
      user("turn-3", "第 3 轮"),
      assistant("turn-3-waiting", "turn-3", "", "in_progress"),
    ];

    const projection = chooseActiveSessionMessages(current, durable);

    expect(projection.map((message) => message.id)).toEqual([
      "turn-1",
      "answer-1",
      "turn-2",
      "answer-2",
      "turn-3",
      "turn-3-waiting",
    ]);
    expect(projection[5]?.cards?.[0]?.data).toMatchObject({
      status: "completed",
      metadata: { client_turn_id: "turn-3" },
    });
  });

  it("replaces steered delivery shells with durable rows and restores canonical FIFO order", () => {
    const current = [
      user("turn-1", "第一轮"),
      assistant("live-answer-1", "turn-1", "第一轮完成"),
      queuedUser("local-turn-2", "第二轮"),
      queuedUser("local-turn-3", "第三轮"),
      assistant("live-answer-3", "local-turn-3", "第三轮完成"),
      assistant("live-answer-2", "local-turn-2", "第二轮完成"),
    ];
    const durable = [
      user("turn-1", "第一轮"),
      assistant("answer-1", "turn-1", "第一轮完成"),
      user("persisted-turn-2", "第二轮"),
      assistant("answer-2", "local-turn-2", "第二轮完成"),
      user("persisted-turn-3", "第三轮"),
      assistant("answer-3", "local-turn-3", "第三轮完成"),
    ];

    const projection = chooseActiveSessionMessages(current, durable);

    expect(projection.map((message) => message.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(
      projection
        .filter((message) => message.role === "user")
        .map((message) => message.id),
    ).toEqual(["turn-1", "persisted-turn-2", "persisted-turn-3"]);
    expect(
      projection
        .filter((message) => message.role === "assistant")
        .map(
          (message) =>
            message.cards?.[0]?.data?.output?.[0]?.content?.[0]?.text,
        ),
    ).toEqual(["第一轮完成", "第二轮完成", "第三轮完成"]);
    expect(
      projection
        .filter((message) => message.role === "user")
        .map((message) => message.cards?.[0]?.data?.runtime_queue_status),
    ).toEqual([undefined, undefined, undefined]);
  });

  it("reconciles repeated steered prompts one-to-one without collapsing turns", () => {
    const current = [
      queuedUser("local-turn-1", "继续"),
      queuedUser("local-turn-2", "继续"),
    ];
    const durable = [
      user("persisted-turn-1", "继续"),
      assistant("answer-1", "local-turn-1", "第一轮完成"),
      user("persisted-turn-2", "继续"),
      assistant("answer-2", "local-turn-2", "第二轮完成"),
    ];

    const projection = chooseActiveSessionMessages(current, durable);

    expect(
      projection
        .filter((message) => message.role === "user")
        .map((message) => message.id),
    ).toEqual(["persisted-turn-1", "persisted-turn-2"]);
    expect(
      projection
        .filter((message) => message.role === "assistant")
        .map((message) => message.id),
    ).toEqual(["answer-1", "answer-2"]);
  });

  it("reads old persisted turns without client identity without duplicating the user", () => {
    const optimistic = user("new-client-turn", "同一条旧会话消息");
    const readOld = {
      id: "legacy-persisted-user",
      role: "user",
      cards: [
        {
          code: "RequestCard",
          data: {
            input: [
              {
                content: [
                  {
                    type: "input_text",
                    text: "同一条旧会话消息",
                    status: "completed",
                  },
                ],
              },
            ],
          },
        },
      ],
    } satisfies ILantaiChatMessage;
    const durableReply = assistant(
      "legacy-durable-answer",
      "new-client-turn",
      "旧会话仍可恢复",
    );

    const projection = chooseActiveSessionMessages(
      [optimistic],
      [readOld, durableReply],
    );

    expect(
      projection.filter((message) => message.role === "user"),
    ).toHaveLength(1);
    expect(projection.at(-1)?.cards?.[0]?.data).toMatchObject({
      status: "completed",
    });
  });

  it("does not duplicate an optimistic submit while durable history only has its user row", () => {
    const optimistic = user("live-client-turn", "把这个做成 PPT");
    const liveReply = assistant(
      "live-stream-reply",
      "live-client-turn",
      "正在读取材料",
      "in_progress",
    );
    const persisted = persistedUserWithoutClientTurn(
      "persisted-database-row",
      "把这个做成 PPT",
    );

    const projection = chooseActiveSessionMessages(
      [optimistic, liveReply],
      [persisted],
    );

    expect(projection.map((message) => message.role)).toEqual([
      "user",
      "assistant",
    ]);
    expect(
      projection.filter((message) => message.role === "user"),
    ).toHaveLength(1);
    expect(projection[0]?.id).toBe("live-client-turn");
    expect(projection[1]?.cards?.[0]?.data).toMatchObject({
      status: "in_progress",
      metadata: { client_turn_id: "live-client-turn" },
    });
  });

  it("does not append a legacy original row from partial history after queued inputs", () => {
    const current = [
      user("original-turn", "开始处理"),
      assistant("original-answer", "original-turn", "已开始", "in_progress"),
      queuedUser("supplement-turn", "补充要求"),
      queuedUser("redirect-turn", "调整方向"),
      assistant("redirect-answer", "redirect-turn", "已调整"),
    ];
    const partialHistory = [
      persistedUserWithoutClientTurn("persisted-original-row", "开始处理"),
    ];

    const projection = chooseActiveSessionMessages(current, partialHistory);

    expect(projection.filter((message) => message.role === "user")).toHaveLength(3);
    expect(projection.map((message) => message.id)).toEqual(
      current.map((message) => message.id),
    );
  });

  it("preserves a distinct explicit submit in partial history even when its text repeats", () => {
    const current = [
      user("original-turn", "继续"),
      assistant("original-answer", "original-turn", "已开始", "in_progress"),
      queuedUser("supplement-turn", "补充要求"),
    ];
    const repeat = user("intentional-repeat-turn", "继续");

    const projection = chooseActiveSessionMessages(current, [repeat]);

    expect(projection.filter((message) => message.role === "user").map((message) => message.id)).toEqual([
      "original-turn", "supplement-turn", "intentional-repeat-turn",
    ]);
  });

  it("reconciles multiple persisted user rows whose database ids look like client ids", () => {
    const current = [
      user("user-optimistic-one", "第一轮请求"),
      assistant("live-answer-one", "user-optimistic-one", "第一轮完成"),
      user("user-optimistic-two", "第二轮请求"),
      assistant("live-answer-two", "user-optimistic-two", "第二轮完成"),
    ];
    const durable = [
      persistedUserWithoutClientTurn("user-msg_database_one", "第一轮请求"),
      assistant("durable-answer-one", "user-optimistic-one", "第一轮完成"),
      persistedUserWithoutClientTurn("user-msg_database_two", "第二轮请求"),
      assistant("durable-answer-two", "user-optimistic-two", "第二轮完成"),
    ];

    const projection = chooseActiveSessionMessages(current, durable);

    expect(projection.map((message) => message.role)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    expect(
      projection
        .filter((message) => message.role === "user")
        .map((message) => message.id),
    ).toEqual(["user-msg_database_one", "user-msg_database_two"]);
  });

  it("collapses adjacent mirrors of one user submit without hiding a later intentional repeat", () => {
    const first = user("optimistic-turn", "帮我整理这个问题");
    first.createdAt = "2026-09-04T07:19:00.000Z";
    const mirrored = user("persisted-row", "帮我整理这个问题");
    mirrored.metadata = { client_turn_id: "optimistic-turn" };
    mirrored.cards![0].data.client_turn_id = "optimistic-turn";
    mirrored.createdAt = "2026-09-04T07:19:00.420Z";
    const intentionalRepeat = user("later-turn", "帮我整理这个问题");
    intentionalRepeat.createdAt = "2026-09-04T07:23:00.000Z";

    const projection = chooseActiveSessionMessages([
      first,
      mirrored,
      assistant("answer-1", "optimistic-turn", "第一轮完成"),
      intentionalRepeat,
      assistant("answer-2", "later-turn", "重复请求也已完成"),
    ]);

    expect(
      projection
        .filter((message) => message.role === "user")
        .map((message) => message.id),
    ).toEqual(["optimistic-turn", "later-turn"]);
  });

  it("keeps two rapid identical submits when their client turn ids differ", () => {
    const first = user("rapid-turn-1", "继续");
    first.createdAt = "2026-09-04T07:19:00.000Z";
    const second = user("rapid-turn-2", "继续");
    second.createdAt = "2026-09-04T07:19:00.420Z";

    const projection = chooseActiveSessionMessages([first, second]);

    expect(
      projection
        .filter((message) => message.role === "user")
        .map((message) => message.id),
    ).toEqual(["rapid-turn-1", "rapid-turn-2"]);
  });

  it("keeps rapid identical legacy rows when no stable turn identity links them", () => {
    const first = persistedUserWithoutClientTurn("legacy-row-one", "继续");
    first.createdAt = "2026-09-04T07:19:00.000Z";
    const second = persistedUserWithoutClientTurn("legacy-row-two", "继续");
    second.createdAt = "2026-09-04T07:19:00.420Z";

    const projection = chooseActiveSessionMessages([first, second]);

    expect(
      projection
        .filter((message) => message.role === "user")
        .map((message) => message.id),
    ).toEqual(["legacy-row-one", "legacy-row-two"]);
  });

  it("does not let an older terminal snapshot replace a newer durable turn", () => {
    const reconciler = createTranscriptReconciler({
      responseData: (message) =>
        message.role === "assistant"
          ? (message.cards?.[0]?.data as never)
          : null,
      responseTurnKey: (response) =>
        String(response.metadata?.client_turn_id || ""),
      hasEquivalentStreamedAnswer: () => false,
      shouldMergeAssistantResponses: () => false,
      mergeAssistantResponses: (current) => current,
      normalizeAdjacentAssistantResponses: (messages) => messages,
      isActiveStatus: (status) => status === "in_progress",
      isTerminalStatus: (status) =>
        status === "completed" || status === "failed",
    });
    const current = [
      user("running-turn", "生成 PPT"),
      user("newer-running-turn", "继续完善这份 PPT"),
    ];
    const staleTerminal = [
      user("running-turn", "生成 PPT"),
      assistant("stale-terminal", "running-turn", "旧完成快照"),
    ];

    const result = reconciler.reconcileRouteHydration({
      activeSessionMessages: current,
      localSessionMessages: current,
      incomingMessages: staleTerminal,
      localMatchesCurrentSession: true,
      durableSessionGenerating: true,
      primaryStreamActive: false,
      runtimeWasActive: true,
    });

    expect(result.terminalAuthority).toBe(false);
  });

  it("lets durable terminal history replace an orphaned waiting shell", () => {
    const reconciler = createTranscriptReconciler({
      responseData: (message) =>
        message.role === "assistant"
          ? (message.cards?.[0]?.data as never)
          : null,
      responseTurnKey: (response) =>
        String(response.metadata?.client_turn_id || ""),
      hasEquivalentStreamedAnswer: () => false,
      shouldMergeAssistantResponses: () => false,
      mergeAssistantResponses: (current) => current,
      normalizeAdjacentAssistantResponses: (messages) => messages,
      isActiveStatus: (status) => status === "in_progress",
      isTerminalStatus: (status) =>
        status === "completed" || status === "failed",
    });
    const submitted = user("finished-turn", "生成 PPT");
    const terminal = assistant(
      "finished-answer",
      "finished-turn",
      "PPT 已生成",
    );
    const orphanedWaiting = assistant(
      "orphaned-waiting",
      "finished-turn",
      "",
      "in_progress",
    );
    orphanedWaiting.cards![0].data.metadata = {
      ...orphanedWaiting.cards![0].data.metadata,
      optimistic_pending: true,
    };

    const result = reconciler.reconcileRouteHydration({
      activeSessionMessages: [submitted, terminal, orphanedWaiting],
      localSessionMessages: [submitted, terminal, orphanedWaiting],
      incomingMessages: [submitted, terminal],
      localMatchesCurrentSession: true,
      durableSessionGenerating: true,
      primaryStreamActive: false,
      runtimeWasActive: true,
    });

    expect(result.terminalAuthority).toBe(true);
    expect(result.preservingExistingMessages).toBe(false);
    expect(result.messages.map((message) => message.id)).toEqual([
      "finished-turn",
      "finished-answer",
    ]);
  });

  it("never carries transcript cards across route ownership changes", () => {
    const foreign = [user("foreign-turn", "另一个会话")];
    const owned = [
      user("owned-turn", "当前会话"),
      assistant("owned-answer", "owned-turn", "当前回答"),
    ];

    for (let iteration = 0; iteration < 100; iteration += 1) {
      expect(
        reconcileActiveSessionProjection(foreign, owned, false).map(
          (message) => message.id,
        ),
      ).toEqual(["owned-turn", "owned-answer"]);
    }
  });
});
