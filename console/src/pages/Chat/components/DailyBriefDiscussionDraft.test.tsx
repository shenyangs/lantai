import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DailyBriefDiscussionDraft from "./DailyBriefDiscussionDraft";

const { getDiscussionDraft } = vi.hoisted(() => ({
  getDiscussionDraft: vi.fn(),
}));
vi.mock("@/api/modules/dailyBrief", () => ({
  dailyBriefApi: { getDiscussionDraft },
}));

beforeEach(() => {
  vi.resetAllMocks();
  getDiscussionDraft.mockResolvedValue({
    composer_text: "讨论问题\n已查证的事实\n来源：https://example.com/primary",
  });
});

describe("brief discussion handoff", () => {
  it("inserts context only after a click, revalidates it, and leaves sending to the user", async () => {
    const onInsert = vi.fn(() => true);
    const onClose = vi.fn();
    render(
      <DailyBriefDiscussionDraft
        draftId="draft-1"
        onInsert={onInsert}
        onClose={onClose}
      />,
    );
    const button = await screen.findByRole("button", { name: "插入到输入框" });
    expect(onInsert).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    await userEvent.click(button);
    await waitFor(() =>
      expect(onInsert).toHaveBeenCalledWith(
        expect.stringContaining("来源：https://example.com/primary"),
      ),
    );
    expect(getDiscussionDraft).toHaveBeenCalledTimes(2);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("does not insert a draft whose authority was revoked after it loaded", async () => {
    const onInsert = vi.fn(() => true);
    getDiscussionDraft
      .mockResolvedValueOnce({ composer_text: "可用草稿" })
      .mockRejectedValueOnce(new Error("revoked"));
    render(
      <DailyBriefDiscussionDraft
        draftId="draft-1"
        onInsert={onInsert}
        onClose={vi.fn()}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "插入到输入框" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("草稿已不可用");
    expect(onInsert).not.toHaveBeenCalled();
  });
});
