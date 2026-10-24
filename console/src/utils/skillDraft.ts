import type { ChatHistory, Message } from "../api/types";

const CHAT_SKILL_DRAFT_KEY = "lantai:chat-skill-draft:v1";
const MAX_USER_TEXT_LENGTH = 1600;
const MAX_ASSISTANT_TEXT_LENGTH = 4800;

export interface ChatSkillDraft {
  agentId?: string;
  sourceChatId?: string;
  name: string;
  content: string;
  tags?: string[];
  createdAt: number;
}

function trimText(value: string, maxLength: number): string {
  const normalized = value.replace(/\r\n/g, "\n").trim();
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, maxLength).trim()}\n\n[内容已截断]`;
}

function normalizeSkillName(input: string): string {
  const ascii = input
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (ascii) return ascii.slice(0, 48);
  const now = new Date();
  const stamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
    String(now.getHours()).padStart(2, "0"),
    String(now.getMinutes()).padStart(2, "0"),
  ].join("");
  return `chat-skill-${stamp}`;
}

function buildSkillDescription(userText: string): string {
  const normalized = userText.replace(/\s+/g, " ").trim();
  if (!normalized) return "从聊天中沉淀出的可复用技能草稿";
  return `处理“${normalized.slice(0, 28)}”这类任务的技能草稿`;
}

function extractTextFromContent(content: unknown): string {
  if (!content) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((item) => extractTextFromContent(item))
      .filter(Boolean)
      .join("\n\n");
  }
  if (typeof content !== "object") return "";

  const record = content as Record<string, unknown>;
  if (record.type === "text" && typeof record.text === "string") {
    return record.text;
  }
  if (record.type === "refusal" && typeof record.refusal === "string") {
    return record.refusal;
  }

  const directText =
    (typeof record.text === "string" && record.text) ||
    (typeof record.content === "string" && record.content) ||
    "";
  if (directText) return directText;

  if (Array.isArray(record.content)) {
    return extractTextFromContent(record.content);
  }
  if (Array.isArray(record.output)) {
    return extractTextFromContent(record.output);
  }
  return "";
}

function extractLatestTurn(messages: Message[]): {
  userText: string;
  assistantText: string;
} | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const assistant = messages[index];
    if (assistant.role !== "assistant") continue;
    const assistantText = trimText(
      extractTextFromContent(assistant.content),
      MAX_ASSISTANT_TEXT_LENGTH,
    );
    if (!assistantText) continue;

    for (let userIndex = index - 1; userIndex >= 0; userIndex -= 1) {
      const userMessage = messages[userIndex];
      if (userMessage.role !== "user") continue;
      const userText = trimText(
        extractTextFromContent(userMessage.content),
        MAX_USER_TEXT_LENGTH,
      );
      if (!userText) continue;
      return { userText, assistantText };
    }
  }
  return null;
}

function buildSkillContent(
  name: string,
  userText: string,
  assistantText: string,
) {
  const description = buildSkillDescription(userText);
  return `---
name: ${name}
description: ${description}
---

# ${name}

## Use when
- 用户的目标与“原始需求”接近，需要复用同类处理流程。
- 需要把一次成功对话沉淀成可持续复用的技能草稿。

## What to do
1. 先识别任务目标、约束和希望的输出形态。
2. 参考下方“成功回复参考”，抽取稳定步骤、结构和判断顺序。
3. 输出时保留清晰结构，避免把一次性上下文原样照搬。

## Output expectations
- 给出可直接交付给用户的结果。
- 结构清楚，必要时包含步骤、结论和后续建议。

## Original request
${userText}

## Successful response reference
${assistantText}

## Notes
- 这是从聊天自动生成的技能草稿。
- 保存前建议在技能页继续精修，或使用“AI 优化”整理结构。
`;
}

export function createSkillDraftFromChatHistory(
  history: ChatHistory,
  options?: {
    agentId?: string;
    chatId?: string;
  },
): ChatSkillDraft | null {
  const latestTurn = extractLatestTurn(
    Array.isArray(history.messages) ? history.messages : [],
  );
  if (!latestTurn) return null;

  const name = normalizeSkillName(latestTurn.userText);
  return {
    agentId: options?.agentId,
    sourceChatId: options?.chatId,
    name,
    content: buildSkillContent(
      name,
      latestTurn.userText,
      latestTurn.assistantText,
    ),
    tags: ["聊天沉淀"],
    createdAt: Date.now(),
  };
}

export function stashSkillDraft(draft: ChatSkillDraft) {
  window.sessionStorage.setItem(CHAT_SKILL_DRAFT_KEY, JSON.stringify(draft));
}

export function consumeSkillDraft(agentId?: string): ChatSkillDraft | null {
  const raw = window.sessionStorage.getItem(CHAT_SKILL_DRAFT_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as ChatSkillDraft;
    if (parsed.agentId && agentId && parsed.agentId !== agentId) {
      return null;
    }
    if (!parsed.name || !parsed.content) return null;
    window.sessionStorage.removeItem(CHAT_SKILL_DRAFT_KEY);
    return parsed;
  } catch {
    window.sessionStorage.removeItem(CHAT_SKILL_DRAFT_KEY);
    return null;
  }
}
