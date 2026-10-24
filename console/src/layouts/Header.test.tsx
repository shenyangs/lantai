import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/common_setup";
import {
  CREATE_NEW_CHAT_SETTLED_EVENT,
  type CreateNewChatSettledDetail,
} from "@/pages/Chat/chatSessionEvents";
import Header from "./Header";

const { mockNavigate, mockListWorkspaceGrants, mockAgentState } = vi.hoisted(
  () => ({
    mockNavigate: vi.fn(),
    mockListWorkspaceGrants: vi.fn().mockResolvedValue([]),
    mockAgentState: {
      selectedAgent: "default",
      agents: [
        {
          id: "default",
          name: "沈行知",
          workspace_dir: "/tmp/lantai",
          enabled: true,
        },
      ],
      setAgents: vi.fn(),
    },
  }),
);

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("../stores/agentStore", () => {
  const useAgentStore = () => mockAgentState;
  useAgentStore.getState = () => mockAgentState;
  return { useAgentStore };
});

vi.mock("../api/modules/agents", () => ({
  agentsApi: { listAgents: vi.fn().mockResolvedValue({ agents: [] }) },
}));

vi.mock("../api/modules/workspaceGrants", () => ({
  workspaceGrantsApi: { list: mockListWorkspaceGrants },
}));

vi.mock("../api/modules/chat", () => ({
  chatApi: { ensureTopicWorkspace: vi.fn() },
}));

vi.mock("../hooks/useAppMessage", () => ({
  useAppMessage: () => ({ message: { error: vi.fn(), success: vi.fn() } }),
}));

vi.mock("../pages/Chat/ModelSelector", () => ({
  default: ({
    open = false,
    onOpenChange,
  }: {
    open?: boolean;
    onOpenChange?: (open: boolean) => void;
  }) => (
    <button
      type="button"
      aria-label="模型选择"
      aria-expanded={open}
      onClick={() => onOpenChange?.(!open)}
    >
      model
    </button>
  ),
}));

vi.mock("../components/ThemeToggleButton", () => ({
  default: ({
    open = false,
    onOpenChange,
  }: {
    open?: boolean;
    onOpenChange?: (open: boolean) => void;
  }) => (
    <button
      type="button"
      aria-label="主题选择"
      aria-expanded={open}
      onClick={() => onOpenChange?.(!open)}
    >
      theme
    </button>
  ),
}));

vi.mock("../utils/topicWorkspace", () => ({
  getActiveTopicWorkspace: () => null,
  setActiveTopicWorkspace: vi.fn(),
  TOPIC_WORKSPACE_CHANGED_EVENT: "lantai:topic-workspace-changed",
}));

describe("Header new chat command", () => {
  beforeEach(() => {
    mockNavigate.mockReset();
    window.sessionStorage.clear();
  });

  it("acknowledges one new-chat request before accepting another", async () => {
    renderWithProviders(<Header />);
    const button = screen.getByRole("button", {
      name: "chat.newChatTooltip",
    });

    fireEvent.click(button);
    fireEvent.click(button);

    expect(mockNavigate).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();

    window.dispatchEvent(
      new CustomEvent<CreateNewChatSettledDetail>(
        CREATE_NEW_CHAT_SETTLED_EVENT,
        { detail: { ok: true } },
      ),
    );
    await waitFor(() => expect(button).toBeEnabled());
  });

  it("keeps the header menus mutually exclusive", async () => {
    renderWithProviders(<Header />);
    const modelButton = screen.getByRole("button", { name: "模型选择" });
    const workspaceButton = screen.getByTitle("/tmp/lantai");

    fireEvent.click(modelButton);
    expect(modelButton).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(workspaceButton);
    await waitFor(() => {
      expect(modelButton).toHaveAttribute("aria-expanded", "false");
      expect(workspaceButton).toHaveAttribute("aria-expanded", "true");
    });

    fireEvent.click(modelButton);
    await waitFor(() => {
      expect(modelButton).toHaveAttribute("aria-expanded", "true");
      expect(workspaceButton).toHaveAttribute("aria-expanded", "false");
    });

    const themeButton = screen.getByRole("button", { name: "主题选择" });
    fireEvent.click(themeButton);
    await waitFor(() => {
      expect(modelButton).toHaveAttribute("aria-expanded", "false");
      expect(workspaceButton).toHaveAttribute("aria-expanded", "false");
      expect(themeButton).toHaveAttribute("aria-expanded", "true");
    });
  });
});
