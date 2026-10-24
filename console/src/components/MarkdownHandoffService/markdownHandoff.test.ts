import { afterEach, describe, expect, it } from "vitest";
import {
  MARKDOWN_HANDOFF_STORAGE_KEY,
  buildMarkdownHandoffPrompt,
  consumeMarkdownHandoff,
  isMarkdownHandoffEnvelope,
  storeMarkdownHandoff,
  type MarkdownHandoffEnvelope,
} from "./markdownHandoff";

const envelope: MarkdownHandoffEnvelope = {
  protocol_version: "2.0",
  mode: "suggest",
  path: "/tmp/note.md",
  document_name: "note.md",
  text: "# Heading",
  selection_only: true,
};

afterEach(() => window.sessionStorage.clear());

describe("Markdown handoff", () => {
  it("builds a bounded, review-first prompt with provenance", () => {
    const prompt = buildMarkdownHandoffPrompt(envelope);
    expect(prompt).toContain("此轮为建议模式");
    expect(prompt).toContain("范围：当前选区");
    expect(prompt).toContain("本地路径：/tmp/note.md");
    expect(prompt).toContain("<markdown_snapshot>\n# Heading");
  });

  it("stores and consumes exactly once", () => {
    storeMarkdownHandoff(envelope);
    expect(window.sessionStorage.getItem(MARKDOWN_HANDOFF_STORAGE_KEY)).toBeTruthy();
    expect(consumeMarkdownHandoff()).toEqual(envelope);
    expect(consumeMarkdownHandoff()).toBeNull();
  });

  it("rejects malformed envelopes", () => {
    expect(isMarkdownHandoffEnvelope({ ...envelope, text: "" })).toBe(false);
    expect(isMarkdownHandoffEnvelope({ ...envelope, mode: "write" })).toBe(false);
  });
});
