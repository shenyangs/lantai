import React from "react";
import { Card, Typography } from "antd";
import { useChatAnywhereOptions } from "./lantaiChat";

function toolName(data: Record<string, unknown>): string {
  const content = Array.isArray(data.content) ? data.content : [];
  for (const item of content) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const nested = record.data;
    if (nested && typeof nested === "object") {
      const nestedRecord = nested as Record<string, unknown>;
      if (typeof nestedRecord.name === "string") return nestedRecord.name;
      if (typeof nestedRecord.tool_name === "string") return nestedRecord.tool_name;
    }
    if (typeof record.name === "string") return record.name;
    if (typeof record.tool_name === "string") return record.tool_name;
  }
  return String(data.name || data.tool_name || data.type || "tool");
}

export default function LantaiResponseTool({
  data,
  isApproval,
}: {
  data: Record<string, unknown>;
  isApproval?: boolean;
}) {
  const customToolRenderConfig = useChatAnywhereOptions(
    (options) => options.customToolRenderConfig || {},
  );
  const name = toolName(data);
  const Renderer = customToolRenderConfig[name];
  if (Renderer) {
    return React.createElement(Renderer, { data, isApproval });
  }
  const cardProps: React.ComponentProps<typeof Card> & {
    "data-chat-tool-call": string;
  } = {
    size: "small",
    className: "lantai-tool-card lantai-process-summary",
    "data-chat-tool-call": "1",
  };
  return React.createElement(
    Card,
    cardProps,
    React.createElement(Typography.Text, { strong: true }, name),
    React.createElement(
      "pre",
      { style: { whiteSpace: "pre-wrap", margin: "8px 0 0" } },
      JSON.stringify(data, null, 2),
    ),
  );
}
