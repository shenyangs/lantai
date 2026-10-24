import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  buildNativeRuntimeStreamMessages,
  buildRuntimeEventSessionPatch,
  ensureNativeRuntimeTurnPlaceholder,
  isNativeMessageStreamEvent,
  latestPreviewSeq,
  mergePreviewEvents,
  runtimeEventToPreviewEvent,
  runtimeResponseHasTerminalEvent,
} from "./runtimeEventStream";
import type { RuntimeEvent, RuntimeEventsResponse } from "../../api/types/chat";
import { LANTAI_RESPONSE_CARD_CODE } from "./chatCardCodes";

function event(partial: Partial<RuntimeEvent>): RuntimeEvent {
  return {
    schema_version: partial.schema_version ?? 1,
    id: partial.id || "evt",
    seq: partial.seq ?? 1,
    type: partial.type || "item.completed",
    status: partial.status || "completed",
    source: partial.source || "system",
    payload: partial.payload || {},
    usage: partial.usage ?? null,
    created_at: partial.created_at || "2026-05-29T07:59:00Z",
    turn_id: Object.prototype.hasOwnProperty.call(partial, "turn_id")
      ? (partial.turn_id ?? null)
      : "run-1",
    session_id: partial.session_id ?? "console:chat-1",
    item_id: partial.item_id ?? null,
    presentation: partial.presentation ?? null,
  };
}

function response(events: RuntimeEvent[]): RuntimeEventsResponse {
  return {
    chat_id: "chat-1",
    workspace_dir: "/tmp/topic-workspace",
    after_seq: 0,
    latest_seq: events.at(-1)?.seq ?? 0,
    events,
  };
}

function sharedRuntimeEventFixture(): RuntimeEvent[] {
  const raw = readFileSync(
    resolve(process.cwd(), "../tests/fixtures/runtime_event_v1.json"),
    "utf8",
  );
  const fixture = JSON.parse(raw) as {
    schema_version: number;
    events: RuntimeEvent[];
  };
  expect(fixture.schema_version).toBe(1);
  return fixture.events;
}

function p0Incident(incidentId: string) {
  const raw = readFileSync(
    resolve(
      process.cwd(),
      "../tests/fixtures/runtime/incidents/p0_incident_library_v1.json",
    ),
    "utf8",
  );
  const library = JSON.parse(raw) as {
    schema_version: number;
    incidents: Array<{
      incident_id: string;
      input: Record<string, string | number>;
      expected: Record<string, unknown>;
    }>;
  };
  expect(library.schema_version).toBe(1);
  return library.incidents.find((item) => item.incident_id === incidentId)!;
}

