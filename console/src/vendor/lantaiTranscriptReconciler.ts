import type { ILantaiChatMessage } from "./lantaiChat";
import type { ILantaiRuntimeResponse } from "./lantaiChatTypes";

export interface TranscriptReconcilerPrimitives {
  responseData: (message: ILantaiChatMessage) => ILantaiRuntimeResponse | null;
  responseTurnKey: (response: ILantaiRuntimeResponse) => string;
  responseClientTurnKey?: (response: ILantaiRuntimeResponse) => string;
  hasEquivalentStreamedAnswer: (
    current: ILantaiRuntimeResponse,
    incoming: ILantaiRuntimeResponse,
  ) => boolean;
  shouldMergeAssistantResponses: (
    current: ILantaiChatMessage,
    incoming: ILantaiChatMessage,
  ) => boolean;
  mergeAssistantResponses: (
    current: ILantaiChatMessage,
    incoming: ILantaiChatMessage,
    orderOwner: "previous" | "incoming",
  ) => ILantaiChatMessage;
  normalizeAdjacentAssistantResponses: (
    messages: ILantaiChatMessage[],
  ) => ILantaiChatMessage[];
  isActiveStatus: (status: unknown) => boolean;
  isTerminalStatus: (status: unknown) => boolean;
}

export interface TranscriptReconciler {
  choose: (
    current: ILantaiChatMessage[],
    incoming?: ILantaiChatMessage[],
  ) => ILantaiChatMessage[];
  reconcileProjection: (
    current: ILantaiChatMessage[],
    incoming: ILantaiChatMessage[] | undefined,
    visibleMessagesBelongToActiveSession: boolean,
  ) => ILantaiChatMessage[];
  incomingContainsEveryVisibleUser: (
    current: ILantaiChatMessage[],
    incoming: ILantaiChatMessage[],
  ) => boolean;
  hasTrailingEmptyOptimisticAssistant: (
    messages: ILantaiChatMessage[],
  ) => boolean;
  incomingHasTerminalReplyAfterLastUser: (
    messages: ILantaiChatMessage[],
  ) => boolean;
  reconcileRouteHydration: (
    input: TranscriptRouteHydrationInput,
  ) => TranscriptRouteHydrationResult;
}

export interface TranscriptRouteHydrationInput {
  activeSessionMessages: ILantaiChatMessage[];
  localSessionMessages: ILantaiChatMessage[];
  incomingMessages: ILantaiChatMessage[];
  localMatchesCurrentSession: boolean;
  durableSessionGenerating: boolean;
  primaryStreamActive: boolean;
  runtimeWasActive: boolean;
}

export interface TranscriptRouteHydrationResult {
  messages: ILantaiChatMessage[];
  preservingExistingMessages: boolean;
  terminalAuthority: boolean;
}

export function durableTranscriptHydrationIsRequired({
  messages,
  primaryStreamActive,
  primaryWasActiveAtTerminal,
}: {
  messages: ILantaiChatMessage[];
  primaryStreamActive: boolean;
  primaryWasActiveAtTerminal: boolean;
}): boolean {
  const latestUserIndex = messages.reduce(
    (latest, message, index) => (message.role === "user" ? index : latest),
    -1,
  );
  const hasCanonicalTerminalReply = messages
    .slice(latestUserIndex + 1)
    .some((message) => {
      if (message.role !== "assistant") return false;
      return (message.cards || []).some((card) => {
        const metadata = card?.data?.metadata;
        return (
          metadata &&
          typeof metadata === "object" &&
          metadata.canonical_terminal_reply === true
        );
      });
    });
  if (hasCanonicalTerminalReply) return false;
  if (primaryStreamActive && !primaryWasActiveAtTerminal) return false;
  return true;
}

/**
 * Describes user-supplied content only for preserving the richer attachment
 * representation of an already identity-matched turn. It must never decide
 * whether two submissions are the same turn.
 */
