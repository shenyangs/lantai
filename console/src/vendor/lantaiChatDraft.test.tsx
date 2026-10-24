import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LantaiRuntimeWebUI, useChatAnywhereInput, useChatAnywhereSessions } from "./lantaiChat";

describe("assistant draft handoff", () => {
  it("does not carry the previous draft into a newly created session", async () => {
    let input: ReturnType<typeof useChatAnywhereInput>;
    let sessions: ReturnType<typeof useChatAnywhereSessions>;
    function Entry() { input = useChatAnywhereInput(); sessions = useChatAnywhereSessions(); return null; }
    const drafts: Record<string, string> = { owner: "旧会话私有草稿" };
    render(<LantaiRuntimeWebUI options={{
      sender: { context: <Entry /> },
      session: { api: {
        getSessionList: async () => [{ id: "owner", name: "Owner", messages: [] }],
        getSession: async (id) => ({ id, name: id, messages: [] }),
        createSession: async () => [{ id: "trial", name: "Trial", messages: [] }, { id: "owner", name: "Owner", messages: [] }],
        loadDraftText: (id) => drafts[id] || "",
        saveDraftText: (id, text) => { drafts[id] = text; },
      } },
    }} />);
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("旧会话私有草稿"));
    await act(async () => {
      const created = await sessions!.createSession();
      expect(input!.appendDraft?.("新任务", created!.id)).toBe(true);
    });
    expect(screen.getByRole("textbox")).toHaveValue("新任务");
    expect(drafts.owner).toBe("旧会话私有草稿");
    expect(drafts.trial).toBe("新任务");
  });
  it("appends to the owner draft without submitting or changing a different conversation", async () => {
    let input: ReturnType<typeof useChatAnywhereInput>;
    function Entry() { input = useChatAnywhereInput(); return null; }
    const fetch = vi.fn();
    const saveDraftText = vi.fn();
    render(<LantaiRuntimeWebUI options={{
      api: { fetch }, sender: { context: <Entry />, ariaLabel: "输入消息" },
      session: { api: {
        getSessionList: async () => [{ id: "owner", name: "Owner", messages: [] }],
        getSession: async () => ({ id: "owner", name: "Owner", messages: [] }),
        loadDraftText: () => "已有草稿", saveDraftText,
      } },
    }} />);
    await waitFor(() => expect(screen.getByRole("textbox")).toHaveValue("已有草稿"));
    act(() => { expect(input!.appendDraft?.("不该出现", "other")).toBe(false); });
    expect(screen.getByRole("textbox")).toHaveValue("已有草稿");
    act(() => { expect(input!.appendDraft?.("请 Codex 协助", "owner")).toBe(true); });
    expect(screen.getByRole("textbox")).toHaveValue("已有草稿\n\n请 Codex 协助");
    expect(fetch).not.toHaveBeenCalled();
    expect(saveDraftText).toHaveBeenCalledWith("owner", "已有草稿\n\n请 Codex 协助");
  });
});
