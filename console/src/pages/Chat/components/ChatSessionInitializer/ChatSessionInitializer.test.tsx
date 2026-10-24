import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ChatSessionInitializer from "./index";

const { mockCreateSession, mockSetCurrentSessionId, mockSessionState } =
  vi.hoisted(() => ({
    mockCreateSession: vi.fn(),
    mockSetCurrentSessionId: vi.fn(),
    mockSessionState: {
      sessions: [] as any[],
      currentSessionId: null as string | null,
    },
  }));

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: () => ({
    sessions: mockSessionState.sessions,
    currentSessionId: mockSessionState.currentSessionId,
    setCurrentSessionId: mockSetCurrentSessionId,
  }),
  useChatAnywhereSessions: () => ({
    createSession: mockCreateSession,
  }),
}));

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
};

describe("ChatSessionInitializer", () => {
  beforeEach(() => {
    mockSessionState.sessions = [];
    mockSessionState.currentSessionId = null;
    mockCreateSession.mockReset();
    mockCreateSession.mockResolvedValue(undefined);
    mockSetCurrentSessionId.mockReset();
    window.sessionStorage.clear();
  });

  it("creates exactly one new chat for the process cold-start route and consumes the marker", async () => {
    render(
      <MemoryRouter initialEntries={["/chat?lantaiColdStart=1"]}>
        <ChatSessionInitializer />
        <LocationProbe />
      </MemoryRouter>,
    );

    await waitFor(() => expect(mockCreateSession).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.getByTestId("location-search").textContent).toBe(""),
    );
  });

  it("creates a source-bound chat from the knowledge workspace context", async () => {
    window.sessionStorage.setItem(
      "lantai:create-new-chat-context",
      JSON.stringify({
        knowledgeSource: {
          path: ".wiki/raw/sources/budget.xlsx",
          filename: "budget.xlsx",
          kind: "office",
        },
      }),
    );

    render(
      <MemoryRouter initialEntries={["/chat?lantaiColdStart=1"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(mockCreateSession).toHaveBeenCalledWith({
        name: "关于 budget.xlsx",
        meta: {
          knowledge_scope_mode: "exact",
          knowledge_source: {
            path: ".wiki/raw/sources/budget.xlsx",
            filename: "budget.xlsx",
            kind: "office",
          },
          knowledge_sources: [
            {
              path: ".wiki/raw/sources/budget.xlsx",
              filename: "budget.xlsx",
              kind: "office",
            },
          ],
        },
      }),
    );
    expect(
      window.sessionStorage.getItem("lantai:create-new-chat-context"),
    ).toBeNull();
  });

  it("creates a chat with an explicit multi-source scope", async () => {
    window.sessionStorage.setItem(
      "lantai:create-new-chat-context",
      JSON.stringify({
        knowledgeSources: [
          { path: ".wiki/raw/alpha.txt", filename: "alpha.txt" },
          { path: ".wiki/raw/beta.txt", filename: "beta.txt" },
        ],
        knowledgeScopeMode: "expanded",
      }),
    );

    render(
      <MemoryRouter initialEntries={["/chat?lantaiColdStart=1"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(mockCreateSession).toHaveBeenCalledWith({
        name: "基于 2 份资料",
        meta: {
          knowledge_sources: [
            { path: ".wiki/raw/alpha.txt", filename: "alpha.txt" },
            { path: ".wiki/raw/beta.txt", filename: "beta.txt" },
          ],
          knowledge_scope_mode: "expanded",
        },
      }),
    );
  });

  it("does not create a new chat for an ordinary same-process return to the chat route", async () => {
    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() => expect(mockCreateSession).not.toHaveBeenCalled());
  });

  it("clears the old selected conversation before creating a requested new chat", async () => {
    mockSessionState.currentSessionId = "old-chat";
    window.sessionStorage.setItem("lantai:create-new-chat", "1");

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() => expect(mockCreateSession).toHaveBeenCalledTimes(1));
    expect(mockSetCurrentSessionId).toHaveBeenCalledWith(undefined);
    expect(mockSetCurrentSessionId.mock.invocationCallOrder[0]).toBeLessThan(
      mockCreateSession.mock.invocationCallOrder[0],
    );
  });

  it("selects the local session id when the URL uses its real backend id", async () => {
    mockSessionState.sessions = [
      {
        id: "temp-123",
        realId: "chat-real-1",
        name: "测试会话",
      },
    ];

    render(
      <MemoryRouter initialEntries={["/chat/chat-real-1"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockSetCurrentSessionId).toHaveBeenCalledWith("temp-123");
    });
  });

  it("selects the URL chat id directly when the session list is stale", async () => {
    render(
      <MemoryRouter initialEntries={["/chat/wechat-chat-1"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockSetCurrentSessionId).toHaveBeenCalledWith("wechat-chat-1");
    });
  });

  it("selects the local session id when the URL uses the backend session id", async () => {
    mockSessionState.sessions = [
      {
        id: "local-chat-1",
        realId: "chat-real-2",
        sessionId: "console:backend-session-2",
        name: "后端会话",
      },
    ];

    render(
      <MemoryRouter initialEntries={["/chat/console:backend-session-2"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockSetCurrentSessionId).toHaveBeenCalledWith("local-chat-1");
    });
  });

  it("does not route an ambiguous shared session alias to the first chat", async () => {
    mockSessionState.sessions = [
      {
        id: "chat-one",
        realId: "chat-one",
        sessionId: "console:shared",
      },
      {
        id: "chat-two",
        realId: "chat-two",
        sessionId: "console:shared",
      },
    ];

    render(
      <MemoryRouter initialEntries={["/chat/console:shared"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockSetCurrentSessionId).toHaveBeenCalledWith("console:shared");
    });
    expect(mockSetCurrentSessionId).not.toHaveBeenCalledWith("chat-one");
    expect(mockSetCurrentSessionId).not.toHaveBeenCalledWith("chat-two");
  });

  it("does not pull an explicit branch selection back to an unchanged source URL", async () => {
    const source = { id: "source-chat", realId: "source-chat" };
    const branch = { id: "branch-chat", realId: "branch-chat" };
    mockSessionState.sessions = [source];
    mockSessionState.currentSessionId = "source-chat";

    const view = render(
      <MemoryRouter initialEntries={["/chat/source-chat"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockSetCurrentSessionId).not.toHaveBeenCalled();
    });

    mockSessionState.sessions = [branch, source];
    mockSessionState.currentSessionId = "branch-chat";
    view.rerender(
      <MemoryRouter initialEntries={["/chat/source-chat"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(mockSetCurrentSessionId).not.toHaveBeenCalled();
    });
  });

  it("accepts only one external new-chat request while creation is pending", async () => {
    let resolveCreate: (() => void) | undefined;
    mockCreateSession.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    window.sessionStorage.setItem("lantai:create-new-chat", "1");

    render(
      <MemoryRouter initialEntries={["/chat"]}>
        <ChatSessionInitializer />
      </MemoryRouter>,
    );
    await waitFor(() => expect(mockCreateSession).toHaveBeenCalledTimes(1));

    window.sessionStorage.setItem("lantai:create-new-chat", "1");
    act(() => {
      window.dispatchEvent(new Event("lantai:create-new-chat"));
    });
    expect(mockCreateSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveCreate?.();
    });
  });
});