export function userMessageInputSignature(message: ILantaiChatMessage): string {
  if (message.role !== "user") return "";
  const parts: string[] = [];
  const canonicalAssetName = (
    record: Record<string, unknown>,
    sourceUrl: string,
  ): string => {
    const declaredName = [record.file_name, record.filename].find(
      (candidate) => typeof candidate === "string" && candidate.trim(),
    );
    if (typeof declaredName === "string") return declaredName.trim();
    if (!sourceUrl || sourceUrl.startsWith("blob:")) return "";
    const basename = decodeURIComponent(sourceUrl.split(/[\\/]/).pop() || "");
    return basename.replace(/^[0-9a-f]{32}_/i, "");
  };
  const visit = (value: unknown): void => {
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!value || typeof value !== "object") return;
    const record = value as Record<string, unknown>;
    if (typeof record.text === "string") {
      parts.push(`text:${record.text}`);
      return;
    }
    const mediaType = String(record.type || "").toLowerCase();
    const nestedSource =
      record.source &&
      typeof record.source === "object" &&
      !Array.isArray(record.source)
        ? (record.source as Record<string, unknown>)
        : null;
    const url = [
      record.file_url,
      record.image_url,
      record.audio_url,
      record.video_url,
      record.url,
      record.data,
      nestedSource?.url,
    ].find((candidate) => typeof candidate === "string");
    if (
      ["file", "image", "audio", "video"].includes(mediaType) &&
      typeof url === "string"
    ) {
      parts.push(`asset:${mediaType}:${canonicalAssetName(record, url)}`);
      return;
    }
    Object.entries(record).forEach(([key, child]) => {
      if (
        key !== "id" &&
        key !== "status" &&
        key !== "metadata" &&
        key !== "created_at" &&
        key !== "updated_at"
      ) {
        visit(child);
      }
    });
  };
  for (const card of message.cards || []) {
    visit(card.data?.input ?? card.data);
  }
  return parts.join("\u001f");
}

interface UserMessageClientTurnIdentity {
  key: string;
  explicit: boolean;
}

function userMessageClientTurnIdentity(
  message: ILantaiChatMessage,
): UserMessageClientTurnIdentity {
  if (message.role !== "user") return { key: "", explicit: false };
  const messageId = typeof message.id === "string" ? message.id.trim() : "";
  const candidates: unknown[] = [
    message.client_turn_id,
    (message.metadata as Record<string, unknown> | undefined)?.client_turn_id,
  ];
  const origins: unknown[] = [
    (message.metadata as Record<string, unknown> | undefined)
      ?.client_turn_id_origin,
  ];
  for (const card of message.cards || []) {
    const data = card.data as Record<string, unknown> | undefined;
    candidates.push(
      data?.client_turn_id,
      (data?.metadata as Record<string, unknown> | undefined)?.client_turn_id,
    );
    origins.push(
      data?.client_turn_id_origin,
      (data?.metadata as Record<string, unknown> | undefined)
        ?.client_turn_id_origin,
    );
    const inputs = Array.isArray(data?.input) ? data.input : [];
    for (const input of inputs) {
      if (!input || typeof input !== "object") continue;
      const record = input as Record<string, unknown>;
      candidates.push(
        record.client_turn_id,
        (record.metadata as Record<string, unknown> | undefined)
          ?.client_turn_id,
      );
      origins.push(
        record.client_turn_id_origin,
        (record.metadata as Record<string, unknown> | undefined)
          ?.client_turn_id_origin,
      );
    }
  }
  const key = candidates.find(
    (candidate) => typeof candidate === "string" && candidate.trim(),
  );
  if (typeof key === "string") {
    const normalizedKey = key.trim();
    const origin = origins.find(
      (candidate) => typeof candidate === "string" && candidate.trim(),
    );
    const normalizedOrigin = typeof origin === "string" ? origin.trim() : "";
    return {
      key: normalizedKey,
      explicit: normalizedOrigin !== "message_id_fallback",
    };
  }

  // A message row id is only a compatibility hint. Persisted database ids
  // can also start with `user-`, so they do not prove that two otherwise
  // identical snapshots came from different submissions.
  const messageIdTurnKey = messageId.startsWith("client-turn:")
    ? messageId.slice("client-turn:".length).trim()
    : /^(?:client-turn-|user-)/.test(messageId)
      ? messageId
      : "";
  return { key: messageIdTurnKey, explicit: false };
}

