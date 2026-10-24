export const CREATE_NEW_CHAT_REQUEST_EVENT = "lantai:create-new-chat";
export const CREATE_NEW_CHAT_SETTLED_EVENT = "lantai:create-new-chat-settled";
export const CREATE_NEW_CHAT_PENDING_KEY = "lantai:create-new-chat";
export const CREATE_NEW_CHAT_CONTEXT_KEY = "lantai:create-new-chat-context";

export type ChatKnowledgeSourceReference = {
  path: string;
  filename: string;
  kind?: string;
};

export type ChatKnowledgeScopeMode = "exact" | "expanded";

export type CreateNewChatSettledDetail = {
  ok: boolean;
};
