import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/common_setup";
import WorksPage, { workSourceDestination } from "./index";

const {
  mockAdd,
  mockContentBlob,
  mockCoverBlob,
  mockGenerateCover,
  mockInspectIndex,
  mockPreviewIndexRecovery,
  mockRestoreIndex,
  mockGetImageConfig,
  mockList,
  mockListTaskRuns,
  mockCreateEditSession,
  mockGetPrivateEditorSettings,
  mockStartPrivateEditor,
  mockUpdateImageConfig,
  mockAgentState,
} = vi.hoisted(() => ({
  mockAdd: vi.fn(),
  mockContentBlob: vi.fn(),
  mockCoverBlob: vi.fn(),
  mockGenerateCover: vi.fn(),
  mockInspectIndex: vi.fn(),
  mockPreviewIndexRecovery: vi.fn(),
  mockRestoreIndex: vi.fn(),
  mockGetImageConfig: vi.fn(),
  mockList: vi.fn(),
  mockListTaskRuns: vi.fn(),
  mockCreateEditSession: vi.fn(),
  mockGetPrivateEditorSettings: vi.fn(),
  mockStartPrivateEditor: vi.fn(),
  mockUpdateImageConfig: vi.fn(),
  mockAgentState: { selectedAgent: "default" },
}));

vi.mock("../../stores/agentStore", () => ({
  useAgentStore: () => ({ selectedAgent: mockAgentState.selectedAgent }),
}));

vi.mock("../../api/modules/works", () => ({
  worksApi: {
    list: mockList,
    inspectIndex: mockInspectIndex,
    previewIndexRecovery: mockPreviewIndexRecovery,
    restoreIndex: mockRestoreIndex,
    add: mockAdd,
    remove: vi.fn(),
    createEditSession: mockCreateEditSession,
    contentBlob: mockContentBlob,
    coverBlob: mockCoverBlob,
    generateCover: mockGenerateCover,
  },
}));

vi.mock("../../api/modules/imageGeneration", () => ({
  imageGenerationApi: {
    getImageGenerationConfig: mockGetImageConfig,
    updateImageGenerationConfig: mockUpdateImageConfig,
  },
}));

vi.mock("../../api/modules/privateEditor", () => ({
  privateEditorApi: {
    getSettings: mockGetPrivateEditorSettings,
    startOrContinue: mockStartPrivateEditor,
  },
}));

vi.mock("../../api/modules/capability", () => ({
  capabilityApi: {
    listTaskRuns: mockListTaskRuns,
  },
}));

const savedWork = {
  id: "work-1",
  artifact_id: "artifact-saved",
  title: "季度复盘",
  kind: "presentation" as const,
  path: "/tmp/works/deck.pptx",
  source_path: "/tmp/topic/deck.pptx",
  mime_type:
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  size: 4096,
  source_chat_id: "chat-1",
  source_run_id: "run-1",
  created_at: "2026-08-09T08:00:00Z",
  updated_at: "2026-08-09T08:00:00Z",
};