describe("runtimeEventStream", () => {
  it("maps RuntimeEvent rows to TurnEventPreview rows", () => {
    expect(
      runtimeEventToPreviewEvent(
        event({
          id: "evt-checkpoint",
          seq: 3,
          type: "item.completed",
          session_id: "console:identity",
          turn_id: "turn-identity",
          item_id: "item-identity",
          payload: {
            legacy_kind: "chat.checkpoint.created",
            run_id: "run-identity",
            client_turn_id: "client-turn-identity",
            phase: "checkpoint",
            trace_title: "创建检查点",
            trace_detail: "before-preview-fix",
            duration_ms: 42,
          },
        }),
      ),
    ).toMatchObject({
      id: "evt-checkpoint",
      seq: 3,
      session_id: "console:identity",
      run_id: "run-identity",
      client_turn_id: "client-turn-identity",
      turn_id: "turn-identity",
      item_id: "item-identity",
      kind: "chat.checkpoint.created",
      phase: "checkpoint",
      trace_title: "创建检查点",
      trace_detail: "before-preview-fix",
      duration_ms: 42,
    });
  });

  it("creates a visible in-progress assistant state as soon as a native turn starts", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-turn-started",
          seq: 1,
          type: "turn.started",
          status: "running",
          turn_id: "run-placeholder",
          payload: { legacy_kind: "chat.turn.started" },
        }),
      ]),
      undefined,
      undefined,
      [{ id: "user-1", role: "user", cards: [] }],
      { mirrorNativeMessages: true },
    );

    expect(patch.generating).toBe(true);
    expect(patch.messages).toHaveLength(2);
    expect(patch.messages?.[1]).toMatchObject({
      role: "assistant",
      msgStatus: "loading",
      cards: [
        {
          code: LANTAI_RESPONSE_CARD_CODE,
          data: {
            status: "in_progress",
            metadata: { native_runtime_turn_id: "run-placeholder" },
          },
        },
      ],
    });
  });

  it("attaches the selected work method and bounded runtime projection to its response", () => {
    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-research-start",
          seq: 1,
          type: "turn.started",
          status: "running",
          turn_id: "run-research",
          payload: {
            task_contract: { work_method_id: "deep_research" },
            phase: "framing",
          },
        }),
        event({
          id: "evt-research-tool",
          seq: 2,
          type: "tool.started",
          status: "running",
          turn_id: "run-research",
          payload: {
            phase: "collecting",
            trace_title: "检索权威来源",
            trace_detail: "正在交叉核对来源",
          },
        }),
      ],
      [{ id: "user-1", role: "user", cards: [] }],
      { strictTurnIdentity: true },
    );

    const metadata = (messages[1].cards?.[0]?.data as any).metadata;
    expect(metadata.work_method_id).toBe("deep_research");
    expect(metadata.runtime_projection_events).toEqual([
      expect.objectContaining({ id: "evt-research-start", seq: 1 }),
      expect.objectContaining({ id: "evt-research-tool", seq: 2 }),
    ]);
  });

  it("materializes tool lifecycle events inside the active assistant turn", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-tool-start",
          seq: 1,
          type: "tool.call.started",
          status: "running",
          source: "tool",
          item_id: "call-1",
          turn_id: "run-tools",
          payload: {
            tool_name: "send_file_to_user",
            arguments: { path: "/tmp/topic/项目路演PPT.pptx" },
          },
        }),
        event({
          id: "evt-tool-complete",
          seq: 2,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-1",
          turn_id: "run-tools",
          payload: {
            tool_name: "send_file_to_user",
            output: "File sent successfully.",
          },
        }),
      ]),
      { status: "running", run_id: "run-tools" },
      null,
      [{ id: "user-1", role: "user", cards: [] }],
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    const responseCard = patch.messages?.[1]?.cards?.[0]?.data as any;
    expect(responseCard.output).toEqual([
      expect.objectContaining({
        type: "plugin_call",
        content: [
          expect.objectContaining({
            data: expect.objectContaining({
              call_id: "call-1",
              name: "send_file_to_user",
            }),
          }),
        ],
      }),
      expect.objectContaining({
        type: "plugin_call_output",
        content: [
          expect.objectContaining({
            data: expect.objectContaining({
              call_id: "call-1",
              name: "send_file_to_user",
              output: "File sent successfully.",
            }),
          }),
        ],
      }),
    ]);
  });

  it("projects a delivered EffectRecord file into the assistant turn", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-delivery-complete",
          seq: 1,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-delivery",
          turn_id: "run-delivery",
          payload: {
            tool_name: "send_file_to_user",
            output: [
              {
                type: "file",
                source: {
                  type: "url",
                  url: "file:///tmp/topic/delivery-check-note.txt",
                },
                filename: "delivery-check-note.txt",
              },
              { type: "text", text: "File sent successfully." },
            ],
            artifact_effects: [
              {
                artifact_effect_id: "effect-delivery",
                artifact_id: "artifact-shared",
                artifact_role: "delivery",
                status: "delivered",
                relative_path: "delivery-check-note.txt",
                size_bytes: 75,
                sha256: "sha256:delivered",
                media_type: "text/plain",
              },
            ],
          },
        }),
        event({
          id: "evt-delivery-replay",
          seq: 2,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-delivery-replay",
          turn_id: "run-delivery",
          payload: {
            tool_name: "send_file_to_user",
            output: [
              {
                type: "file",
                source: {
                  type: "url",
                  url: "file:///tmp/topic/delivery-check-note.txt",
                },
                filename: "delivery-check-note.txt",
              },
            ],
            artifact_effects: [
              {
                artifact_effect_id: "effect-delivery-replay",
                artifact_id: "artifact-shared",
                artifact_role: "delivery",
                status: "delivered",
                relative_path: "delivery-check-note.txt",
                size_bytes: 75,
                sha256: "sha256:delivered",
              },
            ],
          },
        }),
      ]),
      { status: "running", run_id: "run-delivery" },
      null,
      [{ id: "user-1", role: "user", cards: [] }],
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    const responseCard = patch.messages?.[1]?.cards?.[0]?.data as any;
    expect(responseCard.output).toContainEqual(
      expect.objectContaining({
        type: "message",
        status: "completed",
        content: [
          expect.objectContaining({
            type: "file",
            file_url: "file:///tmp/topic/delivery-check-note.txt",
            file_source_url: "file:///tmp/topic/delivery-check-note.txt",
            file_name: "delivery-check-note.txt",
            file_size: 75,
          }),
        ],
        metadata: expect.objectContaining({
          runtime_artifact_id: "artifact-shared",
          runtime_artifact_sha256: "sha256:delivered",
          runtime_artifact_effect_id: "effect-delivery",
        }),
      }),
    );
    expect(
      responseCard.output.filter(
        (item: any) => item.metadata?.runtime_artifact_id === "artifact-shared",
      ),
    ).toHaveLength(1);
  });

  it("projects a delivered video into a durable assistant video message", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-video-delivery-complete",
          seq: 1,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-video-delivery",
          turn_id: "run-video-delivery",
          payload: {
            tool_name: "send_file_to_user",
            output: [
              {
                type: "video",
                source: {
                  type: "url",
                  url: "file:///tmp/topic/generated-video.mp4",
                },
              },
              { type: "text", text: "File sent successfully." },
            ],
            artifact_effects: [
              {
                artifact_effect_id: "effect-video-delivery",
                artifact_id: "artifact-video",
                artifact_role: "delivery",
                status: "delivered",
                relative_path: "generated-video.mp4",
                size_bytes: 9_595_196,
                sha256: "sha256:video-delivered",
                media_type: "video/mp4",
              },
            ],
          },
        }),
      ]),
      { status: "running", run_id: "run-video-delivery" },
      null,
      [{ id: "user-1", role: "user", cards: [] }],
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    const responseCard = patch.messages?.[1]?.cards?.[0]?.data as any;
    expect(responseCard.output).toContainEqual(
      expect.objectContaining({
        type: "message",
        status: "completed",
        content: [
          expect.objectContaining({
            type: "video",
            video_url: "file:///tmp/topic/generated-video.mp4",
          }),
        ],
        metadata: expect.objectContaining({
          runtime_artifact_id: "artifact-video",
          runtime_artifact_sha256: "sha256:video-delivered",
          runtime_artifact_effect_id: "effect-video-delivery",
        }),
      }),
    );
  });

  it("does not project an uncommitted or non-delivery artifact as a file", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-primary-complete",
          seq: 1,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-write",
          turn_id: "run-primary",
          payload: {
            tool_name: "write_file",
            output: [
              {
                type: "file",
                source: { type: "url", url: "file:///tmp/topic/draft.txt" },
                filename: "draft.txt",
              },
            ],
            artifact_effects: [
              {
                artifact_effect_id: "effect-primary",
                artifact_id: "artifact-primary",
                artifact_role: "primary",
                status: "committed",
                relative_path: "draft.txt",
                sha256: "sha256:primary",
              },
            ],
          },
        }),
      ]),
      { status: "running", run_id: "run-primary" },
      null,
      [{ id: "user-1", role: "user", cards: [] }],
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    const responseCard = patch.messages?.[1]?.cards?.[0]?.data as any;
    expect(
      responseCard.output.filter((item: any) => item.type === "message"),
    ).toEqual([]);
  });

  it("uses the EffectRecord relative path to disambiguate equal filenames", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-nested-delivery",
          seq: 1,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-nested-delivery",
          turn_id: "run-nested-delivery",
          payload: {
            tool_name: "send_file_to_user",
            output: [
              {
                type: "file",
                source: { type: "url", url: "file:///tmp/topic/a/report.txt" },
                filename: "report.txt",
              },
              {
                type: "file",
                source: { type: "url", url: "file:///tmp/topic/b/report.txt" },
                filename: "report.txt",
              },
            ],
            artifact_effects: [
              {
                artifact_effect_id: "effect-nested-delivery",
                artifact_id: "artifact-nested-delivery",
                artifact_role: "delivery",
                status: "delivered",
                relative_path: "b/report.txt",
                size_bytes: 12,
                sha256: "sha256:nested-delivery",
              },
            ],
          },
        }),
      ]),
      { status: "running", run_id: "run-nested-delivery" },
      null,
      [{ id: "user-1", role: "user", cards: [] }],
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    const responseCard = patch.messages?.[1]?.cards?.[0]?.data as any;
    const artifact = responseCard.output.find(
      (item: any) => item.metadata?.runtime_artifact_id,
    );
    expect(artifact.content[0].file_url).toBe("file:///tmp/topic/b/report.txt");
  });

  it("refuses an ambiguous delivered file projection", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-ambiguous-delivery",
          seq: 1,
          type: "tool.call.completed",
          status: "completed",
          source: "tool",
          item_id: "call-ambiguous-delivery",
          turn_id: "run-ambiguous-delivery",
          payload: {
            tool_name: "send_file_to_user",
            output: [
              {
                type: "file",
                source: { type: "url", url: "file:///tmp/topic/a/report.txt" },
                filename: "report.txt",
              },
              {
                type: "file",
                source: { type: "url", url: "file:///tmp/topic/b/report.txt" },
                filename: "report.txt",
              },
            ],
            artifact_effects: [
              {
                artifact_effect_id: "effect-ambiguous-delivery",
                artifact_id: "artifact-ambiguous-delivery",
                artifact_role: "delivery",
                status: "delivered",
                relative_path: "report.txt",
                sha256: "sha256:ambiguous-delivery",
              },
            ],
          },
        }),
      ]),
      { status: "running", run_id: "run-ambiguous-delivery" },
      null,
      [{ id: "user-1", role: "user", cards: [] }],
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    const responseCard = patch.messages?.[1]?.cards?.[0]?.data as any;
    expect(
      responseCard.output.filter(
        (item: any) => item.metadata?.runtime_artifact_id,
      ),
    ).toEqual([]);
  });

  it("keeps overlapping native turns in separate visible placeholders", () => {
    const first = ensureNativeRuntimeTurnPlaceholder(
      [{ id: "user-1", role: "user", cards: [] }],
      "run-first",
      { strictTurnIdentity: true },
    );
    const second = ensureNativeRuntimeTurnPlaceholder(
      first.messages,
      "run-second",
      { strictTurnIdentity: true },
    );

    expect(first.inserted).toBe(true);
    expect(second.inserted).toBe(true);
    expect(second.messages).toHaveLength(3);
    expect(
      second.messages
        .slice(1)
        .map(
          (message: any) =>
            message.cards?.[0]?.data?.metadata?.native_runtime_turn_id,
        ),
    ).toEqual(["run-first", "run-second"]);
  });

  it("accepts shared RuntimeEvent v1 fixture rows", () => {
    const [fixtureEvent] = sharedRuntimeEventFixture();
    const preview = runtimeEventToPreviewEvent(fixtureEvent);

    expect(fixtureEvent.schema_version).toBe(1);
    expect(fixtureEvent.type).toBe("turn.continue_requested");
    expect(preview).toMatchObject({
      id: "evt-continue-1",
      kind: "turn.continue_requested",
      status: "info",
    });
  });

  it("merges preview rows by id and keeps the newest sequence window", () => {
    const merged = mergePreviewEvents(
      [
        { id: "old", seq: 1 } as any,
        { id: "dup", seq: 2, label: "old" } as any,
      ],
      [
        { id: "dup", seq: 3, label: "new" } as any,
        { id: "next", seq: 4 } as any,
      ],
      2,
    );

    expect(merged.map((item) => item.id)).toEqual(["dup", "next"]);
    expect(merged[0].label).toBe("new");
  });

  it("replays the P0 duplicate-reply incident as one visible output", () => {
    const incident = p0Incident("duplicate-execution-and-reply-v1");
    const fixture = incident.input;
    const expected = incident.expected as {
      visible_output_count: number;
      visible_text_instances: number;
    };
    const visibleText = String(fixture.visible_text);
    const buildCompleted = (
      id: string,
      itemId: string,
      seq: number,
    ): RuntimeEvent =>
      event({
        id,
        seq,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        turn_id: String(fixture.first_run_id),
        item_id: itemId,
        payload: {
          client_turn_id: fixture.client_turn_id,
          source_message_id: fixture.source_message_id,
          content_type: "text",
          stream_message_type: "message",
          text: visibleText,
          message: {
            id: itemId,
            role: "assistant",
            metadata: {
              client_turn_id: fixture.client_turn_id,
              source_message_id: fixture.source_message_id,
            },
            content: [{ type: "text", text: visibleText }],
          },
        },
      });

    const messages = buildNativeRuntimeStreamMessages(
      [
        buildCompleted(
          String(fixture.first_event_id),
          String(fixture.first_item_id),
          1,
        ),
        buildCompleted(
          String(fixture.mirror_event_id),
          String(fixture.mirror_item_id),
          2,
        ),
      ],
      [],
      { strictTurnIdentity: true },
    );
    const output = (messages[0]?.cards?.[0]?.data as any)?.output || [];
    const serialized = JSON.stringify(output);

    expect(output).toHaveLength(expected.visible_output_count);
    expect(serialized.split(visibleText).length - 1).toBe(
      expected.visible_text_instances,
    );
  });

  it("does not append a stale recovery delta after the full message completed", () => {
    const completed = event({
      id: "evt-completed",
      seq: 10,
      type: "message.completed",
      status: "completed",
      source: "assistant",
      turn_id: "run-terminal",
      item_id: "msg-terminal",
      payload: {
        source_message_id: "msg-terminal",
        client_turn_id: "client-terminal",
        content_type: "text",
        stream_message_type: "message",
        message: {
          id: "msg-terminal",
          role: "assistant",
          metadata: {
            source_message_id: "msg-terminal",
            client_turn_id: "client-terminal",
          },
          content: [{ type: "text", text: "answer1" }],
        },
      },
    });
    const staleDelta = event({
      id: "evt-stale-delta",
      seq: 9,
      type: "message.delta",
      status: "running",
      source: "assistant",
      turn_id: "run-terminal",
      item_id: "msg-terminal",
      payload: {
        source_message_id: "msg-terminal",
        client_turn_id: "client-terminal",
        content_type: "text",
        stream_message_type: "message",
        delta: "1",
        message: {
          id: "msg-terminal",
          role: "assistant",
          metadata: {
            source_message_id: "msg-terminal",
            client_turn_id: "client-terminal",
          },
          content: [{ type: "text", text: "1" }],
        },
      },
    });

    const afterCompleted = buildNativeRuntimeStreamMessages([completed], [], {
      strictTurnIdentity: true,
    });
    const recovered = buildNativeRuntimeStreamMessages(
      [staleDelta],
      afterCompleted,
      { strictTurnIdentity: true },
    );
    const output = (recovered[0]?.cards?.[0]?.data as any)?.output || [];

    expect(output).toHaveLength(1);
    expect(output[0]?.content?.[0]?.text).toBe("answer1");
    expect(output[0]?.status).toBe("completed");
  });

  it("repairs repeated live deltas from the canonical completed snapshot", () => {
    const answer = "文件写好了，我现在跑一遍验证。";
    const delta = (id: string, seq: number) =>
      event({
        id,
        seq,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-repeated-live",
        item_id: "msg-repeated-live",
        payload: {
          source_message_id: "msg-repeated-live",
          client_turn_id: "client-repeated-live",
          content_type: "text",
          stream_message_type: "message",
          delta: answer,
          message: {
            id: "msg-repeated-live",
            role: "assistant",
            metadata: {
              source_message_id: "msg-repeated-live",
              client_turn_id: "client-repeated-live",
            },
            content: [],
          },
        },
      });
    const completed = event({
      id: "evt-repeated-completed",
      seq: 4,
      type: "message.completed",
      status: "completed",
      source: "assistant",
      turn_id: "run-repeated-live",
      item_id: "msg-repeated-live",
      payload: {
        source_message_id: "msg-repeated-live",
        client_turn_id: "client-repeated-live",
        content_type: "text",
        stream_message_type: "message",
        message: {
          id: "msg-repeated-live",
          role: "assistant",
          metadata: {
            source_message_id: "msg-repeated-live",
            client_turn_id: "client-repeated-live",
          },
          content: [{ type: "text", text: answer }],
        },
      },
    });

    const messages = buildNativeRuntimeStreamMessages(
      [
        delta("evt-repeated-a", 1),
        delta("evt-repeated-b", 2),
        delta("evt-repeated-c", 3),
        completed,
      ],
      [],
      { strictTurnIdentity: true },
    );
    const output = (messages[0]?.cards?.[0]?.data as any)?.output || [];

    expect(output).toHaveLength(1);
    expect(output[0]?.content?.[0]?.text).toBe(answer);
    expect(output[0]?.status).toBe("completed");
  });

  it("replays the P0 restart incident without retaining the old generation", () => {
    const incident = p0Incident("restart-stale-generation-v1");
    const fixture = incident.input;
    const expected = incident.expected as {
      status: string;
      stream_epoch: string;
      run_id: string;
      preview_event_ids: string[];
    };
    const incoming = response([
      event({
        id: String(fixture.new_event_id),
        seq: Number(fixture.new_event_seq),
        type: "turn.started",
        status: "running",
        turn_id: String(fixture.new_run_id),
        payload: { legacy_kind: "chat.turn.started" },
      }),
    ]);
    incoming.chat_id = String(fixture.chat_id);
    incoming.stream_epoch = String(fixture.new_stream_epoch);
    incoming.cursor = String(fixture.new_cursor);
    incoming.resync_required = true;
    incoming.resync_reason = "stream_epoch_mismatch";

    const patch = buildRuntimeEventSessionPatch(incoming, {
      status: "completed",
      run_id: String(fixture.old_run_id),
      stream_epoch: String(fixture.old_stream_epoch),
      events_preview: [
        {
          id: String(fixture.old_event_id),
          seq: Number(fixture.old_event_seq),
          type: "turn.completed",
          kind: "chat.turn.settled",
          status: "completed",
          label: "旧进程已经结束",
        } as any,
      ],
    });

    expect(patch.runtimeState.status).toBe(expected.status);
    expect(patch.runtimeState.stream_epoch).toBe(expected.stream_epoch);
    expect(patch.runtimeState.run_id).toBe(expected.run_id);
    expect(patch.runtimeState.events_preview?.map((item) => item.id)).toEqual(
      expected.preview_event_ids,
    );
  });

  it("rebuilds preview state and persists the delivery cursor after resync", () => {
    const incoming = response([
      event({ id: "new-ledger", seq: 1, type: "turn.started" }),
    ]);
    incoming.stream_epoch = "epoch-b";
    incoming.cursor = "epoch-b:1";
    incoming.resync_required = true;
    incoming.resync_reason = "stream_epoch_mismatch";

    const patch = buildRuntimeEventSessionPatch(incoming, {
      status: "running",
      stream_epoch: "epoch-a",
      stream_cursor: "epoch-a:20",
      events_preview: [
        runtimeEventToPreviewEvent(event({ id: "old-ledger", seq: 20 })),
      ],
    });

    expect(patch.runtimeState).toMatchObject({
      stream_epoch: "epoch-b",
      stream_cursor: "epoch-b:1",
    });
    expect(patch.runtimeState.events_preview?.map((item) => item.id)).toEqual([
      "new-ledger",
    ]);
  });

  it("builds a running session patch from non-terminal events", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-start",
          seq: 1,
          type: "turn.started",
          status: "running",
          payload: {
            legacy_kind: "chat.turn.started",
            trace_title: "开始运行",
          },
        }),
        event({
          id: "evt-tool",
          seq: 2,
          type: "tool.completed",
          status: "completed",
          payload: {
            legacy_kind: "chat.tool_call.finished",
            phase: "preview",
            trace_title: "启动预览：npm",
          },
        }),
      ]),
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-1",
      summary: "启动预览：npm",
      can_stop: true,
      can_reconnect: true,
    });
    expect(patch.turnEventPreview.events).toHaveLength(2);
  });

  it("marks final answer and errors as terminal", () => {
    expect(
      buildRuntimeEventSessionPatch(
        response([
          event({
            id: "evt-final",
            type: "item.completed",
            payload: {
              legacy_kind: "chat.final_answer",
              trace_title: "本轮回复完成",
            },
          }),
        ]),
      ).runtimeState.status,
    ).toBe("completed");

    expect(
      buildRuntimeEventSessionPatch(
        response([
          event({
            id: "evt-error",
            type: "turn.failed",
            status: "failed",
            payload: {
              legacy_kind: "chat.error",
              trace_title: "运行失败",
            },
          }),
        ]),
      ).runtimeState.status,
    ).toBe("failed");

    expect(
      buildRuntimeEventSessionPatch(
        response([
          event({
            id: "evt-error-before-settled",
            seq: 4,
            type: "turn.failed",
            status: "failed",
            payload: {
              legacy_kind: "chat.error",
              trace_title: "运行失败",
            },
          }),
          event({
            id: "evt-settled-after-error",
            seq: 5,
            type: "turn.completed",
            status: "finished",
            payload: {
              legacy_kind: "chat.turn.settled",
              final_task_status: "finished",
              trace_title: "本轮结束",
            },
          }),
        ]),
      ).runtimeState.status,
    ).toBe("failed");

    expect(
      buildRuntimeEventSessionPatch(
        response([
          event({
            id: "evt-failed-settled",
            type: "turn.failed",
            status: "failed",
            payload: {
              legacy_kind: "chat.turn.settled",
              final_task_status: "failed",
            },
          }),
        ]),
      ).runtimeState.status,
    ).toBe("failed");
  });

  it("keeps semantic delivery outcome separate from runtime completion", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-incomplete-settled",
          type: "turn.completed",
          status: "finished",
          payload: {
            legacy_kind: "chat.turn.settled",
            final_task_status: "finished",
            outcome_status: "incomplete",
            delivery_status: "partial",
          },
        }),
      ]),
    );

    expect(patch.runtimeState.status).toBe("completed");
    expect(patch.runtimeState.outcome_status).toBe("incomplete");
    expect(patch.runtimeState.delivery_status).toBe("partial");
  });

  it("forces delivery back to pending while the current run is active", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-running-boundary",
          type: "turn.started",
          status: "running",
          turn_id: "run-current",
        }),
        event({
          id: "evt-running-current",
          type: "tool.call.started",
          status: "running",
          turn_id: "run-current",
          payload: { delivery_status: "verified" },
        }),
      ]),
      {
        status: "completed",
        run_id: "run-old",
        delivery_status: "verified",
      },
      null,
    );

    expect(patch.runtimeState.status).toBe("running");
    expect(patch.runtimeState.delivery_status).toBe("pending");
  });

  it("does not inherit an older turn terminal across a newer turn boundary", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-old-terminal",
          seq: 40,
          type: "turn.completed",
          status: "completed",
          turn_id: "run-old",
          payload: {
            legacy_kind: "chat.turn.settled",
            outcome_status: "delivered",
            delivery_status: "verified",
          },
        }),
        event({
          id: "evt-new-start",
          seq: 41,
          type: "turn.started",
          status: "running",
          turn_id: "run-new",
          payload: { legacy_kind: "chat.turn.started" },
        }),
      ]),
      {
        status: "completed",
        run_id: "run-old",
        outcome_status: "delivered",
        delivery_status: "verified",
      },
    );

    expect(patch.runtimeState.status).toBe("running");
    expect(patch.runtimeState.run_id).toBe("run-new");
    expect(patch.runtimeState.outcome_status).toBe("unknown");
    expect(patch.runtimeState.delivery_status).toBe("pending");
    expect(patch.runtimeState.events_preview?.map((item) => item.id)).toEqual([
      "evt-new-start",
    ]);
  });

  it("does not treat message.completed as a turn terminal", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-message-completed",
          seq: 2,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          item_id: "msg-1",
          payload: {
            legacy_kind: "chat.message.completed",
            trace_title: "消息片段完成",
          },
        }),
      ]),
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState.status).toBe("running");
  });

  it("does not settle the turn from a stalled item lifecycle status", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-item-stalled",
          seq: 3,
          type: "item.completed",
          status: "stalled",
          turn_id: "run-parent",
          payload: { trace_title: "子步骤暂时没有推进" },
        }),
      ]),
      { status: "running", run_id: "run-parent" },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState.status).toBe("running");
    expect(patch.acceptedTerminal).toBe(false);
  });

  it("keeps a settled parent terminal when a late child item arrives", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-late-child-item",
          seq: 12,
          type: "item.completed",
          status: "completed",
          turn_id: "run-child",
          payload: {
            parent_run_id: "run-parent",
            outcome_status: "blocked",
            delivery_status: "not_delivered",
          },
        }),
      ]),
      {
        status: "completed",
        run_id: "run-parent",
        outcome_status: "delivered",
        delivery_status: "verified",
        can_stop: false,
        can_reconnect: false,
      },
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState).toMatchObject({
      status: "completed",
      run_id: "run-parent",
      outcome_status: "delivered",
      delivery_status: "verified",
      can_stop: false,
      can_reconnect: false,
    });
    expect(patch.turnEventPreview.events).toEqual([
      expect.objectContaining({ id: "evt-late-child-item" }),
    ]);
    expect(patch.acceptedTerminal).toBe(false);
  });

  it("does not revive a settled parent when a child run starts late", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-late-child-start",
          seq: 13,
          type: "turn.started",
          status: "running",
          turn_id: "run-child",
          payload: {
            parent_run_id: "run-parent",
            client_turn_id: "client-child",
          },
        }),
      ]),
      {
        status: "completed",
        run_id: "run-parent",
        client_turn_id: "client-parent",
        outcome_status: "delivered",
        delivery_status: "verified",
      },
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState).toMatchObject({
      status: "completed",
      run_id: "run-parent",
      client_turn_id: "client-parent",
      outcome_status: "delivered",
      delivery_status: "verified",
    });
    expect(patch.acceptedTerminal).toBe(false);
  });

  it.each(
    ["completed", "failed", "interrupted", "stalled"].flatMap((parentStatus) =>
      [
        { type: "turn.started", status: "running" },
        { type: "turn.continue_requested", status: "running" },
        { type: "item.completed", status: "completed" },
        { type: "turn.failed", status: "failed" },
      ].map((child) => ({ parentStatus, ...child })),
    ),
  )(
    "keeps a $parentStatus parent settled after late child $type",
    ({ parentStatus, type, status }) => {
      const patch = buildRuntimeEventSessionPatch(
        response([
          event({
            id: `evt-child-${parentStatus}-${type}`,
            seq: 13,
            type,
            status,
            turn_id: "run-child",
            payload: {
              parent_run_id: "run-parent",
              ...(type === "turn.failed"
                ? { final_task_status: "failed" }
                : {}),
            },
          }),
        ]),
        {
          status: parentStatus,
          run_id: "run-parent",
          client_turn_id: "client-parent",
          outcome_status: "delivered",
          delivery_status: "verified",
        },
      );

      expect(patch.generating).toBe(false);
      expect(patch.runtimeState).toMatchObject({
        status: parentStatus,
        run_id: "run-parent",
        client_turn_id: "client-parent",
        outcome_status: "delivered",
        delivery_status: "verified",
      });
      expect(patch.acceptedTerminal).toBe(false);
    },
  );

  it("keeps a running parent active when its child run fails", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-child-failed",
          seq: 12,
          type: "turn.failed",
          status: "failed",
          turn_id: "run-child",
          payload: {
            parent_run_id: "run-parent",
            final_task_status: "failed",
            outcome_status: "blocked",
            delivery_status: "not_delivered",
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-parent",
        client_turn_id: "client-parent",
        outcome_status: "unknown",
        delivery_status: "pending",
        events_preview: [
          runtimeEventToPreviewEvent(
            event({
              id: "evt-parent-started",
              seq: 10,
              type: "turn.started",
              status: "running",
              turn_id: "run-parent",
              payload: { client_turn_id: "client-parent" },
            }),
          ),
        ],
      },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-parent",
      client_turn_id: "client-parent",
      outcome_status: "unknown",
      delivery_status: "pending",
    });
    expect(patch.acceptedTerminal).toBe(false);
  });

  it("does not settle a parent from a parent-linked terminal missing child run identity", () => {
    const childTerminal = event({
      id: "evt-child-terminal-without-run",
      type: "turn.failed",
      status: "failed",
      turn_id: null,
      payload: {
        parent_run_id: "run-parent",
        final_task_status: "failed",
      },
    });
    const patch = buildRuntimeEventSessionPatch(response([childTerminal]), {
      status: "running",
      run_id: "run-parent",
    });

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-parent",
    });
    expect(patch.acceptedTerminal).toBe(false);
    expect(runtimeResponseHasTerminalEvent(response([childTerminal]))).toBe(
      false,
    );
  });

  it("does not let a child terminal override a parent terminal in the same batch", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-parent-completed",
          seq: 20,
          type: "turn.completed",
          status: "completed",
          turn_id: "run-parent",
          payload: {
            client_turn_id: "client-parent",
            final_task_status: "completed",
            outcome_status: "delivered",
            delivery_status: "verified",
          },
        }),
        event({
          id: "evt-child-failed-after-parent",
          seq: 21,
          type: "turn.failed",
          status: "failed",
          turn_id: "run-child",
          payload: {
            parent_run_id: "run-parent",
            final_task_status: "failed",
            outcome_status: "blocked",
            delivery_status: "not_delivered",
          },
        }),
      ]),
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState).toMatchObject({
      status: "completed",
      run_id: "run-parent",
      client_turn_id: "client-parent",
      outcome_status: "delivered",
      delivery_status: "verified",
    });
    expect(patch.acceptedTerminal).toBe(true);
  });

  it("rejects an untagged old terminal after a new client turn is visible on the same run", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-old-untagged-terminal",
          seq: 12,
          type: "turn.failed",
          status: "failed",
          turn_id: "run-shared",
          created_at: "2026-05-29T08:00:30Z",
          payload: {
            final_task_status: "failed",
            outcome_status: "blocked",
            delivery_status: "not_delivered",
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-shared",
        client_turn_id: "client-turn-old",
        outcome_status: "unknown",
        delivery_status: "pending",
        events_preview: [
          runtimeEventToPreviewEvent(
            event({
              id: "evt-old-progress",
              seq: 11,
              type: "message.delta",
              status: "running",
              turn_id: "run-shared",
              created_at: "2026-05-29T08:00:20Z",
              payload: { client_turn_id: "client-turn-old" },
            }),
          ),
        ],
      },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "user-new",
            role: "user",
            createdAt: "2026-05-29T08:01:00Z",
            metadata: { client_turn_id: "client-turn-new" },
            cards: [],
          },
        ],
      },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-shared",
      client_turn_id: "client-turn-new",
      outcome_status: "unknown",
      delivery_status: "pending",
    });
    expect(patch.acceptedTerminal).toBe(false);
  });

  it("rejects a late explicit terminal after a real optimistic user turn on the same run", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "old-terminal",
          seq: 11,
          created_at: "2026-09-05T00:02:00Z",
          turn_id: "run-shared",
          type: "turn.failed",
          status: "failed",
          payload: {
            client_turn_id: "user-old",
            final_task_status: "failed",
            outcome_status: "blocked",
            delivery_status: "not_delivered",
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-shared",
        client_turn_id: "user-old",
        started_at: "2026-09-05T00:00:00Z",
        updated_at: "2026-09-05T00:00:45Z",
        events_preview: [
          {
            id: "old-progress",
            seq: 10,
            ts: "2026-09-05T00:00:40Z",
            type: "message.delta",
            kind: "chat.message.delta",
            status: "running",
            label: "旧轮进度",
            detail: "",
            run_id: "run-shared",
            turn_id: "run-shared",
            client_turn_id: "user-old",
          },
        ],
      },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "user-new",
            role: "user",
            createdAt: "2026-09-05T00:01:00Z",
            metadata: {
              client_turn_id: "user-new",
              client_turn_id_origin: "optimistic_submission",
            },
            cards: [],
          },
        ],
      },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-shared",
      client_turn_id: "user-new",
      outcome_status: "unknown",
      delivery_status: "pending",
    });
    expect(patch.acceptedTerminal).toBe(false);
  });

  it("keeps the active foreground run when a different run terminal arrives late", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "old-late-terminal",
          seq: 21,
          created_at: "2026-09-05T00:02:00Z",
          turn_id: "run-old",
          type: "turn.failed",
          status: "failed",
          payload: {
            final_task_status: "failed",
            outcome_status: "blocked",
            delivery_status: "not_delivered",
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-new",
        client_turn_id: "client-turn-new",
        started_at: "2026-09-05T00:01:00Z",
        updated_at: "2026-09-05T00:01:30Z",
        outcome_status: "unknown",
        delivery_status: "pending",
        events_preview: [
          {
            id: "new-progress",
            seq: 20,
            ts: "2026-09-05T00:01:30Z",
            type: "message.delta",
            kind: "chat.message.delta",
            status: "running",
            label: "新轮进度",
            detail: "",
            run_id: "run-new",
            turn_id: "run-new",
            client_turn_id: "client-turn-new",
          },
        ],
      },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-new",
      client_turn_id: "client-turn-new",
      outcome_status: "unknown",
      delivery_status: "pending",
    });
    expect(patch.acceptedTerminal).toBe(false);
  });

  it.each([false, true])(
    "accepts a terminal-only %sresync for the latest authoritative user turn",
    (resyncRequired) => {
      const terminalResponse = {
        ...response([
          event({
            id: "new-terminal-without-start",
            seq: 31,
            created_at: "2026-09-05T00:02:00Z",
            turn_id: "run-new",
            type: "turn.completed",
            status: "completed",
            payload: {
              client_turn_id: "user-new",
              final_task_status: "completed",
              outcome_status: "delivered",
              delivery_status: "verified",
            },
          }),
        ]),
        resync_required: resyncRequired,
      };
      const patch = buildRuntimeEventSessionPatch(
        terminalResponse,
        {
          status: "running",
          run_id: "run-old",
          client_turn_id: "user-old",
          started_at: "2026-09-05T00:00:00Z",
          updated_at: "2026-09-05T00:00:30Z",
          outcome_status: "unknown",
          delivery_status: "pending",
        },
        undefined,
        undefined,
        {
          identityMessages: [
            {
              id: "user-new",
              role: "user",
              createdAt: "2026-09-05T00:01:00Z",
              metadata: {
                client_turn_id: "user-new",
                client_turn_id_origin: "optimistic_submission",
              },
              cards: [],
            },
          ],
        },
      );

      expect(patch.generating).toBe(false);
      expect(patch.acceptedTerminal).toBe(true);
      expect(patch.runtimeState).toMatchObject({
        status: "completed",
        run_id: "run-new",
        client_turn_id: "user-new",
        outcome_status: "delivered",
        delivery_status: "verified",
      });
      expect(patch.runtimeState.events_preview?.map((item) => item.id)).toEqual(
        ["new-terminal-without-start"],
      );
    },
  );

  it("rejects a stale same-run boundary replay older than the latest user", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "old-boundary",
          seq: 1,
          created_at: "2026-09-05T00:00:00Z",
          turn_id: "run-shared",
          type: "turn.started",
          status: "running",
          payload: { client_turn_id: "client-turn-old" },
        }),
        event({
          id: "old-boundary-terminal",
          seq: 2,
          created_at: "2026-09-05T00:00:30Z",
          turn_id: "run-shared",
          type: "turn.completed",
          status: "completed",
          payload: {
            client_turn_id: "client-turn-old",
            final_task_status: "completed",
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-shared",
        client_turn_id: "client-turn-old",
        started_at: "2026-09-05T00:00:00Z",
        updated_at: "2026-09-05T00:00:30Z",
        events_preview: [
          {
            id: "old-progress",
            seq: 10,
            ts: "2026-09-05T00:00:20Z",
            type: "message.delta",
            kind: "chat.message.delta",
            status: "running",
            label: "旧轮进度",
            detail: "",
            run_id: "run-shared",
            turn_id: "run-shared",
            client_turn_id: "client-turn-old",
          },
        ],
      },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "user-new",
            role: "user",
            createdAt: "2026-09-05T00:01:00Z",
            metadata: { client_turn_id: "client-turn-new" },
            cards: [],
          },
        ],
      },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-shared",
      client_turn_id: "client-turn-new",
    });
    expect(patch.acceptedTerminal).toBe(false);
  });

  it("does not project an unrelated run into the active transcript or preview", () => {
    const existingPreview = {
      id: "new-progress",
      seq: 20,
      ts: "2026-09-05T00:01:30Z",
      type: "item.started",
      kind: "item.started",
      status: "running",
      label: "新轮进度",
      detail: "",
      run_id: "run-new",
      turn_id: "run-new",
    };
    const existingMessages = [
      { id: "user-new", role: "user", cards: [] },
      { id: "assistant-new", role: "assistant", cards: [] },
    ];
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "old-run-delta",
          seq: 21,
          type: "message.delta",
          status: "running",
          turn_id: "run-old",
          payload: {
            delta: "不应串入的新文本",
            message: { id: "old-message", role: "assistant" },
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-new",
        events_preview: [existingPreview],
        summary: "新轮进度",
      },
      undefined,
      existingMessages,
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState.run_id).toBe("run-new");
    expect(patch.runtimeState.summary).toBe("新轮进度");
    expect(patch.runtimeState.events_preview?.map((item) => item.id)).toEqual([
      "new-progress",
    ]);
    expect(patch.messages).toBeUndefined();
  });

  it.each(["completed", "failed", "interrupted", "stalled"] as const)(
    "keeps a %s parent settled when a nested child reports late activity",
    (parentStatus) => {
      const patch = buildRuntimeEventSessionPatch(
        response([
          event({
            id: "grandchild-item",
            seq: 12,
            type: "item.completed",
            status: "completed",
            turn_id: "run-grandchild",
            payload: { parent_run_id: "run-child" },
          }),
        ]),
        {
          status: parentStatus,
          run_id: "run-parent",
          delivery_status: "verified",
          events_preview: [
            runtimeEventToPreviewEvent(
              event({
                id: "child-start",
                seq: 10,
                type: "turn.started",
                status: "running",
                turn_id: "run-child",
                payload: { parent_run_id: "run-parent" },
              }),
            ),
          ],
        },
      );

      expect(patch.runtimeState).toMatchObject({
        status: parentStatus,
        run_id: "run-parent",
        delivery_status: "verified",
      });
      expect(patch.generating).toBe(false);
      expect(patch.acceptedTerminal).toBe(false);
    },
  );

  it("treats a clearly newer unscoped user row as a new logical turn", () => {
    const identityMessages = [
      {
        id: "user-new",
        role: "user",
        createdAt: "2026-09-05T00:01:00Z",
        cards: [],
      },
    ];
    const progress = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "old-untagged-boundary",
          created_at: "2026-09-05T00:00:00Z",
          type: "turn.started",
          status: "running",
          turn_id: "run-shared",
          payload: { client_turn_id: "client-old" },
        }),
        event({
          id: "old-untagged-terminal",
          created_at: "2026-09-05T00:02:00Z",
          type: "turn.completed",
          status: "completed",
          turn_id: "run-shared",
          payload: {
            client_turn_id: "client-old",
            outcome_status: "delivered",
            delivery_status: "verified",
          },
        }),
      ]),
      {
        status: "completed",
        run_id: "run-shared",
        client_turn_id: "client-old",
        started_at: "2026-09-05T00:00:00Z",
        updated_at: "2026-09-05T00:00:30Z",
      },
      undefined,
      undefined,
      { identityMessages },
    );

    expect(progress.runtimeState).toMatchObject({
      status: "running",
      client_turn_id: null,
      outcome_status: "unknown",
      delivery_status: "pending",
    });
    expect(progress.acceptedTerminal).toBe(false);

    const completed = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "current-untagged-terminal",
          created_at: "2026-09-05T00:03:00Z",
          type: "turn.completed",
          status: "completed",
          turn_id: "run-shared",
        }),
      ]),
      progress.runtimeState,
      undefined,
      undefined,
      { identityMessages },
    );
    expect(completed.runtimeState.status).toBe("completed");
    expect(completed.acceptedTerminal).toBe(true);
  });

  it("rejects an explicit terminal older than the latest unscoped user", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-stale-explicit-terminal",
          seq: 11,
          created_at: "2026-09-05T00:00:40Z",
          type: "turn.failed",
          status: "failed",
          turn_id: "run-shared",
          payload: {
            client_turn_id: "client-old",
            final_task_status: "failed",
            outcome_status: "blocked",
            delivery_status: "not_delivered",
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-shared",
        client_turn_id: "client-old",
        started_at: "2026-09-05T00:00:00Z",
        // This is reducer receipt time, not causal event time. It must not let
        // an older terminal jump over the user timestamp fence.
        updated_at: "2026-09-05T00:02:00Z",
        outcome_status: "unknown",
        delivery_status: "pending",
        events_preview: [
          runtimeEventToPreviewEvent(
            event({
              id: "evt-old-progress",
              seq: 10,
              created_at: "2026-09-05T00:00:30Z",
              type: "item.started",
              status: "running",
              turn_id: "run-shared",
              payload: { client_turn_id: "client-old" },
            }),
          ),
        ],
      },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "user-new",
            role: "user",
            createdAt: "2026-09-05T00:01:00Z",
            cards: [],
          },
        ],
      },
    );

    expect(patch.acceptedTerminal).toBe(false);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-shared",
      outcome_status: "unknown",
      delivery_status: "pending",
    });
  });

  it("does not let a queued user row fence the active turn terminal", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "active-terminal",
          created_at: "2026-09-05T00:01:30Z",
          type: "turn.completed",
          status: "completed",
          turn_id: "run-active",
          payload: { client_turn_id: "client-active" },
        }),
      ]),
      {
        status: "running",
        run_id: "run-active",
        client_turn_id: "client-active",
        started_at: "2026-09-05T00:00:00Z",
        updated_at: "2026-09-05T00:00:30Z",
      },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "user-queued",
            role: "user",
            createdAt: "2026-09-05T00:01:00Z",
            metadata: { client_turn_id: "user-queued" },
            cards: [
              {
                code: "RequestCard",
                data: { runtime_queue_status: "queued" },
              },
            ],
          },
        ],
      },
    );

    expect(patch.runtimeState.status).toBe("completed");
    expect(patch.runtimeState.client_turn_id).toBe("client-active");
    expect(patch.acceptedTerminal).toBe(true);
  });

  it("starts a new logical turn when a strong user identity follows legacy state without a client id", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-old-terminal-before-new-user",
          seq: 12,
          type: "turn.completed",
          status: "completed",
          turn_id: "run-shared",
          created_at: "2026-05-29T08:00:30Z",
          payload: { final_task_status: "completed" },
        }),
      ]),
      {
        status: "completed",
        run_id: "run-shared",
        outcome_status: "delivered",
        delivery_status: "verified",
      },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "user-new",
            role: "user",
            createdAt: "2026-05-29T08:01:00Z",
            metadata: { client_turn_id: "client-turn-new" },
            cards: [],
          },
        ],
      },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-shared",
      client_turn_id: "client-turn-new",
      outcome_status: "unknown",
      delivery_status: "pending",
    });
    expect(patch.acceptedTerminal).toBe(false);
  });

  it("keeps legacy message-id client fallbacks compatible with untagged terminals", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-legacy-untagged-terminal",
          seq: 12,
          type: "turn.completed",
          status: "completed",
          turn_id: "run-legacy",
          created_at: "2026-05-29T08:01:30Z",
          payload: { final_task_status: "completed" },
        }),
      ]),
      { status: "running", run_id: "run-legacy" },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "persisted-user-row",
            role: "user",
            createdAt: "2026-05-29T08:01:00Z",
            metadata: { client_turn_id: "persisted-user-row" },
            cards: [],
          },
        ],
      },
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState.status).toBe("completed");
    expect(patch.acceptedTerminal).toBe(true);
  });

  it("promotes canonical runtime identity over a hydrated message-id fallback", () => {
    const identityMessages = [
      {
        id: "persisted-user-row",
        role: "user",
        createdAt: "2026-05-29T08:01:00Z",
        metadata: { client_turn_id: "persisted-user-row" },
        cards: [],
      },
    ];
    const progress = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-canonical-progress",
          seq: 11,
          created_at: "2026-05-29T08:01:10Z",
          type: "message.delta",
          status: "running",
          turn_id: "run-legacy",
          payload: { client_turn_id: "canonical-client" },
        }),
      ]),
      { status: "running", run_id: "run-legacy" },
      undefined,
      undefined,
      { identityMessages },
    );

    expect(progress.runtimeState.client_turn_id).toBe("canonical-client");

    const terminal = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-canonical-terminal",
          seq: 12,
          created_at: "2026-05-29T08:01:30Z",
          type: "turn.completed",
          status: "completed",
          turn_id: "run-legacy",
          payload: {
            client_turn_id: "canonical-client",
            final_task_status: "completed",
          },
        }),
      ]),
      progress.runtimeState,
      undefined,
      undefined,
      { identityMessages },
    );

    expect(terminal.generating).toBe(false);
    expect(terminal.runtimeState).toMatchObject({
      status: "completed",
      client_turn_id: "canonical-client",
    });
    expect(terminal.acceptedTerminal).toBe(true);
  });

  it("accepts an explicit canonical terminal on a cold legacy state", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-cold-canonical-terminal",
          type: "turn.completed",
          status: "completed",
          turn_id: "run-legacy",
          payload: {
            client_turn_id: "canonical-client",
            final_task_status: "completed",
          },
        }),
      ]),
      { status: "running", run_id: "run-legacy" },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "persisted-user-row",
            role: "user",
            metadata: { client_turn_id: "persisted-user-row" },
            cards: [],
          },
        ],
      },
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState.client_turn_id).toBe("canonical-client");
    expect(patch.acceptedTerminal).toBe(true);
  });

  it("keeps a cold terminal batch within one client-turn cohort", () => {
    const patch = buildRuntimeEventSessionPatch({
      ...response([
        event({
          id: "evt-client-a-completed",
          seq: 10,
          created_at: "2026-09-05T00:01:00Z",
          type: "turn.completed",
          status: "completed",
          turn_id: "transport-run",
          payload: {
            client_turn_id: "client-a",
            final_task_status: "completed",
            outcome_status: "delivered",
            delivery_status: "verified",
          },
        }),
        event({
          id: "evt-client-b-failed",
          seq: 11,
          created_at: "2026-09-05T00:02:00Z",
          type: "turn.failed",
          status: "failed",
          turn_id: "transport-run",
          payload: {
            client_turn_id: "client-b",
            final_task_status: "failed",
            outcome_status: "blocked",
            delivery_status: "not_delivered",
          },
        }),
      ]),
      resync_required: true,
    });

    expect(patch.acceptedTerminal).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "failed",
      run_id: "transport-run",
      client_turn_id: "client-b",
      outcome_status: "blocked",
      delivery_status: "not_delivered",
    });
    expect(patch.runtimeState.events_preview?.map((item) => item.id)).toEqual([
      "evt-client-b-failed",
    ]);
  });

  it("does not let a terminal select its own stale client identity", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-new-client-start",
          seq: 20,
          type: "turn.started",
          status: "running",
          turn_id: "run-shared",
          payload: { client_turn_id: "client-turn-new" },
        }),
        event({
          id: "evt-old-client-failed",
          seq: 21,
          type: "turn.failed",
          status: "failed",
          turn_id: "run-shared",
          payload: {
            client_turn_id: "client-turn-old",
            final_task_status: "failed",
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-shared",
        client_turn_id: "client-turn-old",
      },
    );

    expect(patch.generating).toBe(true);
    expect(patch.runtimeState).toMatchObject({
      status: "running",
      run_id: "run-shared",
      client_turn_id: "client-turn-new",
    });
    expect(patch.acceptedTerminal).toBe(false);
  });

  it("accepts an untagged terminal when preceding scoped activity proves the current client", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-current-untagged-terminal",
          seq: 12,
          type: "turn.completed",
          status: "completed",
          turn_id: "run-shared",
          created_at: "2026-05-29T08:01:30Z",
          payload: {
            final_task_status: "completed",
            outcome_status: "delivered",
            delivery_status: "verified",
          },
        }),
      ]),
      {
        status: "running",
        run_id: "run-shared",
        client_turn_id: "client-turn-current",
        events_preview: [
          runtimeEventToPreviewEvent(
            event({
              id: "evt-current-progress",
              seq: 11,
              type: "message.delta",
              status: "running",
              turn_id: "run-shared",
              created_at: "2026-05-29T08:01:20Z",
              payload: { client_turn_id: "client-turn-current" },
            }),
          ),
        ],
      },
      undefined,
      undefined,
      {
        identityMessages: [
          {
            id: "user-current",
            role: "user",
            createdAt: "2026-05-29T08:01:00Z",
            metadata: { client_turn_id: "client-turn-current" },
            cards: [],
          },
        ],
      },
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState).toMatchObject({
      status: "completed",
      client_turn_id: "client-turn-current",
      outcome_status: "delivered",
      delivery_status: "verified",
    });
    expect(patch.acceptedTerminal).toBe(true);
  });

  it("attaches a late post-answer Ququ without reopening or replacing the turn", () => {
    const clientTurnId = "client-turn-late-ququ";
    const existingMessages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-main-answer",
          seq: 2,
          type: "message.completed",
          status: "completed",
          turn_id: "run-main",
          item_id: "answer-main",
          payload: {
            client_turn_id: clientTurnId,
            source_message_id: "answer-main",
            content_type: "text",
            stream_message_type: "message",
            message: {
              id: "answer-main",
              role: "assistant",
              metadata: { client_turn_id: clientTurnId },
              content: [{ type: "text", text: "正文已经完整结束。" }],
            },
          },
        }),
      ],
      [],
      { strictTurnIdentity: true },
    );
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-late-ququ",
          seq: 4,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "run-main:post-answer:job",
          item_id: "ququ-job",
          payload: {
            client_turn_id: clientTurnId,
            answer_message_id: "answer-main",
            source_message_id: "ququ-job",
            content_type: "text",
            stream_message_type: "message",
            details: {
              delivery: "late_bound_sidecar",
              original_run_id: "run-main",
              job_id: "job",
            },
            message: {
              id: "ququ-job",
              role: "assistant",
              metadata: { client_turn_id: clientTurnId },
              content: [
                { type: "text", text: "~>[echo] 这句是后来才漏出来的。" },
              ],
            },
          },
        }),
      ]),
      {
        status: "completed",
        run_id: "run-main",
        outcome_status: "delivered",
        delivery_status: "verified",
      },
      undefined,
      existingMessages,
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState.status).toBe("completed");
    expect(patch.runtimeState.run_id).toBe("run-main");
    expect(patch.messages).toHaveLength(1);
    const responseCard = patch.messages?.[0]?.cards?.[0]?.data as any;
    expect(
      responseCard.output.map((item: any) => item.content?.[0]?.text),
    ).toEqual(["正文已经完整结束。", "~>[echo] 这句是后来才漏出来的。"]);
  });

  it("keeps the foreground run identity when terminal and late Ququ replay together", () => {
    const clientTurnId = "client-turn-batched-ququ";
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-main-terminal",
          seq: 10,
          type: "turn.completed",
          status: "completed",
          source: "system",
          turn_id: "run-main",
          payload: { final_task_status: "finished" },
        }),
        event({
          id: "evt-batched-late-ququ",
          seq: 11,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "run-main:post-answer:job",
          item_id: "ququ-batched-job",
          payload: {
            client_turn_id: clientTurnId,
            answer_message_id: "answer-main",
            source_message_id: "ququ-batched-job",
            details: {
              delivery: "late_bound_sidecar",
              original_run_id: "run-main",
              job_id: "job",
            },
            message: {
              id: "ququ-batched-job",
              role: "assistant",
              metadata: { client_turn_id: clientTurnId },
              content: [{ type: "text", text: "~>[echo] 回放也不能串走。" }],
            },
          },
        }),
      ]),
      undefined,
      undefined,
      [],
      { mirrorNativeMessages: true, strictTurnIdentity: true },
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState.status).toBe("completed");
    expect(patch.runtimeState.run_id).toBe("run-main");
  });

  it("recovers the original run identity from a lone late Ququ", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-lone-late-ququ",
          seq: 12,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "run-main:post-answer:job",
          payload: {
            client_turn_id: "client-turn-lone-ququ",
            details: {
              delivery: "late_bound_sidecar",
              original_run_id: "run-main",
            },
            message: {
              id: "ququ-lone-job",
              role: "assistant",
              content: [{ type: "text", text: "~>[echo] 还在原来的那一轮。" }],
            },
          },
        }),
      ]),
    );

    expect(patch.runtimeState.run_id).toBe("run-main");
  });

  it("does not regress a terminal run to running on same-run late non-terminal events", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-late-tool",
          seq: 12,
          type: "tool.completed",
          status: "completed",
          turn_id: "run-1",
          payload: {
            legacy_kind: "chat.tool_call.finished",
            trace_title: "迟到工具结果",
          },
        }),
      ]),
      {
        status: "completed",
        run_id: "run-1",
        summary: "本轮结束",
        events_preview: [
          {
            id: "evt-final",
            seq: 11,
            type: "turn.completed",
            kind: "chat.turn.settled",
            status: "completed",
            label: "本轮结束",
          } as any,
        ],
      },
      null,
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState.status).toBe("completed");
    expect(patch.runtimeState.can_stop).toBe(false);
    expect(patch.runtimeState.can_reconnect).toBe(false);
  });

  it("keeps runtime error and stop request events non-terminal until settled", () => {
    expect(
      buildRuntimeEventSessionPatch(
        response([
          event({
            id: "evt-runtime-error",
            type: "runtime.error",
            status: "failed",
            payload: {
              legacy_kind: "chat.error",
              trace_title: "运行错误",
            },
          }),
        ]),
      ),
    ).toMatchObject({
      generating: true,
      runtimeState: { status: "running" },
    });

    expect(
      buildRuntimeEventSessionPatch(
        response([
          event({
            id: "evt-stop-requested",
            type: "runtime.warning",
            status: "interrupted",
            payload: {
              legacy_kind: "chat.turn.stop_requested",
              trace_title: "请求停止",
            },
          }),
        ]),
      ),
    ).toMatchObject({
      generating: true,
      runtimeState: { status: "running" },
    });

    expect(
      buildRuntimeEventSessionPatch(
        response([
          event({
            id: "evt-runtime-error",
            seq: 3,
            type: "runtime.error",
            status: "failed",
            payload: {
              legacy_kind: "chat.error",
              trace_title: "运行错误",
            },
          }),
          event({
            id: "evt-failed-settled",
            seq: 4,
            type: "turn.failed",
            status: "failed",
            payload: {
              legacy_kind: "chat.turn.settled",
              final_task_status: "failed",
            },
          }),
        ]),
      ).runtimeState.status,
    ).toBe("failed");

    expect(
      buildRuntimeEventSessionPatch(
        response([
          event({
            id: "evt-stop-requested",
            seq: 5,
            type: "runtime.warning",
            status: "interrupted",
            payload: {
              legacy_kind: "chat.turn.stop_requested",
              trace_title: "请求停止",
            },
          }),
          event({
            id: "evt-interrupted-settled",
            seq: 6,
            type: "turn.interrupted",
            status: "interrupted",
            payload: {
              legacy_kind: "chat.turn.settled",
              final_task_status: "interrupted",
            },
          }),
        ]),
      ).runtimeState.status,
    ).toBe("interrupted");
  });

  it("settles a stopped packaged turn when the client id only appears on the start contract", () => {
    const identityMessages = [
      {
        id: "user-packaged-stop",
        role: "user",
        createdAt: "2026-09-15T10:39:12.990Z",
        metadata: {
          client_turn_id: "user-packaged-stop",
          client_turn_id_origin: "optimistic_submission",
        },
        cards: [],
      },
    ];
    const progress = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-stop-turn-started",
          seq: 2,
          type: "turn.started",
          status: "running",
          turn_id: "run-packaged-stop",
          created_at: "2026-09-15T10:39:13.000Z",
          payload: {
            legacy_kind: "chat.turn.started",
            task_contract: { client_turn_id: "user-packaged-stop" },
          },
        }),
        event({
          id: "evt-stop-message-started",
          seq: 4,
          type: "message.started",
          status: "running",
          turn_id: "run-packaged-stop",
          created_at: "2026-09-15T10:39:13.100Z",
          payload: {
            client_turn_id: "user-packaged-stop",
            message: { id: "message-packaged-stop" },
          },
        }),
        event({
          id: "evt-stop-message-delta",
          seq: 5,
          type: "message.delta",
          status: "running",
          turn_id: "run-packaged-stop",
          created_at: "2026-09-15T10:39:13.200Z",
          payload: {
            client_turn_id: "user-packaged-stop",
            delta: "working",
            message: { id: "message-packaged-stop" },
          },
        }),
      ]),
      undefined,
      undefined,
      undefined,
      { identityMessages },
    );

    const settled = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-stop-requested",
          seq: 6,
          type: "runtime.warning",
          status: "interrupted",
          turn_id: null,
          created_at: "2026-09-15T10:39:15.000Z",
          payload: { legacy_kind: "chat.turn.stop_requested" },
        }),
        event({
          id: "evt-stop-runtime-error",
          seq: 7,
          type: "runtime.error",
          status: "failed",
          turn_id: "run-packaged-stop",
          created_at: "2026-09-15T10:39:15.100Z",
          payload: { legacy_kind: "chat.error" },
        }),
        event({
          id: "evt-stop-settled",
          seq: 8,
          type: "turn.failed",
          status: "failed",
          turn_id: "run-packaged-stop",
          created_at: "2026-09-15T10:39:15.200Z",
          payload: {
            legacy_kind: "chat.turn.settled",
            final_task_status: "failed",
          },
        }),
      ]),
      progress.runtimeState,
      progress.turnEventPreview,
      undefined,
      { identityMessages },
    );

    expect(settled.generating).toBe(false);
    expect(settled.acceptedTerminal).toBe(true);
    expect(settled.runtimeState).toMatchObject({
      status: "failed",
      run_id: "run-packaged-stop",
      client_turn_id: "user-packaged-stop",
      can_stop: false,
    });
  });

  it("maps backend stalled events to stalled runtime state", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-stalled",
          type: "turn.stalled",
          status: "stalled",
          payload: {
            legacy_kind: "chat.system.event",
            trace_title: "本轮可能卡住",
          },
        }),
      ]),
    );

    expect(patch.generating).toBe(false);
    expect(patch.runtimeState).toMatchObject({
      status: "stalled",
      can_stop: false,
      can_reconnect: false,
    });
  });

  it("reads latest preview sequence from runtime state or legacy preview", () => {
    expect(
      latestPreviewSeq(
        { status: "running", events_preview: [{ id: "a", seq: 7 } as any] },
        { events: [{ id: "b", seq: 5 } as any] },
      ),
    ).toBe(7);
  });

  it("advances reconciliation from the durable stream sequence, not header preview", () => {
    expect(
      latestPreviewSeq(
        {
          status: "running",
          stream_latest_seq: 29,
          stream_cursor: "epoch-a:29",
          events_preview: [{ id: "header-only", seq: 4 } as any],
        },
        { events: [{ id: "legacy-header", seq: 5 } as any] },
      ),
    ).toBe(29);
  });

  it("ignores an older terminal when a newer turn starts in the same batch", () => {
    expect(
      runtimeResponseHasTerminalEvent(
        response([
          event({
            id: "old-terminal",
            seq: 1,
            type: "turn.completed",
            turn_id: "run-old",
          }),
          event({
            id: "new-start",
            seq: 2,
            type: "turn.started",
            status: "running",
            turn_id: "run-new",
          }),
        ]),
      ),
    ).toBe(false);
  });

  it("keeps native message deltas out of the header preview", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-delta",
          seq: 8,
          type: "message.delta",
          status: "running",
          source: "assistant",
          item_id: "msg-1",
          payload: {
            legacy_kind: "chat.message.delta",
            delta: "你",
            content_type: "text",
            message: {
              id: "msg-1",
              role: "assistant",
              content: [{ type: "text", text: "你" }],
            },
          },
        }),
      ]),
      { status: "running", events_preview: [{ id: "old", seq: 4 } as any] },
      null,
    );

    expect(
      isNativeMessageStreamEvent(
        event({ id: "evt-tool", type: "tool.completed" }),
      ),
    ).toBe(false);
    expect(patch.turnEventPreview.events.map((item) => item.id)).toEqual([
      "old",
    ]);
  });

  it("builds one native response card for answer and thinking streams", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-thinking",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-thinking-answer",
        item_id: "thinking-1",
        payload: {
          legacy_kind: "chat.message.delta",
          delta: "想",
          content_type: "thinking",
          stream_message_type: "reasoning",
          message: {
            id: "thinking-1",
            role: "assistant",
            content: [{ type: "thinking", text: "想" }],
          },
        },
      }),
      event({
        id: "evt-answer-a",
        seq: 2,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-thinking-answer",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.delta",
          delta: "你",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "你" }],
          },
        },
      }),
      event({
        id: "evt-answer-b",
        seq: 3,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-thinking-answer",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.delta",
          delta: "好",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "好" }],
          },
        },
      }),
      event({
        id: "evt-answer-final",
        seq: 4,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        turn_id: "run-thinking-answer",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.completed",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "你好" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(messages).toHaveLength(1);
    expect(responseCard.output).toHaveLength(2);
    expect(responseCard.output[0].type).toBe("reasoning");
    expect(responseCard.output[0].status).toBe("completed");
    expect(responseCard.output[0].content[0].status).toBe("completed");
    expect(responseCard.output[0].content[0].text).toBe("想");
    expect(responseCard.output[1].type).toBe("message");
    expect(responseCard.output[1].status).toBe("completed");
    expect(responseCard.output[1].content[0].text).toBe("你好");
  });

  it("reconciles runtime frames into the direct SSE card without duplicating the turn or text", () => {
    const directMessages = [
      {
        id: "assistant-direct",
        role: "assistant",
        msgStatus: "loading",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              id: "response-direct",
              object: "response",
              status: "in_progress",
              output: [
                {
                  id: "msg-1:reasoning",
                  object: "message",
                  status: "in_progress",
                  type: "reasoning",
                  role: "assistant",
                  content: [
                    {
                      object: "content",
                      status: "in_progress",
                      type: "text",
                      index: 0,
                      text: "正在分析",
                    },
                  ],
                  metadata: { client_turn_id: "client-turn-1" },
                },
              ],
              metadata: {
                client_turn_id: "client-turn-1",
                run_id: "transport-run-1",
              },
            },
          },
        ],
      },
    ] as any;
    const runtimeDelta = event({
      id: "evt-runtime-reasoning",
      seq: 2,
      type: "message.delta",
      status: "running",
      source: "assistant",
      turn_id: null,
      item_id: "msg-1:reasoning",
      payload: {
        client_turn_id: "client-turn-1",
        turn_id: "transport-run-1",
        run_id: "transport-run-1",
        delta: "分析",
        content_type: "thinking",
        stream_message_type: "reasoning",
        message: {
          id: "msg-1:reasoning",
          role: "assistant",
          content: [{ type: "thinking", text: "正在分析" }],
          metadata: { client_turn_id: "client-turn-1" },
        },
      },
    });

    const first = buildNativeRuntimeStreamMessages(
      [runtimeDelta],
      directMessages,
      { strictTurnIdentity: true },
    );
    const replayed = buildNativeRuntimeStreamMessages([runtimeDelta], first, {
      strictTurnIdentity: true,
    });
    const responseCard = replayed[0]?.cards?.[0]?.data as any;

    expect(replayed).toHaveLength(1);
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].id).toBe("msg-1:reasoning");
    expect(responseCard.output[0].content[0].text).toBe("正在分析");
    expect(responseCard.output[0].metadata.runtime_event_ids).toEqual([
      "evt-runtime-reasoning",
    ]);
    expect(responseCard.metadata).toMatchObject({
      client_turn_id: "client-turn-1",
      native_runtime_turn_id: "client-turn-1",
      run_id: "transport-run-1",
    });
  });

  it("does not create a transport-run placeholder beside a client-turn transcript", () => {
    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-start",
          seq: 1,
          type: "turn.started",
          status: "running",
          turn_id: "transport-run-1",
        }),
        event({
          id: "evt-answer",
          seq: 2,
          type: "message.delta",
          status: "running",
          source: "assistant",
          turn_id: null,
          item_id: "msg-1:message",
          payload: {
            client_turn_id: "client-turn-1",
            turn_id: "transport-run-1",
            delta: "答",
            content_type: "text",
            stream_message_type: "message",
            message: {
              id: "msg-1:message",
              role: "assistant",
              content: [{ type: "text", text: "答" }],
              metadata: { client_turn_id: "client-turn-1" },
            },
          },
        }),
      ],
      [],
      { strictTurnIdentity: true },
    );

    expect(messages).toHaveLength(1);
    expect((messages[0].cards?.[0]?.data as any).metadata).toMatchObject({
      client_turn_id: "client-turn-1",
      run_id: "transport-run-1",
    });
  });

  it("updates automatic context compaction in place across its lifecycle", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-compaction-started",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-compaction",
        item_id: "context-compaction-1",
        payload: {
          message: {
            id: "context-compaction-1",
            role: "assistant",
            metadata: {
              source: "legacy",
              metadata: {
                system_action: {
                  kind: "context_compaction",
                  status: "running",
                  title: "正在自动压缩上下文",
                },
              },
            },
            content: [{ type: "text", text: "正在整理较早的对话。" }],
          },
        },
      }),
      event({
        id: "evt-compaction-completed",
        seq: 2,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        turn_id: "run-compaction",
        item_id: "context-compaction-1",
        payload: {
          message: {
            id: "context-compaction-1",
            role: "assistant",
            metadata: {
              source: "legacy",
              metadata: {
                system_action: {
                  kind: "context_compaction",
                  status: "completed",
                  title: "上下文已自动压缩",
                  detail: "较早内容已转为摘要，后续回答可能省略部分原始细节。",
                },
              },
            },
            content: [{ type: "text", text: "上下文已整理。" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(messages).toHaveLength(1);
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].status).toBe("completed");
    expect(responseCard.output[0].content[0].text).toBe("上下文已整理。");
    expect(
      responseCard.output[0].metadata.metadata.system_action,
    ).toMatchObject({
      kind: "context_compaction",
      status: "completed",
      title: "上下文已自动压缩",
    });
  });

  it("closes thinking before the first streaming answer token for the same turn", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-thinking",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-1",
        item_id: "msg-1",
        payload: {
          content_type: "thinking",
          stream_message_type: "reasoning",
          delta: "先想",
          message: {
            id: "msg-1",
            role: "assistant",
            content: [{ type: "thinking", text: "先想" }],
          },
        },
      }),
      event({
        id: "evt-answer",
        seq: 2,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-1",
        item_id: "msg-1",
        payload: {
          content_type: "text",
          stream_message_type: "message",
          delta: "再答",
          message: {
            id: "msg-1",
            role: "assistant",
            content: [{ type: "text", text: "再答" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.status).toBe("in_progress");
    expect(responseCard.output[0]).toMatchObject({
      type: "reasoning",
      status: "completed",
      content: [{ status: "completed", text: "先想" }],
    });
    expect(responseCard.output[1]).toMatchObject({
      type: "message",
      status: "in_progress",
      content: [{ text: "再答" }],
    });
  });

  it("treats cumulative native delta payloads as snapshots", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-answer-a",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.delta",
          delta: "你",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "你" }],
          },
        },
      }),
      event({
        id: "evt-answer-b",
        seq: 2,
        type: "message.delta",
        status: "running",
        source: "assistant",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.delta",
          delta: "你好",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "你好" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output[0].content[0].text).toBe("你好");
  });

  it("appends incremental native delta payloads that only carry the current fragment", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-answer-a",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.delta",
          delta: "你",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "你" }],
          },
        },
      }),
      event({
        id: "evt-answer-b",
        seq: 2,
        type: "message.delta",
        status: "running",
        source: "assistant",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.delta",
          delta: "好",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "好" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output[0].content[0].text).toBe("你好");
  });

  it("discards an incomplete streamed fragment before output-limit recovery", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-truncated-delta",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-output-recovery",
        item_id: "msg-truncated:message",
        payload: {
          delta: "文档",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "msg-truncated:message",
            role: "assistant",
            content: [{ type: "text", text: "文档" }],
          },
        },
      }),
      event({
        id: "evt-output-recovery",
        seq: 2,
        type: "runtime.warning",
        status: "info",
        source: "system",
        turn_id: "run-output-recovery",
        payload: {
          kind: "model_output_recovery",
          details: {
            discard_message_ids: ["msg-truncated:message"],
          },
        },
      }),
      event({
        id: "evt-recovered-answer",
        seq: 3,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        turn_id: "run-output-recovery",
        item_id: "msg-recovered:message",
        payload: {
          message: {
            id: "msg-recovered:message",
            role: "assistant",
            content: [{ type: "text", text: "完整回答已经生成。" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].content[0].text).toBe("完整回答已经生成。");
    expect(responseCard.output[0].metadata.native_runtime_source_id).toBe(
      "msg-recovered:message",
    );
  });

  it("appends legacy native delta fragments even when older rows lack payload.delta", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-answer-a",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.delta",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "你" }],
          },
        },
      }),
      event({
        id: "evt-answer-b",
        seq: 2,
        type: "message.delta",
        status: "running",
        source: "assistant",
        item_id: "answer-1",
        payload: {
          legacy_kind: "chat.message.delta",
          content_type: "text",
          stream_message_type: "message",
          message: {
            id: "answer-1",
            role: "assistant",
            content: [{ type: "text", text: "好" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output[0].content[0].text).toBe("你好");
  });

  it("claims the direct SSE output when the ledger uses another provider item id", () => {
    const existingMessages = [
      {
        id: "assistant-direct",
        role: "assistant",
        msgStatus: "loading",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              id: "response-direct",
              object: "response",
              status: "in_progress",
              metadata: { client_turn_id: "client-turn-1" },
              output: [
                {
                  id: "direct-provider-answer",
                  object: "message",
                  type: "message",
                  role: "assistant",
                  status: "in_progress",
                  metadata: {
                    source_message_id: "logical-provider-answer",
                  },
                  content: [
                    {
                      object: "content",
                      type: "text",
                      status: "in_progress",
                      text: "PACKAGED_MODEL_REPLY: complete",
                    },
                  ],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-ledger-prefix",
          seq: 1,
          type: "message.delta",
          status: "running",
          source: "assistant",
          turn_id: "transport-run-1",
          item_id: "ledger-provider-answer",
          payload: {
            client_turn_id: "client-turn-1",
            source_message_id: "logical-provider-answer",
            content_type: "text",
            stream_message_type: "message",
            delta: "PACKAGED_MODEL_REPLY",
            message: {
              id: "ledger-provider-answer",
              role: "assistant",
              metadata: {
                client_turn_id: "client-turn-1",
                source_message_id: "logical-provider-answer",
              },
              content: [{ type: "text", text: "PACKAGED_MODEL_REPLY" }],
            },
          },
        }),
        event({
          id: "evt-ledger-completed",
          seq: 2,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "transport-run-1",
          item_id: "ledger-provider-answer",
          payload: {
            client_turn_id: "client-turn-1",
            source_message_id: "logical-provider-answer",
            content_type: "text",
            stream_message_type: "message",
            text: "PACKAGED_MODEL_REPLY: complete",
            message: {
              id: "ledger-provider-answer",
              role: "assistant",
              metadata: {
                client_turn_id: "client-turn-1",
                source_message_id: "logical-provider-answer",
              },
              content: [
                { type: "text", text: "PACKAGED_MODEL_REPLY: complete" },
              ],
            },
          },
        }),
      ],
      existingMessages,
      { strictTurnIdentity: true },
    );

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(messages).toHaveLength(1);
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].content[0].text).toBe(
      "PACKAGED_MODEL_REPLY: complete",
    );
  });

  it("does not let a shorter completed ledger snapshot truncate visible stream text", () => {
    const fullAnswer =
      "PACKAGED_MODEL_REPLY: PACKAGED_PROVIDER_STREAM_UI_PROMPT_SWITCH_f57e52c3";
    const shorterSnapshot = fullAnswer.slice(0, -1);
    const existingMessages = [
      {
        id: "assistant-direct",
        role: "assistant",
        msgStatus: "loading",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              id: "response-direct",
              object: "response",
              status: "in_progress",
              metadata: { client_turn_id: "client-turn-1" },
              output: [
                {
                  id: "direct-provider-answer",
                  object: "message",
                  type: "message",
                  role: "assistant",
                  status: "in_progress",
                  metadata: {
                    source_message_id: "logical-provider-answer",
                  },
                  content: [{ type: "text", text: fullAnswer }],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-ledger-short-completed",
          seq: 1,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "transport-run-1",
          item_id: "ledger-provider-answer",
          payload: {
            client_turn_id: "client-turn-1",
            source_message_id: "logical-provider-answer",
            content_type: "text",
            stream_message_type: "message",
            text: shorterSnapshot,
            message: {
              id: "ledger-provider-answer",
              role: "assistant",
              metadata: {
                client_turn_id: "client-turn-1",
                source_message_id: "logical-provider-answer",
              },
              content: [{ type: "text", text: shorterSnapshot }],
            },
          },
        }),
      ],
      existingMessages,
      { strictTurnIdentity: true },
    );

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].status).toBe("completed");
    expect(responseCard.output[0].content[0].text).toBe(fullAnswer);
  });

  it("does not append a replayed tail after a direct stream was reconciled", () => {
    const fullAnswer = "PACKAGED_MODEL_REPLY: answer2";
    const existingMessages = [
      {
        id: "assistant-direct-tail",
        role: "assistant",
        msgStatus: "loading",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              id: "response-direct-tail",
              object: "response",
              status: "in_progress",
              metadata: { client_turn_id: "client-tail" },
              output: [
                {
                  id: "direct-tail-answer",
                  object: "message",
                  type: "message",
                  role: "assistant",
                  status: "in_progress",
                  metadata: { source_message_id: "logical-tail-answer" },
                  content: [{ type: "text", text: fullAnswer }],
                },
              ],
            },
          },
        ],
      },
    ] as any;
    const deltaEvent = (
      id: string,
      seq: number,
      delta: string,
      cumulative: string,
    ) =>
      event({
        id,
        seq,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "transport-tail",
        item_id: "ledger-tail-answer",
        payload: {
          client_turn_id: "client-tail",
          source_message_id: "logical-tail-answer",
          content_type: "text",
          stream_message_type: "message",
          delta,
          message: {
            id: "ledger-tail-answer",
            role: "assistant",
            metadata: {
              client_turn_id: "client-tail",
              source_message_id: "logical-tail-answer",
            },
            content: [{ type: "text", text: cumulative }],
          },
        },
      });

    const reconciled = buildNativeRuntimeStreamMessages(
      [
        deltaEvent(
          "evt-ledger-prefix",
          1,
          "PACKAGED_MODEL_REPLY",
          "PACKAGED_MODEL_REPLY",
        ),
        deltaEvent("evt-ledger-tail", 2, "2", "2"),
      ],
      existingMessages,
      { strictTurnIdentity: true },
    );
    const responseCard = reconciled[0]?.cards?.[0]?.data as any;

    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].content[0].text).toBe(fullAnswer);
  });

  it("reconciles live and durable public progress as one reasoning block", () => {
    const progressId = "public-progress:run-1:1";
    const progressText = "正在检索和核对公开资料。";
    const existingMessages = [
      {
        id: "assistant-live-progress",
        role: "assistant",
        msgStatus: "loading",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              id: "response-live-progress",
              object: "response",
              status: "in_progress",
              metadata: { client_turn_id: "client-progress" },
              output: [
                {
                  id: "live-progress",
                  object: "message",
                  type: "reasoning",
                  role: "assistant",
                  status: "in_progress",
                  metadata: { source_message_id: progressId },
                  content: [{ type: "thinking", text: progressText }],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-durable-progress",
          seq: 1,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "run-1",
          item_id: "provider-message",
          payload: {
            client_turn_id: "client-progress",
            source_message_id: "provider-message",
            content_type: "thinking",
            stream_message_type: "reasoning",
            public_progress: {
              schema_version: 2,
              id: progressId,
              revision: 1,
              source: "runtime_facts",
              text_origin: "assistant_public_text",
              tool_call_ids: ["call-progress"],
            },
            message: {
              id: "provider-message",
              role: "assistant",
              content: [{ type: "thinking", text: progressText }],
            },
          },
        }),
      ],
      existingMessages,
      { strictTurnIdentity: true },
    );

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].type).toBe("reasoning");
    expect(responseCard.output[0].status).toBe("completed");
    expect(responseCard.output[0].content[0].text).toBe(progressText);
    expect(responseCard.output[0].metadata).toMatchObject({
      run_id: "run-1",
      client_turn_id: "client-progress",
      public_projection: "runtime_progress_v2",
      public_progress_id: progressId,
      public_progress_revision: 1,
      source: "runtime_facts",
      private_reasoning_exposed: false,
      text_origin: "assistant_public_text",
      tool_call_ids: ["call-progress"],
    });
    expect(responseCard.output[0].content[0].metadata).toMatchObject({
      public_projection: "runtime_progress_v2",
      public_progress_id: progressId,
      source: "runtime_facts",
      private_reasoning_exposed: false,
      text_origin: "assistant_public_text",
      tool_call_ids: ["call-progress"],
    });
  });

  it.each([
    {
      label: "the wrong schema version",
      progress: {
        schema_version: 1,
        id: "not-public-schema",
        revision: 1,
        source: "runtime_facts",
      },
    },
    {
      label: "a non-runtime source",
      progress: {
        schema_version: 2,
        id: "not-public-source",
        revision: 1,
        source: "provider",
        text_origin: "assistant_public_text",
      },
    },
    {
      label: "a private-thinking origin",
      progress: {
        schema_version: 2,
        id: "not-public-origin",
        revision: 1,
        source: "runtime_facts",
        text_origin: "provider_thinking",
      },
    },
    {
      label: "a zero revision",
      progress: {
        schema_version: 2,
        id: "not-public-zero-revision",
        revision: 0,
        source: "runtime_facts",
        text_origin: "assistant_public_text",
      },
    },
    {
      label: "a string revision",
      progress: {
        schema_version: 2,
        id: "not-public-string-revision",
        revision: "1",
        source: "runtime_facts",
        text_origin: "assistant_public_text",
      },
    },
    {
      label: "a numeric id",
      progress: {
        schema_version: 2,
        id: 42,
        revision: 1,
        source: "runtime_facts",
        text_origin: "assistant_public_text",
      },
    },
    {
      label: "a boolean id",
      progress: {
        schema_version: 2,
        id: true,
        revision: 1,
        source: "runtime_facts",
        text_origin: "assistant_public_text",
      },
    },
    {
      label: "an object id",
      progress: {
        schema_version: 2,
        id: { forged: true },
        revision: 1,
        source: "runtime_facts",
        text_origin: "assistant_public_text",
      },
    },
    {
      label: "an array id",
      progress: {
        schema_version: 2,
        id: ["forged"],
        revision: 1,
        source: "runtime_facts",
        text_origin: "assistant_public_text",
      },
    },
    {
      label: "a padded source",
      progress: {
        schema_version: 2,
        id: "not-public-padded-source",
        revision: 1,
        source: " runtime_facts ",
        text_origin: "assistant_public_text",
      },
    },
    {
      label: "a padded origin",
      progress: {
        schema_version: 2,
        id: "not-public-padded-origin",
        revision: 1,
        source: "runtime_facts",
        text_origin: "assistant_public_text ",
      },
    },
    {
      label: "an empty tool-call identity list",
      progress: {
        schema_version: 2,
        id: "not-public-empty-tool-ids",
        revision: 1,
        source: "runtime_facts",
        text_origin: "assistant_public_text",
        tool_call_ids: [],
      },
    },
    {
      label: "a malformed tool-call identity",
      progress: {
        schema_version: 2,
        id: "not-public-malformed-tool-id",
        revision: 1,
        source: "runtime_facts",
        text_origin: "assistant_public_text",
        tool_call_ids: [""],
      },
    },
  ])("does not promote reasoning marked with $label", ({ progress }) => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: `evt-${progress.id}`,
        type: "message.completed",
        status: "completed",
        source: "assistant",
        turn_id: "run-untrusted-progress",
        item_id: "provider-untrusted-progress",
        payload: {
          content_type: "thinking",
          stream_message_type: "reasoning",
          public_progress: progress,
          message: {
            id: "provider-untrusted-progress",
            role: "assistant",
            content: [{ type: "thinking", text: "不应升级的提供商推理" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].metadata).not.toHaveProperty(
      "public_projection",
    );
    expect(responseCard.output[0].content[0].metadata).toBeUndefined();
  });

  it("strips forged flat progress metadata while reconciling a low-latency reasoning block", () => {
    const forged = {
      public_projection: "runtime_progress_v2",
      public_progress_id: "forged-progress",
      public_progress_revision: 7,
      source: "runtime_facts",
      private_reasoning_exposed: false,
      tool_call_ids: ["forged-call"],
    };
    const existingMessages = [
      {
        id: "assistant-live-forged",
        role: "assistant",
        msgStatus: "loading",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              status: "in_progress",
              output: [
                {
                  id: "provider-forged",
                  type: "reasoning",
                  status: "in_progress",
                  metadata: {
                    source_message_id: "provider-forged",
                    ...forged,
                  },
                  content: [{ type: "thinking", text: "private reasoning" }],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-forged-flat-progress",
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "run-forged-progress",
          item_id: "provider-forged",
          payload: {
            source_message_id: "provider-forged",
            content_type: "thinking",
            stream_message_type: "reasoning",
            message: {
              id: "provider-forged",
              role: "assistant",
              metadata: forged,
              content: [{ type: "thinking", text: "private reasoning" }],
            },
          },
        }),
      ],
      existingMessages,
      { strictTurnIdentity: true },
    );

    const output = (messages[0]?.cards?.[0]?.data as any).output[0];
    expect(output.content[0].text).toBe("private reasoning");
    expect(output.metadata).not.toHaveProperty("public_projection");
    expect(output.metadata).not.toHaveProperty("public_progress_id");
    expect(output.metadata).not.toHaveProperty("private_reasoning_exposed");
    expect(output.metadata).not.toHaveProperty("tool_call_ids");
    expect(output.content[0].metadata).toBeUndefined();
  });

  it("reconciles live and durable provider reasoning as one block", () => {
    const sourceId = "provider-reasoning-1";
    const reasoningText = "先核对原文，再确认发布日期。";
    const existingMessages = [
      {
        id: "assistant-live-reasoning",
        role: "assistant",
        msgStatus: "loading",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              id: "response-live-reasoning",
              object: "response",
              status: "in_progress",
              metadata: { client_turn_id: "client-reasoning" },
              output: [
                {
                  id: "live-reasoning",
                  object: "message",
                  type: "reasoning",
                  role: "assistant",
                  status: "in_progress",
                  metadata: { source_message_id: sourceId },
                  content: [{ type: "thinking", text: reasoningText }],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-durable-reasoning",
          seq: 1,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "run-reasoning",
          item_id: sourceId,
          payload: {
            client_turn_id: "client-reasoning",
            source_message_id: sourceId,
            content_type: "thinking",
            stream_message_type: "reasoning",
            message: {
              id: sourceId,
              role: "assistant",
              metadata: { source_message_id: sourceId },
              content: [{ type: "thinking", text: reasoningText }],
            },
          },
        }),
      ],
      existingMessages,
      { strictTurnIdentity: true },
    );

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].type).toBe("reasoning");
    expect(responseCard.output[0].status).toBe("completed");
    expect(responseCard.output[0].content[0].text).toBe(reasoningText);
    expect(responseCard.output[0].metadata).not.toHaveProperty(
      "public_projection",
    );
    expect(responseCard.output[0].metadata).not.toHaveProperty(
      "public_progress_id",
    );
    expect(responseCard.output[0].content[0]).not.toHaveProperty("metadata");
  });

  it("does not append a one-character ledger prefix after a completed answer with progress rows", () => {
    const progress = "我再补一轮检索。";
    const fullAnswer = "查清楚了。这里是完整答案。";
    const existingMessages = [
      {
        id: "assistant-direct",
        role: "assistant",
        msgStatus: "finished",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              id: "response-direct",
              object: "response",
              status: "completed",
              metadata: { client_turn_id: "client-turn-1" },
              output: [
                {
                  id: "msg-progress",
                  object: "message",
                  type: "message",
                  role: "assistant",
                  status: "completed",
                  content: [{ type: "text", text: progress }],
                },
                {
                  id: "msg-final",
                  object: "message",
                  type: "message",
                  role: "assistant",
                  status: "completed",
                  content: [{ type: "text", text: fullAnswer }],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-ledger-prefix",
          seq: 53,
          type: "message.delta",
          status: "running",
          source: "assistant",
          turn_id: "transport-run-1",
          item_id: "msg-final:message",
          payload: {
            client_turn_id: "client-turn-1",
            content_type: "text",
            stream_message_type: "message",
            delta: "查",
            message: {
              id: "msg-final:message",
              role: "assistant",
              metadata: { client_turn_id: "client-turn-1" },
              content: [{ type: "text", text: "查" }],
            },
          },
        }),
      ],
      existingMessages,
      { strictTurnIdentity: true },
    );

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(messages).toHaveLength(1);
    expect(responseCard.output).toHaveLength(2);
    expect(
      responseCard.output.map((output: any) => output.content?.[0]?.text),
    ).toEqual([progress, fullAnswer]);
  });

  it("keeps two distinct durable assistant outputs in the same client turn", () => {
    const clientTurnId = "client-turn-distinct-outputs";
    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-progress-completed",
          seq: 1,
          type: "message.completed",
          status: "completed",
          turn_id: "transport-run-distinct",
          item_id: "msg-progress-distinct",
          payload: {
            client_turn_id: clientTurnId,
            content_type: "text",
            stream_message_type: "message",
            message: {
              id: "msg-progress-distinct",
              role: "assistant",
              metadata: { client_turn_id: clientTurnId },
              content: [{ type: "text", text: "我已经找到第一批线索。" }],
            },
          },
        }),
        event({
          id: "evt-answer-completed",
          seq: 2,
          type: "message.completed",
          status: "completed",
          turn_id: "transport-run-distinct",
          item_id: "msg-answer-distinct",
          payload: {
            client_turn_id: clientTurnId,
            content_type: "text",
            stream_message_type: "message",
            message: {
              id: "msg-answer-distinct",
              role: "assistant",
              metadata: { client_turn_id: clientTurnId },
              content: [{ type: "text", text: "完整结论如下。" }],
            },
          },
        }),
      ],
      [],
      { strictTurnIdentity: true },
    );

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(messages).toHaveLength(1);
    expect(responseCard.output).toHaveLength(2);
    expect(
      responseCard.output.map((output: any) => output.content?.[0]?.text),
    ).toEqual(["我已经找到第一批线索。", "完整结论如下。"]);
  });

  it("does not erase two distinct durable outputs just because their text matches", () => {
    const clientTurnId = "client-turn-repeated-legitimate-text";
    const completed = (id: string, seq: number) =>
      event({
        id: `evt-${id}`,
        seq,
        type: "message.completed",
        status: "completed",
        turn_id: "transport-run-repeated-text",
        item_id: id,
        payload: {
          client_turn_id: clientTurnId,
          content_type: "text",
          stream_message_type: "message",
          message: {
            id,
            role: "assistant",
            metadata: { client_turn_id: clientTurnId },
            content: [{ type: "text", text: "我在。" }],
          },
        },
      });

    const messages = buildNativeRuntimeStreamMessages(
      [completed("msg-first-repeated", 1), completed("msg-second-repeated", 2)],
      [],
      { strictTurnIdentity: true },
    );
    const responseCard = messages[0]?.cards?.[0]?.data as any;

    expect(responseCard.output).toHaveLength(2);
    expect(responseCard.output.map((item: any) => item.id)).not.toEqual([
      responseCard.output[0].id,
      responseCard.output[0].id,
    ]);
  });

  it("collapses an exact runtime mirror duplicate without swallowing a distinct Ququ row", () => {
    const answer = "行，那就没事。想说了随时喊我。";
    const existingMessages = [
      {
        id: "assistant-direct",
        role: "assistant",
        msgStatus: "loading",
        cards: [
          {
            code: LANTAI_RESPONSE_CARD_CODE,
            data: {
              id: "response-direct",
              object: "response",
              status: "in_progress",
              metadata: { client_turn_id: "client-turn-1" },
              output: [
                {
                  id: "direct-answer",
                  object: "message",
                  type: "message",
                  role: "assistant",
                  status: "completed",
                  metadata: {
                    source_message_id: "logical-answer-1",
                  },
                  content: [{ type: "text", text: answer }],
                },
                {
                  id: "ledger-answer-alias",
                  object: "message",
                  type: "message",
                  role: "assistant",
                  status: "completed",
                  metadata: {
                    source_message_id: "logical-answer-1",
                  },
                  content: [{ type: "text", text: answer }],
                },
                {
                  id: "ququ-answer",
                  object: "message",
                  type: "message",
                  role: "assistant",
                  status: "completed",
                  content: [
                    {
                      type: "text",
                      text: "~>[echo] 我刚才确实停了一下。",
                    },
                  ],
                },
              ],
            },
          },
        ],
      },
    ] as any;

    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-ledger-completed",
          seq: 2,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          turn_id: "transport-run-1",
          item_id: "ledger-answer-alias",
          payload: {
            client_turn_id: "client-turn-1",
            source_message_id: "logical-answer-1",
            content_type: "text",
            stream_message_type: "message",
            text: answer,
            message: {
              id: "ledger-answer-alias",
              role: "assistant",
              metadata: {
                client_turn_id: "client-turn-1",
                source_message_id: "logical-answer-1",
              },
              content: [{ type: "text", text: answer }],
            },
          },
        }),
      ],
      existingMessages,
      { strictTurnIdentity: true },
    );

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(2);
    expect(
      responseCard.output.filter(
        (output: any) => output.content?.[0]?.text === answer,
      ),
    ).toHaveLength(1);
    expect(responseCard.output[1].content[0].text).toBe(
      "~>[echo] 我刚才确实停了一下。",
    );
  });

  it("keeps thinking and answer separate when native stream reuses a message id", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-thinking",
          seq: 1,
          type: "message.delta",
          status: "running",
          source: "assistant",
          item_id: "msg-1",
          payload: {
            delta: "想",
            content_type: "thinking",
            message: {
              id: "msg-1",
              role: "assistant",
              content: [{ type: "thinking", text: "想" }],
            },
          },
        }),
        event({
          id: "evt-answer",
          seq: 2,
          type: "message.delta",
          status: "running",
          source: "assistant",
          item_id: "msg-1",
          payload: {
            delta: "答",
            content_type: "text",
            message: {
              id: "msg-1",
              role: "assistant",
              content: [{ type: "text", text: "答" }],
            },
          },
        }),
        event({
          id: "evt-answer-final",
          seq: 3,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          item_id: "msg-1",
          payload: {
            content_type: "text",
            message: {
              id: "msg-1",
              role: "assistant",
              content: [{ type: "text", text: "答完" }],
            },
          },
        }),
        event({
          id: "evt-turn-completed",
          seq: 4,
          type: "turn.completed",
          status: "completed",
          source: "system",
          payload: {
            legacy_kind: "chat.turn.settled",
            trace_title: "本轮结束",
          },
        }),
      ]),
      null,
      null,
      [],
    );

    const responseCard = patch.messages?.[0]?.cards?.[0]?.data as any;
    expect(patch.generating).toBe(false);
    expect(responseCard.status).toBe("completed");
    expect(responseCard.output).toHaveLength(2);
    expect(responseCard.output[0].type).toBe("reasoning");
    expect(responseCard.output[0].status).toBe("completed");
    expect(responseCard.output[0].content[0].text).toBe("想");
    expect(responseCard.output[1].type).toBe("message");
    expect(responseCard.output[1].status).toBe("completed");
    expect(responseCard.output[1].content[0].text).toBe("答完");
  });

  it("keeps distinct public progress revisions when a provider message id is reused", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-progress-0",
        seq: 1,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-progress",
        item_id: "provider-message",
        payload: {
          delta: "正在梳理任务目标。",
          content_type: "thinking",
          stream_message_type: "reasoning",
          public_progress: {
            schema_version: 2,
            id: "public-progress:run-progress:1",
            revision: 1,
            source: "runtime_facts",
            text_origin: "assistant_public_text",
          },
          message: {
            id: "provider-message",
            role: "assistant",
            content: [{ type: "thinking", text: "正在梳理任务目标。" }],
          },
        },
      }),
      event({
        id: "evt-progress-1",
        seq: 3,
        type: "message.delta",
        status: "running",
        source: "assistant",
        turn_id: "run-progress",
        item_id: "provider-message",
        payload: {
          delta: "已完成 1 次检索，正在整理结论。",
          content_type: "thinking",
          stream_message_type: "reasoning",
          public_progress: {
            schema_version: 2,
            id: "public-progress:run-progress:2",
            revision: 2,
            source: "runtime_facts",
            text_origin: "assistant_public_text",
          },
          message: {
            id: "provider-message",
            role: "assistant",
            content: [
              {
                type: "thinking",
                text: "已完成 1 次检索，正在整理结论。",
              },
            ],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(2);
    expect(
      responseCard.output.map((item: any) => item.content[0].text),
    ).toEqual(["正在梳理任务目标。", "已完成 1 次检索，正在整理结论。"]);
    expect(responseCard.output.map((item: any) => item.metadata)).toEqual([
      expect.objectContaining({
        public_projection: "runtime_progress_v2",
        public_progress_id: "public-progress:run-progress:1",
        public_progress_revision: 1,
        source: "runtime_facts",
        private_reasoning_exposed: false,
        text_origin: "assistant_public_text",
      }),
      expect.objectContaining({
        public_projection: "runtime_progress_v2",
        public_progress_id: "public-progress:run-progress:2",
        public_progress_revision: 2,
        source: "runtime_facts",
        private_reasoning_exposed: false,
        text_origin: "assistant_public_text",
      }),
    ]);
  });

  it("preserves completed model reasoning status on the visible answer", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-answer-final",
        type: "message.completed",
        status: "completed",
        source: "assistant",
        item_id: "msg-1",
        payload: {
          message: {
            id: "msg-1",
            role: "assistant",
            content: [{ type: "text", text: "直接回答" }],
            metadata: {
              reasoning_status: {
                requested: true,
                returned: false,
              },
            },
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output[0].metadata.reasoning_status).toEqual({
      requested: true,
      returned: false,
    });
  });

  it("keeps final mixed thinking and answer blocks in their own stream outputs", () => {
    const patch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-thinking-delta",
          seq: 1,
          type: "message.delta",
          status: "running",
          source: "assistant",
          item_id: "msg-1",
          payload: {
            delta: "想",
            message: {
              id: "msg-1",
              role: "assistant",
              content: [{ type: "thinking", text: "想" }],
            },
          },
        }),
        event({
          id: "evt-answer-delta",
          seq: 2,
          type: "message.delta",
          status: "running",
          source: "assistant",
          item_id: "msg-1",
          payload: {
            delta: "答",
            message: {
              id: "msg-1",
              role: "assistant",
              content: [{ type: "text", text: "答" }],
            },
          },
        }),
        event({
          id: "evt-final-mixed",
          seq: 3,
          type: "message.completed",
          status: "completed",
          source: "assistant",
          item_id: "msg-1",
          payload: {
            message: {
              id: "msg-1",
              role: "assistant",
              content: [
                { type: "thinking", text: "想完" },
                { type: "text", text: "答完" },
              ],
            },
          },
        }),
        event({
          id: "evt-turn-completed",
          seq: 4,
          type: "turn.completed",
          status: "completed",
          source: "system",
          payload: {
            legacy_kind: "chat.turn.settled",
            trace_title: "本轮结束",
          },
        }),
      ]),
      null,
      null,
      [],
    );

    const responseCard = patch.messages?.[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(2);
    expect(responseCard.output[0].type).toBe("reasoning");
    expect(responseCard.output[0].content[0].text).toBe("想完");
    expect(responseCard.output[1].type).toBe("message");
    expect(responseCard.output[1].content[0].text).toBe("答完");
  });

  it("does not merge provider item ids across turns", () => {
    const firstPatch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-first-delta",
          seq: 1,
          type: "message.delta",
          status: "running",
          source: "assistant",
          turn_id: "turn-1",
          item_id: "msg-reused",
          payload: {
            delta: "first",
            message: {
              id: "msg-reused",
              role: "assistant",
              content: [{ type: "text", text: "first" }],
            },
          },
        }),
        event({
          id: "evt-first-done",
          seq: 2,
          type: "turn.completed",
          status: "completed",
          source: "system",
          turn_id: "turn-1",
          payload: { legacy_kind: "chat.turn.settled" },
        }),
      ]),
      null,
      null,
      [],
    );
    const secondPatch = buildRuntimeEventSessionPatch(
      response([
        event({
          id: "evt-second-delta",
          seq: 3,
          type: "message.delta",
          status: "running",
          source: "assistant",
          turn_id: "turn-2",
          item_id: "msg-reused",
          payload: {
            delta: "second",
            message: {
              id: "msg-reused",
              role: "assistant",
              content: [{ type: "text", text: "second" }],
            },
          },
        }),
      ]),
      null,
      null,
      firstPatch.messages,
    );

    const cards = secondPatch.messages?.map(
      (message) => message.cards?.[0]?.data as any,
    );
    expect(cards).toHaveLength(2);
    expect(cards?.[0].output[0].content[0].text).toBe("first");
    expect(cards?.[1].output[0].content[0].text).toBe("second");
  });

  it("upgrades an untyped message start to reasoning when thinking arrives", () => {
    const messages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-start",
        seq: 1,
        type: "message.started",
        status: "running",
        source: "assistant",
        item_id: "thinking-1",
        payload: {
          message_id: "thinking-1",
        },
      }),
      event({
        id: "evt-thinking",
        seq: 2,
        type: "message.delta",
        status: "running",
        source: "assistant",
        item_id: "thinking-1",
        payload: {
          delta: "先想",
          message: {
            id: "thinking-1",
            role: "assistant",
            content: [{ type: "thinking", text: "先想" }],
          },
        },
      }),
    ]);

    const responseCard = messages[0]?.cards?.[0]?.data as any;
    expect(responseCard.output).toHaveLength(1);
    expect(responseCard.output[0].type).toBe("reasoning");
    expect(responseCard.output[0].content[0].text).toBe("先想");
  });

  it("does not merge a new canonical turn into a stale in-progress assistant card", () => {
    const staleMessages = buildNativeRuntimeStreamMessages([
      event({
        id: "evt-turn-one-delta",
        seq: 1,
        type: "message.delta",
        status: "running",
        turn_id: "turn-one",
        item_id: "answer-one",
        payload: {
          delta: "first answer",
          message: {
            id: "answer-one",
            role: "assistant",
            content: [{ type: "text", text: "first answer" }],
          },
        },
      }),
    ]);

    const messages = buildNativeRuntimeStreamMessages(
      [
        event({
          id: "evt-turn-two-delta",
          seq: 2,
          type: "message.delta",
          status: "running",
          turn_id: "turn-two",
          item_id: "answer-two",
          payload: {
            delta: "second answer",
            message: {
              id: "answer-two",
              role: "assistant",
              content: [{ type: "text", text: "second answer" }],
            },
          },
        }),
      ],
      staleMessages,
      { strictTurnIdentity: true },
    );

    expect(messages).toHaveLength(2);
    expect(
      (messages[0].cards?.[0]?.data as any).output[0].content[0].text,
    ).toBe("first answer");
    expect(
      (messages[1].cards?.[0]?.data as any).output[0].content[0].text,
    ).toBe("second answer");
  });
});
