import type {
  ILantaiRuntimeMessage,
  ILantaiRuntimeResponse,
} from "./lantaiChatTypes";
import { LantaiRuntimeMessageType } from "./lantaiChatTypes";

const CALL_TO_OUTPUT_TYPE = new Map<string, string>([
  [
    LantaiRuntimeMessageType.PLUGIN_CALL,
    LantaiRuntimeMessageType.PLUGIN_CALL_OUTPUT,
  ],
  [LantaiRuntimeMessageType.MCP_CALL, LantaiRuntimeMessageType.MCP_CALL_OUTPUT],
]);

const OUTPUT_TO_CALL_TYPE = new Map<string, string>(
  Array.from(CALL_TO_OUTPUT_TYPE.entries(), ([callType, outputType]) => [
    outputType,
    callType,
  ]),
);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function nonEmptyString(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function contentRecords(
  message: ILantaiRuntimeMessage,
): Record<string, unknown>[] {
  if (!Array.isArray(message.content)) return [];
  return message.content.filter(isRecord);
}

function contentDataRecords(
  message: ILantaiRuntimeMessage,
): Record<string, unknown>[] {
  return contentRecords(message).flatMap((item) =>
    isRecord(item.data) ? [item.data] : [],
  );
}

function firstString(
  records: Record<string, unknown>[],
  keys: string[],
): string {
  for (const record of records) {
    for (const key of keys) {
      const value = nonEmptyString(record[key]);
      if (value) return value;
    }
  }
  return "";
}

function toolCallIdentity(message: ILantaiRuntimeMessage): string {
  const metadata = isRecord(message.metadata) ? message.metadata : {};
  const contentData = contentDataRecords(message);
  const content = contentRecords(message);
  return (
    firstString(
      [...contentData, ...content, message, metadata],
      ["call_id", "tool_call_id", "toolCallId", "callId"],
    ) || firstString([...contentData, ...content], ["id"])
  );
}

function toolName(message: ILantaiRuntimeMessage): string {
  return firstString(
    [message, ...contentDataRecords(message), ...contentRecords(message)],
    ["name", "tool_name", "toolName"],
  );
}

function hasToolError(message: ILantaiRuntimeMessage): boolean {
  if (
    firstString(
      [message, ...contentDataRecords(message)],
      ["error", "error_message"],
    )
  ) {
    return true;
  }
  const status = nonEmptyString(message.status).toLowerCase();
  return /(fail|error|cancel|reject|interrupt|stall)/.test(status);
}

function mergedToolStatus(
  call: ILantaiRuntimeMessage,
  output: ILantaiRuntimeMessage,
): string {
  if (hasToolError(call) || hasToolError(output)) return "failed";
  const outputStatus = nonEmptyString(output.status).toLowerCase();
  if (/(progress|running|queued|created)/.test(outputStatus)) {
    return outputStatus === "created" ? "completed" : "in_progress";
  }
  return "completed";
}

function mergeToolPair(
  call: ILantaiRuntimeMessage,
  output: ILantaiRuntimeMessage,
): ILantaiRuntimeMessage {
  const callContent = Array.isArray(call.content) ? call.content : [];
  const outputContent = Array.isArray(output.content) ? output.content : [];
  const callMetadata = isRecord(call.metadata) ? call.metadata : {};
  const outputMetadata = isRecord(output.metadata) ? output.metadata : {};

  return {
    ...call,
    status: mergedToolStatus(call, output),
    content: [...callContent, ...outputContent],
    metadata: {
      ...callMetadata,
      ...outputMetadata,
      tool_output_message_id: output.id,
    },
  };
}

function normalizeStandaloneToolOutput(
  output: ILantaiRuntimeMessage,
): ILantaiRuntimeMessage {
  const originalContent = Array.isArray(output.content) ? output.content : [];
  const firstContent = originalContent.find(isRecord) || {};
  const firstData = isRecord(firstContent.data) ? firstContent.data : {};
  const {
    output: _output,
    error: _error,
    result: _result,
    ...callData
  } = firstData;

  return {
    ...output,
    type: OUTPUT_TO_CALL_TYPE.get(output.type) || output.type,
    status: hasToolError(output) ? "failed" : "completed",
    content: [
      {
        ...firstContent,
        type:
          output.type === LantaiRuntimeMessageType.MCP_CALL_OUTPUT
            ? LantaiRuntimeMessageType.MCP_CALL
            : "function_call",
        data: callData,
      },
      ...originalContent,
    ],
  };
}

function findPreferredUnusedIndex(
  candidates: number[],
  callIndex: number,
  usedOutputs: Set<number>,
): number | undefined {
  return (
    candidates.find(
      (candidate) => candidate > callIndex && !usedOutputs.has(candidate),
    ) ?? candidates.find((candidate) => !usedOutputs.has(candidate))
  );
}

function mergeToolMessages(
  output: ILantaiRuntimeMessage[] | undefined,
): ILantaiRuntimeMessage[] {
  if (!Array.isArray(output) || output.length === 0) return [];

  const outputsByIdentity = new Map<string, number[]>();
  output.forEach((message, index) => {
    if (!OUTPUT_TO_CALL_TYPE.has(message.type)) return;
    const identity = toolCallIdentity(message);
    if (!identity) return;
    const key = `${message.type}\u0000${identity}`;
    const indices = outputsByIdentity.get(key) || [];
    indices.push(index);
    outputsByIdentity.set(key, indices);
  });

  const pairByCall = new Map<number, number>();
  const usedOutputs = new Set<number>();

  output.forEach((message, callIndex) => {
    const expectedOutputType = CALL_TO_OUTPUT_TYPE.get(message.type);
    if (!expectedOutputType) return;

    const identity = toolCallIdentity(message);
    let resultIndex: number | undefined;
    if (identity) {
      resultIndex = findPreferredUnusedIndex(
        outputsByIdentity.get(`${expectedOutputType}\u0000${identity}`) || [],
        callIndex,
        usedOutputs,
      );
    } else {
      const name = toolName(message);
      if (name) {
        const candidates = output.flatMap((candidate, candidateIndex) => {
          if (
            candidate.type !== expectedOutputType ||
            usedOutputs.has(candidateIndex) ||
            toolCallIdentity(candidate) ||
            toolName(candidate) !== name
          ) {
            return [];
          }
          return [candidateIndex];
        });
        resultIndex = findPreferredUnusedIndex(
          candidates,
          callIndex,
          usedOutputs,
        );
      }
    }

    if (resultIndex === undefined) return;
    pairByCall.set(callIndex, resultIndex);
    usedOutputs.add(resultIndex);
  });

  return output.flatMap((message, index) => {
    const resultIndex = pairByCall.get(index);
    if (resultIndex !== undefined) {
      return [mergeToolPair(message, output[resultIndex])];
    }
    if (usedOutputs.has(index)) return [];
    if (OUTPUT_TO_CALL_TYPE.has(message.type)) {
      return [normalizeStandaloneToolOutput(message)];
    }
    return [message];
  });
}

function maybeGenerating(data: ILantaiRuntimeResponse): boolean {
  return (
    data?.status === "in_progress" ||
    data?.status === "running" ||
    data?.status === "created"
  );
}

export default {
  mergeToolMessages,
  maybeGenerating,
};