describe("WorksPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAgentState.selectedAgent = "default";
    window.localStorage.clear();
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      openOfficeEditor: vi.fn().mockResolvedValue({ success: true }),
    };
    mockList.mockResolvedValue([savedWork]);
    mockInspectIndex.mockResolvedValue({
      status: "ready",
      code: null,
      diagnostic_id: null,
      current_digest: "sha256:ready",
      schema_version: 5,
      work_count: 1,
      invalid_record_indexes: [],
      managed_work_ids: ["work-1"],
      orphaned_managed_ids: [],
      backups: [],
    });
    mockGetImageConfig.mockResolvedValue({
      enabled: false,
      models: [],
      timeout_seconds: 120,
      works_cover_enabled: false,
      works_cover_model: { provider_id: "", model: "" },
    });
    mockGetPrivateEditorSettings.mockResolvedValue({
      enabled: false,
      research_consent: false,
      revocation_epoch: 0,
      updated_at: "2026-09-19T00:00:00Z",
    });
    mockContentBlob.mockResolvedValue(new Blob(["<title>俄罗斯方块</title>"]));
    mockListTaskRuns.mockResolvedValue([
      {
        id: "run-2",
        agent_id: "default",
        source: "chat",
        chat_id: "chat-2",
        title: "制作俄罗斯方块",
        status: "succeeded",
        started_at: "2026-08-09T09:00:00Z",
        steps: [],
        artifacts: [
          {
            id: "artifact-game",
            type: "html",
            title: "俄罗斯方块.html",
            path: "/tmp/topic/tetris.html",
            created_at: "2026-08-09T09:00:00Z",
          },
        ],
      },
    ]);
    mockAdd.mockResolvedValue({
      ...savedWork,
      id: "work-2",
      artifact_id: "artifact-game-copy",
      title: "俄罗斯方块.html",
      kind: "html",
      path: "/tmp/works/tetris.html",
      source_path: "/tmp/topic/tetris.html",
    });
    mockCreateEditSession.mockResolvedValue({
      id: "chat-edit-1",
      name: "修改：季度复盘",
      session_id: "console:work-edit:work-1:edit-1",
      user_id: "default",
      channel: "console",
      meta: { work_edit_id: "work-1" },
      status: "idle",
      pinned: false,
      created_at: "2026-08-09T10:00:00Z",
      updated_at: "2026-08-09T10:00:00Z",
    });
    mockStartPrivateEditor.mockResolvedValue({
      project: {
        id: "private-project-1",
        title: "季度复盘",
      },
      chat: {
        id: "chat-private-1",
        name: "共作：季度复盘",
        session_id: "console:private-editor:private-project-1",
        user_id: "default",
        channel: "console",
        meta: { private_editor_project_id: "private-project-1" },
        status: "idle",
        pinned: false,
        created_at: "2026-09-19T00:00:00Z",
        updated_at: "2026-09-19T00:00:00Z",
      },
    });
  });

  it("previews and confirms index recovery without silently retrying the write", async () => {
    const user = userEvent.setup();
    mockList
      .mockRejectedValueOnce(new Error("作品索引无法解析"))
      .mockResolvedValue([savedWork]);
    mockInspectIndex.mockResolvedValue({
      status: "recovery_required",
      code: "works_index_invalid_json",
      diagnostic_id: "works-index-red",
      current_digest: "sha256:broken",
      schema_version: null,
      work_count: 0,
      invalid_record_indexes: [],
      managed_work_ids: ["work-1", "work-2"],
      orphaned_managed_ids: ["work-1", "work-2"],
      backups: [
        {
          digest: "sha256:good",
          schema_version: 5,
          work_count: 1,
          created_at: "2026-09-21T00:00:00Z",
        },
      ],
    });
    mockPreviewIndexRecovery.mockResolvedValue({
      current_digest: "sha256:broken",
      snapshot_digest: "sha256:good",
      snapshot_schema_version: 5,
      snapshot_works: [savedWork],
      orphaned_managed_ids: ["work-2"],
      confirmation_required: true,
    });
    mockRestoreIndex.mockResolvedValue({
      status: "restored",
      restored_digest: "sha256:good",
      quarantine_digest: "sha256:broken",
      work_count: 1,
    });

    renderWithProviders(<WorksPage />);

    expect(
      await screen.findByRole("heading", { name: "作品索引需要恢复" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/诊断编号：works-index-red/)).toBeInTheDocument();
    expect(mockRestoreIndex).not.toHaveBeenCalled();

    await user.click(
      screen.getByRole("button", { name: "预览并恢复最近备份" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "恢复作品索引？" }),
    ).toHaveTextContent("将恢复 1 条作品记录");
    expect(mockRestoreIndex).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "确认恢复" }));
    await waitFor(() =>
      expect(mockRestoreIndex).toHaveBeenCalledWith(
        "sha256:broken",
        "sha256:good",
      ),
    );
    await waitFor(() => expect(mockList).toHaveBeenCalledTimes(2));
  });

  it("never renders JSON or garbled API output as a work cover", async () => {
    mockList.mockResolvedValue([
      {
        ...savedWork,
        kind: "html",
        title: "八字排盘小工具",
        path: "/tmp/works/bazi.html",
      },
    ]);
    mockContentBlob.mockResolvedValue(
      new Blob(["{}"], { type: "application/json" }),
    );

    renderWithProviders(<WorksPage />);

    expect(
      (await screen.findAllByText("八字排盘小工具")).length,
    ).toBeGreaterThan(0);
    await waitFor(() => expect(mockList).toHaveBeenCalled());
    expect(mockContentBlob).not.toHaveBeenCalled();
    expect(
      screen.queryByTitle("八字排盘小工具 缩略预览"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("网页 / 小工具")).toBeInTheDocument();
  });

  it("defaults to a screenshot gallery and can switch to a list", async () => {
    renderWithProviders(<WorksPage />);

    const pageHeading = await screen.findByRole("heading", {
      level: 1,
      name: "作品",
    });
    expect(pageHeading).toBeVisible();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.queryByLabelText("当前位置")).not.toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "作品截图视图" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("季度复盘").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "列表视图" }));

    expect(
      screen.getByRole("region", { name: "作品列表视图" }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("lantai:works-view-mode")).toBe("list");
  });

  it("contains square native thumbnails instead of cropping them", async () => {
    window.electronAPI = {
      ...window.electronAPI,
      createWorkThumbnail: vi.fn().mockResolvedValue({
        success: true,
        data_url: "data:image/png;base64,native-thumbnail",
      }),
    };

    const view = renderWithProviders(<WorksPage />);
    const thumbnail = await waitFor(() => {
      const image = view.container.querySelector<HTMLImageElement>(
        'img[src="data:image/png;base64,native-thumbnail"]',
      );
      expect(image).not.toBeNull();
      return image as HTMLImageElement;
    });
    Object.defineProperties(thumbnail, {
      naturalWidth: { configurable: true, value: 1024 },
      naturalHeight: { configurable: true, value: 1024 },
    });

    fireEvent.load(thumbnail);

    expect(thumbnail).toHaveAttribute("data-preview-fit", "contain");
    expect(thumbnail).toHaveStyle({ objectFit: "contain" });
  });

  it("keeps landscape native thumbnails filling the preview", async () => {
    window.electronAPI = {
      ...window.electronAPI,
      createWorkThumbnail: vi.fn().mockResolvedValue({
        success: true,
        data_url: "data:image/png;base64,landscape-thumbnail",
      }),
    };

    const view = renderWithProviders(<WorksPage />);
    const thumbnail = await waitFor(() => {
      const image = view.container.querySelector<HTMLImageElement>(
        'img[src="data:image/png;base64,landscape-thumbnail"]',
      );
      expect(image).not.toBeNull();
      return image as HTMLImageElement;
    });
    Object.defineProperties(thumbnail, {
      naturalWidth: { configurable: true, value: 1920 },
      naturalHeight: { configurable: true, value: 1080 },
    });

    fireEvent.load(thumbnail);

    expect(thumbnail).toHaveAttribute("data-preview-fit", "cover");
    expect(thumbnail).toHaveStyle({ objectFit: "cover" });
  });

  it("keeps unsupported presentation results actionable in their preview", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorksPage />);

    await user.click(
      await screen.findByRole("button", { name: "打开作品：季度复盘" }),
    );

    const previewDialog = screen.getByRole("dialog");
    expect(
      within(previewDialog).getByText(/当前类型不能在澜台内预览/),
    ).toBeInTheDocument();
    expect(
      within(previewDialog).getByRole("button", { name: "在访达中显示" }),
    ).toBeInTheDocument();
    expect(
      within(previewDialog).getByRole("button", { name: "导出副本" }),
    ).toBeInTheDocument();
    expect(
      within(previewDialog).getByRole("button", { name: "创建修改副本" }),
    ).toBeInTheDocument();
  });

  it("shows the explicit private-editor entry only for enabled supported works", async () => {
    const user = userEvent.setup();
    mockGetPrivateEditorSettings.mockResolvedValue({
      enabled: true,
      research_consent: false,
      revocation_epoch: 0,
      updated_at: "2026-09-19T00:00:00Z",
    });
    mockList.mockResolvedValue([
      {
        ...savedWork,
        kind: "markdown",
        title: "待修文章",
        path: "/tmp/works/article.md",
        mime_type: "text/markdown",
      },
    ]);
    mockContentBlob.mockResolvedValue(new Blob(["第一段。\n\n第二段。"]));

    renderWithProviders(<WorksPage />);
    await user.click(
      await screen.findByRole("button", { name: "打开作品：待修文章" }),
    );
    const previewDialog = screen.getByRole("dialog");
    await user.click(
      within(previewDialog).getByRole("button", { name: "继续共作" }),
    );

    await waitFor(() =>
      expect(mockStartPrivateEditor).toHaveBeenCalledWith("work-1"),
    );
  });

  it("adds an omitted recent artifact without returning to the old chat", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorksPage />);

    expect((await screen.findAllByText("季度复盘")).length).toBeGreaterThan(0);
    await user.click(
      screen.getAllByRole("button", { name: /从最近产物添加/ })[0],
    );
    expect(
      (await screen.findAllByText("俄罗斯方块.html")).length,
    ).toBeGreaterThan(0);

    await user.click(screen.getByRole("button", { name: "加入作品" }));

    await waitFor(() =>
      expect(mockAdd).toHaveBeenCalledWith({
        path: "/tmp/topic/tetris.html",
        title: "俄罗斯方块.html",
        source_chat_id: "chat-2",
        source_run_id: "run-2",
      }),
    );
    expect(
      (await screen.findAllByText("俄罗斯方块.html")).length,
    ).toBeGreaterThan(0);
  });

  it("keeps costly AI cover generation disabled until the user configures it", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorksPage />);

    await screen.findByRole("heading", { name: "作品" });
    await user.click(screen.getByRole("button", { name: "作品封面设置" }));

    expect(
      screen.getByRole("switch", { name: "为作品启用 AI 封面" }),
    ).toBeDisabled();
    expect(screen.getByText(/标题排版封面仍然始终可用/)).toBeInTheDocument();
  });

  it("runs an HTML mini-app inside the Lantai desktop container", async () => {
    const user = userEvent.setup();
    const openHtmlWork = vi.fn().mockResolvedValue({ success: true });
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      openHtmlWork,
    };
    mockList.mockResolvedValue([
      {
        ...savedWork,
        kind: "html",
        title: "俄罗斯方块",
        path: "/tmp/works/tetris.html",
        source_path: "/tmp/topic/tetris.html",
      },
    ]);

    renderWithProviders(<WorksPage />);
    await user.click(
      await screen.findByRole("button", { name: "打开作品：俄罗斯方块" }),
    );

    expect(openHtmlWork).toHaveBeenCalledWith(
      "/tmp/works/tetris.html",
      "俄罗斯方块",
    );
  });

  it("does not open a saved work whose immutable version was modified", async () => {
    const user = userEvent.setup();
    const openHtmlWork = vi.fn().mockResolvedValue({ success: true });
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      openHtmlWork,
    };
    mockList.mockResolvedValue([
      {
        ...savedWork,
        kind: "html",
        title: "被改动的工具",
        path: "/tmp/works/changed.html",
        source_path: "/tmp/topic/original.html",
        availability_status: "available",
        integrity_status: "modified",
      },
    ]);

    renderWithProviders(<WorksPage />);

    expect(await screen.findByText("保存版本已被改动")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "打开作品：被改动的工具" }),
    );

    expect(openHtmlWork).not.toHaveBeenCalled();
  });

  it("starts a new edit chat from the work menu", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorksPage />);

    await screen.findByRole("heading", { name: "作品" });
    await user.click(
      screen.getByRole("button", { name: "更多操作：季度复盘" }),
    );
    await user.click(await screen.findByText("修改作品"));

    await waitFor(() =>
      expect(mockCreateEditSession).toHaveBeenCalledWith("work-1"),
    );
  });

  it("only links works back to a public source conversation", async () => {
    expect(workSourceDestination(savedWork)).toBe("/chat/chat-1");
    expect(
      workSourceDestination({ source_chat_id: null, source_run_id: "run-2" }),
    ).toBeNull();

    const user = userEvent.setup();
    renderWithProviders(<WorksPage />);
    await screen.findByRole("heading", { name: "作品" });
    await user.click(
      screen.getByRole("button", { name: "更多操作：季度复盘" }),
    );
    expect(await screen.findByText("回到来源对话")).toBeInTheDocument();
  });

  it("states that removing a work leaves the original file alone", async () => {
    const user = userEvent.setup();
    renderWithProviders(<WorksPage />);
    await screen.findByRole("heading", { name: "作品" });
    await user.click(
      screen.getByRole("button", { name: "更多操作：季度复盘" }),
    );
    await user.click(await screen.findByText("从作品移除"));

    expect(
      await screen.findByText(/只会移除澜台保存的作品副本/),
    ).toBeInTheDocument();
  });

  it("discards a late response when the selected agent changes", async () => {
    let resolveAgentA: (value: (typeof savedWork)[]) => void = () => {};
    const agentAResponse = new Promise<(typeof savedWork)[]>((resolve) => {
      resolveAgentA = resolve;
    });
    const agentBWork = {
      ...savedWork,
      id: "work-b",
      title: "同名作品",
      size: 2048,
      source_chat_id: "chat-b",
    };
    mockAgentState.selectedAgent = "agent-a";
    mockList.mockImplementation(() =>
      mockAgentState.selectedAgent === "agent-a"
        ? agentAResponse
        : Promise.resolve([agentBWork]),
    );

    const view = renderWithProviders(<WorksPage />);
    mockAgentState.selectedAgent = "agent-b";
    view.rerender(<WorksPage />);

    const agentBCard = (
      await screen.findByRole("button", { name: "打开作品：同名作品" })
    ).closest("article");
    expect(agentBCard).toHaveTextContent("2.0 KB");
    await act(async () => {
      resolveAgentA([{ ...savedWork, title: "同名作品", size: 1024 }]);
      await agentAResponse;
    });
    expect(screen.queryByText("1.0 KB")).toBeNull();
    expect(screen.getAllByText("同名作品").length).toBeGreaterThan(0);
  });

  it("does not insert an added work into a different agent after a switch", async () => {
    let resolveAddedWork: (value: typeof savedWork) => void = () => {};
    const addedWork = new Promise<typeof savedWork>((resolve) => {
      resolveAddedWork = resolve;
    });
    mockAgentState.selectedAgent = "agent-a";
    mockList.mockResolvedValue([]);
    mockAdd.mockReturnValue(addedWork);
    const user = userEvent.setup();
    const view = renderWithProviders(<WorksPage />);
    await screen.findByRole("heading", { name: "作品" });
    await user.click(
      screen.getAllByRole("button", { name: /从最近产物添加/ })[0],
    );
    await user.click(screen.getByRole("button", { name: "加入作品" }));
    await waitFor(() => expect(mockAdd).toHaveBeenCalledTimes(1));

    mockAgentState.selectedAgent = "agent-b";
    view.rerender(<WorksPage />);
    await act(async () => {
      resolveAddedWork({ ...savedWork, id: "work-agent-a", title: "A 的作品" });
      await addedWork;
    });

    expect(screen.queryByText("A 的作品")).toBeNull();
    expect(screen.getByRole("heading", { name: "作品" })).toBeVisible();
  });
});
