import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import WorkEditSessionBanner from "./WorkEditSessionBanner";

const { mockNavigate, sessionState } = vi.hoisted(() => ({
  mockNavigate: vi.fn(),
  sessionState: {
    currentSessionId: "local-1",
    sessions: [] as Array<Record<string, unknown>>,
  },
}));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: () => sessionState,
}));

describe("WorkEditSessionBanner", () => {
  beforeEach(() => {
    mockNavigate.mockReset();
    sessionState.currentSessionId = "local-1";
    sessionState.sessions = [];
  });

  it("keeps the bound work and copy semantics visible above the composer", () => {
    sessionState.sessions = [
      {
        id: "local-1",
        realId: "chat-edit-1",
        name: "修改：WPS获颁AI办公行业首个国家级权威认证",
        meta: {
          work_edit_session: {
            schema_version: 1,
            work_id: "work-1",
            title: "WPS获颁AI办公行业首个国家级权威认证",
            kind: "presentation",
            source_version: 1,
          },
        },
      },
    ];

    render(<WorkEditSessionBanner />);

    expect(
      screen.getByRole("region", {
        name: "正在修改 WPS获颁AI办公行业首个国家级权威认证 的独立副本",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("正在修改")).toBeInTheDocument();
    expect(
      screen.getByText("WPS获颁AI办公行业首个国家级权威认证"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("基于 V1 创建的独立副本 · 原作品不会被覆盖"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "查看原作品" }));
    expect(mockNavigate).toHaveBeenCalledWith("/works?work=work-1");
  });

  it("keeps older edit sessions understandable after upgrading", () => {
    sessionState.sessions = [
      {
        id: "local-1",
        name: "修改：季度复盘",
        meta: {
          work_edit_id: "legacy/work-1",
          work_edit_parent_version: 3,
        },
      },
    ];

    render(<WorkEditSessionBanner />);

    expect(screen.getByText("季度复盘")).toBeInTheDocument();
    expect(
      screen.getByText("基于 V3 创建的独立副本 · 原作品不会被覆盖"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看原作品" }));
    expect(mockNavigate).toHaveBeenCalledWith("/works?work=legacy%2Fwork-1");
  });

  it("does not show an editing claim for an ordinary chat", () => {
    sessionState.sessions = [
      {
        id: "local-1",
        name: "普通会话",
        meta: {},
      },
    ];

    const { container } = render(<WorkEditSessionBanner />);

    expect(container).toBeEmptyDOMElement();
  });
});
