import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/common_setup";
import PinnedSessionStrip from "./PinnedSessionStrip";

const state = vi.hoisted(() => ({
  setCurrentSessionId: vi.fn(),
  selectSession: vi.fn(),
}));

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: () => ({
    currentSessionId: "one",
    setCurrentSessionId: state.setCurrentSessionId,
    sessions: [
      { id: "one", name: "第一项工作", pinned: true },
      { id: "two", name: "第二项工作", pinned: true },
      { id: "hidden", name: "普通会话", pinned: false },
    ],
  }),
}));

vi.mock("../sessionApi", () => ({
  default: { selectSession: state.selectSession },
}));

describe("PinnedSessionStrip", () => {
  beforeEach(() => vi.clearAllMocks());

  it("switches among pinned conversations without reopening the history drawer", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PinnedSessionStrip onOpenAll={vi.fn()} />);

    expect(
      screen.getByRole("navigation", { name: "置顶会话快速切换" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("普通会话")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /切换到 第二项工作/ }));

    expect(state.selectSession).toHaveBeenCalledWith("two");
    expect(state.setCurrentSessionId).toHaveBeenCalledWith("two");
  });
});