/** Accepted command identity, independent of the message's presentation id. */
export function userMessageCommandId(message: ILantaiChatMessage): string {
  if (message.role !== "user") return "";
  const candidates: unknown[] = [];
  const collect = (value: unknown): void => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    const record = value as Record<string, unknown>;
    const metadata = record.metadata as Record<string, unknown> | undefined;
    candidates.push(
      record.command_id,
      record.runtime_control_command_id,
      metadata?.command_id,
      metadata?.runtime_control_command_id,
    );
  };
  collect(message);
  for (const card of message.cards || []) {
    collect(card.data);
    const inputs = Array.isArray(card.data?.input) ? card.data.input : [];
    inputs.forEach(collect);
  }
  const commandId = candidates.find(
    (candidate) => typeof candidate === "string" && candidate.trim(),
  );
  return typeof commandId === "string" ? commandId.trim() : "";
}

/** Share submission identity between hydration and preserved-session merges. */
export function userMessageSubmissionIdentity(
  message: ILantaiChatMessage,
): UserMessageClientTurnIdentity & {
  source: "client_turn" | "command" | "message_id";
} {
  const clientTurn = userMessageClientTurnIdentity(message);
  if (clientTurn.explicit) return { ...clientTurn, source: "client_turn" };
  const commandId = userMessageCommandId(message);
  if (commandId) return { key: commandId, explicit: true, source: "command" };
  return { ...clientTurn, source: "message_id" };
}

/** Canonical identity shared by optimistic and persisted user-turn cards. */
export function userMessageClientTurnKey(message: ILantaiChatMessage): string {
  return userMessageClientTurnIdentity(message).key;
}

function userMessageAssetCount(message: ILantaiChatMessage): number {
  return userMessageInputSignature(message)
    .split("\u001f")
    .filter((part) => part.startsWith("asset:")).length;
}

