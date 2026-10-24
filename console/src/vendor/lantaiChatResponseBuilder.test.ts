import { describe, expect, it } from "vitest";

import LantaiRuntimeResponseBuilder from "./lantaiChatResponseBuilder";
import {
  LantaiRuntimeMessageType,
  type ILantaiRuntimeMessage,
} from "./lantaiChatTypes";

function toolMessage(
  overrides: Partial<ILantaiRuntimeMessage>,
): ILantaiRuntimeMessage {
  return {
    id: "message-1",
    type: LantaiRuntimeMessageType.PLUGIN_CALL,
    status: "in_progress",
    content: [],
    ...overrides,
  };
}

describe("lantaiChatResponseBuilder.mergeToolMessages", () => {
  it("merges a plugin call and its matching output into one lifecycle item", () => {
    const merged = LantaiRuntimeResponseBuilder.mergeToolMessages([
      toolMessage({
        id: "call-message",
        type: LantaiRuntimeMessageType.PLUGIN_CALL,
        content: [
          {
            type: "function_call",
            data: {
              call_id: "call-write-1",
              name: "write_file",
              arguments: JSON.stringify({ path: "index.html" }),
            },
          },
        ],
      }),
      toolMessage({
        id: "output-message",
        type: LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT,
        status: "completed",
        content: [
          {
            type: "function_call_output",
            data: {
              call_id: "call-write-1",
              name: "write_file",
              output: "Wrote index.html",
            },
          },
        ],
      }),
    ]);

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      id: "call-message",
      type: LantaiRuntimeMessageType.PLUGIN_CALL,
      status: "completed",
    });
    expect(merged[0].content).toHaveLength(2);
    expect(merged[0].content?.[0]?.data).toMatchObject({
      call_id: "call-write-1",
      name: "write_file",
    });
    expect(merged[0].content?.[1]?.data).toMatchObject({
      call_id: "call-write-1",
      output: "Wrote index.html",
    });
  });

  it("does not merge concurrent calls that have different call ids", () => {
    const merged = LantaiRuntimeResponseBuilder.mergeToolMessages([
      toolMessage({
        id: "call-a",
        content: [
          {
            data: {
              call_id: "call-a",
              name: "write_file",
              arguments: "{}",
            },
          },
        ],
      }),
      toolMessage({
        id: "call-b",
        content: [
          {
            data: {
              call_id: "call-b",
              name: "write_file",
              arguments: "{}",
            },
          },
        ],
      }),
      toolMessage({
        id: "output-b",
        type: LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT,
        status: "completed",
        content: [
          {
            data: {
              call_id: "call-b",
              name: "write_file",
              output: "B",
            },
          },
        ],
      }),
      toolMessage({
        id: "output-a",
        type: LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT,
        status: "completed",
        content: [
          {
            data: {
              call_id: "call-a",
              name: "write_file",
              output: "A",
            },
          },
        ],
      }),
    ]);

    expect(merged).toHaveLength(2);
    expect(merged[0].id).toBe("call-a");
    expect(merged[0].content?.[1]?.data?.output).toBe("A");
    expect(merged[1].id).toBe("call-b");
    expect(merged[1].content?.[1]?.data?.output).toBe("B");
  });

  it("keeps an unmatched running call visible", () => {
    const call = toolMessage({
      id: "call-running",
      type: LantaiRuntimeMessageType.MCP_CALL,
      status: "in_progress",
      content: [
        {
          data: {
            call_id: "mcp-1",
            server_label: "filesystem",
            name: "read_file",
            arguments: { path: "README.md" },
          },
        },
      ],
    });

    expect(LantaiRuntimeResponseBuilder.mergeToolMessages([call])).toEqual([
      call,
    ]);
  });

  it("turns a standalone output into a completed inspectable tool item", () => {
    const merged = LantaiRuntimeResponseBuilder.mergeToolMessages([
      toolMessage({
        id: "output-only",
        type: LantaiRuntimeMessageType.MCP_CALL_OUTPUT,
        status: "completed",
        content: [
          {
            data: {
              call_id: "mcp-orphan",
              server_label: "browser",
              name: "open_page",
              output: { url: "https://example.com" },
            },
          },
        ],
      }),
    ]);

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      id: "output-only",
      type: LantaiRuntimeMessageType.MCP_CALL,
      status: "completed",
    });
    expect(merged[0].content?.[0]?.data).toMatchObject({
      call_id: "mcp-orphan",
      name: "open_page",
    });
    expect(merged[0].content?.[1]?.data?.output).toEqual({
      url: "https://example.com",
    });
  });

  it("propagates a tool error to the merged lifecycle status", () => {
    const merged = LantaiRuntimeResponseBuilder.mergeToolMessages([
      toolMessage({
        id: "call-failed",
        content: [
          {
            data: {
              call_id: "call-failed",
              name: "read_file",
              arguments: { path: "missing.md" },
            },
          },
        ],
      }),
      toolMessage({
        id: "output-failed",
        type: LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT,
        status: "failed",
        content: [
          {
            data: {
              call_id: "call-failed",
              name: "read_file",
              error: "file not found",
            },
          },
        ],
      }),
    ]);

    expect(merged).toHaveLength(1);
    expect(merged[0].status).toBe("failed");
    expect(merged[0].content?.[1]?.data?.error).toBe("file not found");
  });
});
