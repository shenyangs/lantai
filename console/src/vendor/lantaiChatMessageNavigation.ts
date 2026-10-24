export interface ChatMessageNavigationTarget {
  chatId?: string;
  messageId: string;
  source: "search" | "external";
}

export interface PendingChatMessageNavigation extends ChatMessageNavigationTarget {
  requestId: string;
  requestedAt: number;
  expiresAt: number;
}

type NavigationListener = (request: PendingChatMessageNavigation) => void;

const REQUEST_TTL_MS = 30_000;
let requestCounter = 0;
let pendingRequest: PendingChatMessageNavigation | null = null;
const listeners = new Set<NavigationListener>();

export function requestChatMessageNavigation(
  target: ChatMessageNavigationTarget,
): PendingChatMessageNavigation {
  const requestedAt = Date.now();
  const request: PendingChatMessageNavigation = {
    ...target,
    messageId: String(target.messageId || "").trim(),
    requestId: `chat-message-navigation-${requestedAt}-${++requestCounter}`,
    requestedAt,
    expiresAt: requestedAt + REQUEST_TTL_MS,
  };
  pendingRequest = request.messageId ? request : null;
  if (pendingRequest) {
    listeners.forEach((listener) => listener(pendingRequest!));
  }
  return request;
}

export function peekPendingChatMessageNavigation(): PendingChatMessageNavigation | null {
  if (pendingRequest && pendingRequest.expiresAt <= Date.now()) {
    pendingRequest = null;
  }
  return pendingRequest;
}

export function consumePendingChatMessageNavigation(
  requestId: string,
): boolean {
  if (!pendingRequest || pendingRequest.requestId !== requestId) return false;
  pendingRequest = null;
  return true;
}

export function clearPendingChatMessageNavigation(): void {
  pendingRequest = null;
}

export function subscribeChatMessageNavigation(
  listener: NavigationListener,
): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