export function createTranscriptReconciler(
  primitives: TranscriptReconcilerPrimitives,
): TranscriptReconciler {
  const responseClientTurnKey =
    primitives.responseClientTurnKey || primitives.responseTurnKey;

  const canonicalUserTurnKeyAt = (
    messages: ILantaiChatMessage[],
    messageIndex: number,
  ): string => {
    const message = messages[messageIndex];
    if (message?.role !== "user") return "";
    const identity = userMessageSubmissionIdentity(message);

    // Legacy persisted user rows may predate client_turn_id. Their following
    // assistant response still carries the accepted turn identity. That
    // relationship also outranks old rows which copied their storage id into
    // client_turn_id without an origin marker.
    for (let index = messageIndex + 1; index < messages.length; index += 1) {
      const candidate = messages[index];
      if (candidate?.role === "user") break;
      if (candidate?.role !== "assistant") continue;
      const response = primitives.responseData(candidate);
      const responseTurnKey = response ? responseClientTurnKey(response) : "";
      if (responseTurnKey) return `turn:${responseTurnKey}`;
    }

    if (identity.explicit && identity.key) return `turn:${identity.key}`;

    const messageId = typeof message.id === "string" ? message.id.trim() : "";
    return messageId ? `message:${messageId}` : "";
  };

  const userOrdinalAt = (
    messages: ILantaiChatMessage[],
    messageIndex: number,
  ): number => {
    let ordinal = -1;
    for (let index = 0; index <= messageIndex; index += 1) {
      if (messages[index]?.role === "user") ordinal += 1;
    }
    return ordinal;
  };

  const canReconcileUserByPosition = (
    currentMessages: ILantaiChatMessage[],
    currentIndex: number,
    incomingMessages: ILantaiChatMessage[],
    incomingIndex: number,
  ): boolean => {
    const currentMessage = currentMessages[currentIndex];
    const incomingMessage = incomingMessages[incomingIndex];
    if (currentMessage?.role !== "user" || incomingMessage?.role !== "user") {
      return false;
    }
    const currentIdentity = userMessageSubmissionIdentity(currentMessage);
    const incomingIdentity = userMessageSubmissionIdentity(incomingMessage);
    if (currentIdentity.explicit && incomingIdentity.explicit) return false;

    // Position is only a compatibility key when one projection carries an
    // authoritative client identity and at least one projection has already
    // started rendering the assistant turn. During the first live-stream
    // window, durable history can contain only the persisted user row while
    // the optimistic projection already contains its assistant response.
    // Requiring assistants on both sides duplicates that one accepted submit.
    // Two bare rows remain distinct, as do two explicitly different client turns.
    const currentHasAssistant = currentMessages.some(
      (message) => message.role === "assistant",
    );
    const incomingHasAssistant = incomingMessages.some(
      (message) => message.role === "assistant",
    );
    if (!currentIdentity.explicit && !incomingIdentity.explicit) {
      if (!currentHasAssistant || !incomingHasAssistant) return false;
    } else if (!currentHasAssistant && !incomingHasAssistant) {
      return false;
    }
    return (
      userOrdinalAt(currentMessages, currentIndex) ===
      userOrdinalAt(incomingMessages, incomingIndex)
    );
  };

  const isSameMessage = (
    current: ILantaiChatMessage,
    incoming: ILantaiChatMessage,
    currentTurnKey = "",
    incomingTurnKey = "",
  ): boolean => {
    if (current.role !== incoming.role) return false;
    if (current.role === "user") {
      const currentIdentity = userMessageSubmissionIdentity(current);
      const incomingIdentity = userMessageSubmissionIdentity(incoming);
      // Older history copied a database row id into client_turn_id. A
      // following accepted assistant can prove that differently named row
      // belongs to the same client turn. A reused row id never proves this.
      const canonicalLegacyAlias = Boolean(
        current.id !== incoming.id &&
        currentTurnKey &&
        currentTurnKey === incomingTurnKey &&
        ((currentIdentity.source !== "command" &&
          currentIdentity.key === current.id &&
          currentTurnKey !== `turn:${currentIdentity.key}`) ||
          (incomingIdentity.source !== "command" &&
            incomingIdentity.key === incoming.id &&
            incomingTurnKey !== `turn:${incomingIdentity.key}`)),
      );
      if (
        currentIdentity.explicit &&
        incomingIdentity.explicit &&
        currentIdentity.key !== incomingIdentity.key &&
        !canonicalLegacyAlias
      ) {
        return false;
      }
    }
    if (current.id && incoming.id && current.id === incoming.id) return true;
    if (current.role === "assistant" && incoming.role === "assistant") {
      const currentResponse = primitives.responseData(current);
      const incomingResponse = primitives.responseData(incoming);
      const currentTurnKey = currentResponse
        ? primitives.responseTurnKey(currentResponse)
        : "";
      const incomingTurnKey = incomingResponse
        ? primitives.responseTurnKey(incomingResponse)
        : "";
      return Boolean(
        (currentTurnKey && currentTurnKey === incomingTurnKey) ||
        (currentResponse &&
          incomingResponse &&
          primitives.hasEquivalentStreamedAnswer(
            currentResponse,
            incomingResponse,
          )),
      );
    }
    if (current.role !== "user" || incoming.role !== "user") return false;
    return Boolean(
      currentTurnKey && incomingTurnKey && currentTurnKey === incomingTurnKey,
    );
  };

  const deduplicateUsers = (
    messages: ILantaiChatMessage[],
  ): ILantaiChatMessage[] => {
    const seen = new Set<string>();
    return messages.filter((message, index) => {
      if (message.role !== "user") return true;
      const turnKey = canonicalUserTurnKeyAt(messages, index);
      if (turnKey && seen.has(turnKey)) return false;
      if (turnKey) seen.add(turnKey);
      return true;
    });
  };

  const mergeActive = (
    current: ILantaiChatMessage[],
    incoming: ILantaiChatMessage[],
  ): ILantaiChatMessage[] => {
    const merged = [...current];
    const consumedCurrentIndices = new Set<number>();
    for (const [incomingIndex, incomingMessage] of incoming.entries()) {
      let index = merged.findIndex(
        (currentMessage, currentIndex) =>
          !consumedCurrentIndices.has(currentIndex) &&
          isSameMessage(
            currentMessage,
            incomingMessage,
            canonicalUserTurnKeyAt(merged, currentIndex),
            canonicalUserTurnKeyAt(incoming, incomingIndex),
          ),
      );
      // Partial history may omit the newly queued inputs. Keep the same
      // legacy positional identity established during hydration instead of
      // appending its database row as another user submission.
      if (index === -1 && incomingMessage.role === "user") {
        index = merged.findIndex(
          (currentMessage, currentIndex) =>
            !consumedCurrentIndices.has(currentIndex) &&
            currentMessage.role === "user" &&
            canReconcileUserByPosition(
              merged,
              currentIndex,
              incoming,
              incomingIndex,
            ),
        );
      }
      if (index === -1) {
        merged.push(incomingMessage);
        continue;
      }
      consumedCurrentIndices.add(index);
      const currentMessage = merged[index];
      if (
        currentMessage.role === "assistant" &&
        incomingMessage.role === "assistant" &&
        primitives.shouldMergeAssistantResponses(
          currentMessage,
          incomingMessage,
        )
      ) {
        merged[index] = primitives.mergeAssistantResponses(
          currentMessage,
          incomingMessage,
          "incoming",
        );
      }
    }
    return primitives.normalizeAdjacentAssistantResponses(merged);
  };

  const hasTrailingEmptyOptimisticAssistant = (
    messages: ILantaiChatMessage[],
  ): boolean => {
    const trailing = messages[messages.length - 1];
    if (trailing?.role !== "assistant") return false;
    const response = primitives.responseData(trailing);
    return Boolean(
      response &&
      response.metadata?.optimistic_pending === true &&
      primitives.isActiveStatus(response.status) &&
      (!Array.isArray(response.output) || response.output.length === 0),
    );
  };

  const incomingHasTerminalReplyAfterLastUser = (
    messages: ILantaiChatMessage[],
  ): boolean => {
    const lastUserIndex = messages.reduce(
      (lastIndex, message, index) =>
        message.role === "user" ? index : lastIndex,
      -1,
    );
    if (lastUserIndex < 0) return false;
    return messages.slice(lastUserIndex + 1).some((message) => {
      if (message.role !== "assistant") return false;
      const response = primitives.responseData(message);
      return Boolean(response && primitives.isTerminalStatus(response.status));
    });
  };

  const userTurnAnchorAt = (
    messages: ILantaiChatMessage[],
    messageIndex: number,
  ): string => {
    for (let index = messageIndex - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (message?.role !== "user") continue;
      return canonicalUserTurnKeyAt(messages, index);
    }
    return "";
  };

  const precedingUserOrdinalAt = (
    messages: ILantaiChatMessage[],
    messageIndex: number,
  ): number => {
    let ordinal = -1;
    for (let index = 0; index < messageIndex; index += 1) {
      if (messages[index]?.role === "user") ordinal += 1;
    }
    return ordinal;
  };

  const incomingSnapshotInvertsVisibleTimeline = (
    current: ILantaiChatMessage[],
    incoming: ILantaiChatMessage[],
  ): boolean => {
    const currentFirstAssistantIndex = current.findIndex(
      (message) => message.role === "assistant",
    );
    if (currentFirstAssistantIndex < 0) return false;
    if (
      !current.some(
        (message, index) =>
          index > currentFirstAssistantIndex && message.role === "user",
      )
    ) {
      return false;
    }
    const incomingFirstAssistantIndex = incoming.findIndex(
      (message) => message.role === "assistant",
    );
    if (incomingFirstAssistantIndex < 0) return false;
    if (
      incoming.some(
        (message, index) =>
          index > incomingFirstAssistantIndex && message.role === "user",
      )
    ) {
      return false;
    }
    const currentUsers = current.filter((message) => message.role === "user");
    const incomingUsers = incoming.filter((message) => message.role === "user");
    return (
      currentUsers.length === incomingUsers.length &&
      currentUsers.every((message, index) => {
        const currentIndex = current.indexOf(message);
        const incomingIndex = incoming.indexOf(incomingUsers[index]);
        return (
          isSameMessage(
            message,
            incomingUsers[index],
            canonicalUserTurnKeyAt(current, currentIndex),
            canonicalUserTurnKeyAt(incoming, incomingIndex),
          ) ||
          canReconcileUserByPosition(
            current,
            currentIndex,
            incoming,
            incomingIndex,
          )
        );
      })
    );
  };

  const enrichVisibleTimelineFromInvertedSnapshot = (
    current: ILantaiChatMessage[],
    incoming: ILantaiChatMessage[],
  ): ILantaiChatMessage[] => {
    const incomingAssistants = incoming.filter(
      (message) => message.role === "assistant",
    );
    let assistantIndex = 0;
    return current.map((message) => {
      if (message.role !== "assistant") return message;
      const incomingAssistant = incomingAssistants[assistantIndex++];
      if (
        !incomingAssistant ||
        !primitives.shouldMergeAssistantResponses(message, incomingAssistant)
      ) {
        return message;
      }
      return primitives.mergeAssistantResponses(
        message,
        incomingAssistant,
        "incoming",
      );
    });
  };

  const ownsCanonicalHistoryTimeline = (
    messages: ILantaiChatMessage[],
  ): boolean =>
    messages.some((message) => {
      if (message.role !== "assistant") return false;
      return (
        primitives.responseData(message)?.metadata
          ?.canonical_history_timeline === true
      );
    });

  const incomingContainsEveryVisibleUser = (
    current: ILantaiChatMessage[],
    incoming: ILantaiChatMessage[],
  ): boolean => {
    const consumedIncomingUserIndices = new Set<number>();
    return current
      .filter((message) => message.role === "user")
      .every((currentUser) => {
        let index = incoming.findIndex(
          (incomingUser, incomingIndex) =>
            !consumedIncomingUserIndices.has(incomingIndex) &&
            isSameMessage(
              currentUser,
              incomingUser,
              canonicalUserTurnKeyAt(current, current.indexOf(currentUser)),
              canonicalUserTurnKeyAt(incoming, incomingIndex),
            ),
        );
        if (index === -1) {
          const currentIndex = current.indexOf(currentUser);
          index = incoming.findIndex(
            (incomingUser, incomingIndex) =>
              !consumedIncomingUserIndices.has(incomingIndex) &&
              incomingUser.role === "user" &&
              canReconcileUserByPosition(
                current,
                currentIndex,
                incoming,
                incomingIndex,
              ),
          );
        }
        if (index === -1) return false;
        consumedIncomingUserIndices.add(index);
        return true;
      });
  };

  const choose = (
    current: ILantaiChatMessage[],
    incoming?: ILantaiChatMessage[],
  ): ILantaiChatMessage[] => {
    const normalizedCurrent = deduplicateUsers(
      primitives.normalizeAdjacentAssistantResponses(current),
    );
    if (!Array.isArray(incoming)) return normalizedCurrent;
    const normalizedIncoming = deduplicateUsers(
      primitives.normalizeAdjacentAssistantResponses(incoming),
    );
    if (
      incomingSnapshotInvertsVisibleTimeline(
        normalizedCurrent,
        normalizedIncoming,
      ) &&
      !ownsCanonicalHistoryTimeline(normalizedIncoming)
    ) {
      return enrichVisibleTimelineFromInvertedSnapshot(
        normalizedCurrent,
        normalizedIncoming,
      );
    }
    const consumedCurrentAssistantIndices = new Set<number>();
    const consumedCurrentUserIndices = new Set<number>();
    const reconciledIncoming = normalizedIncoming.map(
      (incomingMessage, incomingIndex) => {
        if (incomingMessage.role === "user") {
          let currentIndex = normalizedCurrent.findIndex(
            (currentMessage, index) =>
              !consumedCurrentUserIndices.has(index) &&
              currentMessage.role === "user" &&
              isSameMessage(
                currentMessage,
                incomingMessage,
                canonicalUserTurnKeyAt(normalizedCurrent, index),
                canonicalUserTurnKeyAt(normalizedIncoming, incomingIndex),
              ),
          );
          if (currentIndex === -1) {
            currentIndex = normalizedCurrent.findIndex(
              (currentMessage, index) =>
                !consumedCurrentUserIndices.has(index) &&
                currentMessage.role === "user" &&
                canReconcileUserByPosition(
                  normalizedCurrent,
                  index,
                  normalizedIncoming,
                  incomingIndex,
                ),
            );
          }
          if (currentIndex === -1) return incomingMessage;
          consumedCurrentUserIndices.add(currentIndex);
          const currentMessage = normalizedCurrent[currentIndex];
          // A route-return cache mirror only stores text. Keep the live card
          // when it carries the same turn identity plus richer attachments.
          return userMessageAssetCount(currentMessage) >
            userMessageAssetCount(incomingMessage)
            ? currentMessage
            : incomingMessage;
        }
        if (incomingMessage.role !== "assistant") return incomingMessage;
        let matchedByUserTurnAnchor = false;
        let currentIndex = normalizedCurrent.findIndex(
          (currentMessage, index) =>
            !consumedCurrentAssistantIndices.has(index) &&
            currentMessage.role === "assistant" &&
            isSameMessage(currentMessage, incomingMessage),
        );
        if (currentIndex === -1 && primitives.responseData(incomingMessage)) {
          const incomingAnchor = userTurnAnchorAt(
            normalizedIncoming,
            incomingIndex,
          );
          if (incomingAnchor) {
            currentIndex = normalizedCurrent.findIndex(
              (currentMessage, index) =>
                !consumedCurrentAssistantIndices.has(index) &&
                currentMessage.role === "assistant" &&
                Boolean(primitives.responseData(currentMessage)) &&
                userTurnAnchorAt(normalizedCurrent, index) === incomingAnchor,
            );
            matchedByUserTurnAnchor = currentIndex !== -1;
          }
          if (currentIndex === -1) {
            const incomingOrdinal = precedingUserOrdinalAt(
              normalizedIncoming,
              incomingIndex,
            );
            currentIndex = normalizedCurrent.findIndex(
              (currentMessage, index) => {
                if (
                  consumedCurrentAssistantIndices.has(index) ||
                  currentMessage.role !== "assistant" ||
                  !primitives.responseData(currentMessage) ||
                  precedingUserOrdinalAt(normalizedCurrent, index) !==
                    incomingOrdinal
                ) {
                  return false;
                }
                const currentAnchor = userTurnAnchorAt(
                  normalizedCurrent,
                  index,
                );
                return !(
                  currentAnchor.startsWith("turn:") &&
                  incomingAnchor.startsWith("turn:")
                );
              },
            );
            matchedByUserTurnAnchor = currentIndex !== -1;
          }
        }
        if (currentIndex === -1) return incomingMessage;
        consumedCurrentAssistantIndices.add(currentIndex);
        const currentMessage = normalizedCurrent[currentIndex];
        return matchedByUserTurnAnchor ||
          primitives.shouldMergeAssistantResponses(
            currentMessage,
            incomingMessage,
          )
          ? primitives.mergeAssistantResponses(
              currentMessage,
              incomingMessage,
              "incoming",
            )
          : incomingMessage;
      },
    );

    const incomingContainsEveryCurrentUser = incomingContainsEveryVisibleUser(
      normalizedCurrent,
      reconciledIncoming,
    );
    if (!incomingContainsEveryCurrentUser) {
      return mergeActive(normalizedCurrent, reconciledIncoming);
    }
    if (
      hasTrailingEmptyOptimisticAssistant(normalizedCurrent) &&
      incomingHasTerminalReplyAfterLastUser(reconciledIncoming)
    ) {
      return reconciledIncoming;
    }
    const currentHasAssistant = normalizedCurrent.some(
      (message) => message.role === "assistant",
    );
    const incomingHasAssistant = reconciledIncoming.some(
      (message) => message.role === "assistant",
    );
    if (currentHasAssistant && !incomingHasAssistant) return normalizedCurrent;
    if (
      normalizedCurrent.length === 0 ||
      reconciledIncoming.length >= normalizedCurrent.length
    ) {
      return reconciledIncoming;
    }
    return normalizedCurrent;
  };

  const reconcileProjection = (
    current: ILantaiChatMessage[],
    incoming: ILantaiChatMessage[] | undefined,
    visibleMessagesBelongToActiveSession: boolean,
  ): ILantaiChatMessage[] => {
    if (!visibleMessagesBelongToActiveSession) {
      return primitives.normalizeAdjacentAssistantResponses(incoming || []);
    }
    return choose(current, incoming);
  };

  const reconcileRouteHydration = ({
    activeSessionMessages,
    localSessionMessages,
    incomingMessages,
    localMatchesCurrentSession,
    durableSessionGenerating,
    primaryStreamActive,
    runtimeWasActive,
  }: TranscriptRouteHydrationInput): TranscriptRouteHydrationResult => {
    const preservedMessages =
      activeSessionMessages.length > 0
        ? activeSessionMessages
        : localSessionMessages.length > incomingMessages.length
          ? localSessionMessages
          : activeSessionMessages;
    const shouldPreserveActiveMessages =
      preservedMessages.length > incomingMessages.length &&
      (durableSessionGenerating ||
        (localMatchesCurrentSession && incomingMessages.length === 0));
    const selectedMessages = shouldPreserveActiveMessages
      ? preservedMessages
      : choose(preservedMessages, incomingMessages);
    const terminalAuthority = Boolean(
      !primaryStreamActive &&
      runtimeWasActive &&
      incomingContainsEveryVisibleUser(preservedMessages, incomingMessages) &&
      !hasTrailingEmptyOptimisticAssistant(incomingMessages) &&
      incomingHasTerminalReplyAfterLastUser(incomingMessages),
    );
    const authoritativeMessages = terminalAuthority
      ? choose(preservedMessages, incomingMessages)
      : selectedMessages;
    return {
      messages: authoritativeMessages,
      preservingExistingMessages: authoritativeMessages === preservedMessages,
      terminalAuthority,
    };
  };

  return {
    choose,
    reconcileProjection,
    incomingContainsEveryVisibleUser,
    hasTrailingEmptyOptimisticAssistant,
    incomingHasTerminalReplyAfterLastUser,
    reconcileRouteHydration,
  };
}
