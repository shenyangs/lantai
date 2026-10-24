export const LantaiRuntimeContentType = {
  TEXT: "text",
  REFUSAL: "refusal",
  IMAGE: "image",
  VIDEO: "video",
  FILE: "file",
  AUDIO: "audio",
} as const;

export const LantaiRuntimeMessageType = {
  MESSAGE: "message",
  PLUGIN_CALL: "plugin_call",
  PLUGIN_CALL_OUTPUT: "plugin_call_output",
  MCP_CALL: "mcp_call",
  MCP_CALL_OUTPUT: "mcp_call_output",
  MCP_APPROVAL_REQUEST: "mcp_approval_request",
  REASONING: "reasoning",
  ERROR: "error",
  HEARTBEAT: "heartbeat",
} as const;

export const LantaiRuntimeRunStatus = {
  InProgress: "in_progress",
  Completed: "completed",
  Created: "created",
  Failed: "failed",
} as const;

export interface ITextContent {
  type: string;
  text: string;
  status?: string;
  [key: string]: any;
}

export interface ILantaiRuntimeMessage {
  id: string;
  object?: string;
  status?: string;
  type: string;
  role?: string;
  content?: any[];
  metadata?: Record<string, any>;
  [key: string]: any;
}

export interface ILantaiRuntimeResponse {
  id?: string;
  object?: string;
  status?: string;
  output?: ILantaiRuntimeMessage[];
  error?: Record<string, any> | null;
  usage?: Record<string, any> | null;
  metadata?: Record<string, any>;
  [key: string]: any;
}
