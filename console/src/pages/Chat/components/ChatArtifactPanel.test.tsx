import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/common_setup";
import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  artifactPanelModeForWidth,
  compactArtifactWorkspacePath,
  artifactSystemPath,
  flattenArtifactFiles,
  isManagedWorkspaceRoot,
  normalizeArtifactPath,
  visibleArtifactNodes,
} from "./ChatArtifactPanel";
import ChatArtifactPanel from "./ChatArtifactPanel";
import { attachmentRolesFromMessages } from "./artifactExperience";
import { setActiveTopicWorkspace } from "../../../utils/topicWorkspace";
import type { CodeFileNode } from "../../../api/types";
import type { WorkItem, WorkValidation } from "../../../api/types/works";

const {
  mockGetWorkbenchState,
  mockReadFile,
  mockDownloadFile,
  mockWatchFiles,
  mockListWorks,
  mockGetChat,
  mockPreviewWork,
  mockTextPreviewWork,
  mockPreviewPageBlob,
  mockContentBlob,
  mockCreateEditCopy,
  mockAddWork,
  mockDeriveWork,
} = vi.hoisted(() => ({
  mockGetWorkbenchState: vi.fn(),
  mockReadFile: vi.fn(),
  mockDownloadFile: vi.fn(),
  mockWatchFiles: vi.fn(),
  mockListWorks: vi.fn(),
  mockGetChat: vi.fn(),
  mockPreviewWork: vi.fn(),
  mockTextPreviewWork: vi.fn(),
  mockPreviewPageBlob: vi.fn(),
  mockContentBlob: vi.fn(),
  mockCreateEditCopy: vi.fn(),
  mockAddWork: vi.fn(),
  mockDeriveWork: vi.fn(),
}));

vi.mock("../../../api/modules/coding", () => ({
  codingApi: {
    getWorkbenchState: mockGetWorkbenchState,
    readFile: mockReadFile,
    downloadFile: mockDownloadFile,
    watchFiles: mockWatchFiles,
  },
}));

vi.mock("../../../api/modules/chat", () => ({
  chatApi: { getChat: mockGetChat },
}));

vi.mock("../../../api/modules/works", () => ({
  worksApi: {
    list: mockListWorks,
    preview: mockPreviewWork,
    textPreview: mockTextPreviewWork,
    previewPageBlob: mockPreviewPageBlob,
    contentBlob: mockContentBlob,
    createEditCopy: mockCreateEditCopy,
    add: mockAddWork,
    derive: mockDeriveWork,
  },
}));

const treeNodes: CodeFileNode[] = [
  {
    name: "src",
    path: "src",
    kind: "directory",
    size: 0,
    children: [
      {
        name: "app.ts",
        path: "src/app.ts",
        kind: "file",
        size: 21,
        children: [],
      },
    ],
  },
  {
    name: "index.html",
    path: "index.html",
    kind: "file",
    size: 48,
    children: [],
  },
];

function work(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    id: "work-deck-v1",
    artifact_id: "asset-deck",
    title: "季度复盘",
    kind: "presentation",
    path: "/managed/work-deck-v1/季度复盘.pptx",
    source_path: "/tmp/lantai/topic/季度复盘.pptx",
    source_chat_id: "chat-1",
    source_run_id: "run-1",
    source_digest: "sha256:deck-v1",
    artifact_role: "primary",
    completion_bound: true,
    delivery_status: "verified",
    lineage_root_id: "work-deck-v1",
    version: 1,
    availability_status: "available",
    integrity_status: "intact",
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
    ...overrides,
  };
}

function preview(workId = "work-deck-v1", digest = "sha256:deck-v1") {
  return {
    work_id: workId,
    source_digest: digest,
    kind: "presentation" as const,
    renderer: "libreoffice-pdftoppm",
    renderer_version: "fixture-v1",
    page_count: 3,
    pages: [1, 2, 3].map((number) => ({
      number,
      width: 1600,
      height: 900,
    })),
  };
}

function validation(overrides: Partial<WorkValidation> = {}): WorkValidation {
  return {
    schema_version: 1,
    record_id: "validation:run-1:attempt-1:work-deck-v1:sha256:deck-v1",
    status: "passed",
    source_run_id: "run-1",
    source_attempt_id: "attempt-1",
    work_id: "work-deck-v1",
    artifact_id: "asset-deck",
    artifact_version: 1,
    artifact_digest: "sha256:deck-v1",
    validator_id: "runtime-completion-gate",
    validator_version: "2",
    ruleset_id: "delivery-readiness",
    ruleset_version: "2",
    checked_at: "2026-09-21T00:00:00Z",
    required_scope: ["deliverable_openable"],
    observed_scope: ["deliverable_openable"],
    checks: [
      {
        id: "deliverable_openable",
        outcome: "passed",
        required: true,
        detail: "The presentation package opens as OOXML.",
        evidence_refs: [],
      },
    ],
    invalidation_rules: [
      "artifact_digest_changed",
      "validator_version_changed",
      "ruleset_version_changed",
    ],
    ...overrides,
  };
}

describe("ChatArtifactPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    window.localStorage.clear();
    window.electronAPI = {
      platform: "darwin",
      apiBaseUrl: "",
      isDesktop: true,
      openOfficeEditor: vi.fn().mockResolvedValue({ success: true }),
      openDocxEditor: vi.fn().mockResolvedValue({ success: true }),
      openPath: vi.fn().mockResolvedValue({ success: true }),
      openHtmlWork: vi.fn().mockResolvedValue({ success: true }),
      revealPath: vi.fn().mockResolvedValue({ success: true }),
      openWith: vi.fn().mockResolvedValue({ success: true }),
    };
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:artifact-preview"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
    setActiveTopicWorkspace({
      agentId: "default",
      sessionId: "console:chat-1",
      chatId: "chat-1",
      topicWorkspaceDir: "/tmp/lantai/topic",
      effectiveWorkspaceDir: "/tmp/lantai/topic",
    });
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "三维迷宫",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: treeNodes,
        total_entries: 3,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });
    mockDownloadFile.mockResolvedValue(new Blob(["<html></html>"]));
    mockReadFile.mockImplementation(async (path: string) => ({
      path,
      content:
        path === "index.html"
          ? "<!doctype html><title>迷宫</title><h1>三维迷宫</h1>"
          : "export const ready = true;",
      size: 48,
      modified_time: "2026-07-27T00:00:00Z",
      sha256: `sha-${path}`,
      encoding: "utf-8",
      binary: false,
      truncated: false,
    }));
    mockWatchFiles.mockResolvedValue(undefined);
    mockListWorks.mockResolvedValue([]);
    mockGetChat.mockResolvedValue({
      chat_id: "chat-1",
      messages: [],
      meta: {},
      runtime_state: null,
    });
    mockPreviewPageBlob.mockResolvedValue(
      new Blob(["page"], { type: "image/png" }),
    );
    mockPreviewWork.mockResolvedValue(preview());
    mockTextPreviewWork.mockResolvedValue({
      work_id: "work-report-v1",
      source_digest: "sha256:report-v1",
      kind: "document",
      method: "office_xml",
      content_type: "text/plain",
      content: "项目周报\n本周完成成果面板正文预览。",
      truncated: false,
      message: "已通过内置 Office Open XML 结构化读取正文。",
    });
    mockContentBlob.mockResolvedValue(
      new Blob(["content"], { type: "text/plain" }),
    );
    mockCreateEditCopy.mockResolvedValue({
      work_id: "work-1",
      asset_id: "artifact-1",
      edit_path: "/tmp/lantai/topic/.lantai/works/edits/edit-1/deck.pptx",
      base_version: 1,
      base_digest: "deck-digest",
      lineage_root_id: "work-1",
    });
  });

  it("binds a selected presentation page to one prepared agent edit", async () => {
    const user = userEvent.setup();
    const onPrepareArtifactEdit = vi.fn();
    mockListWorks.mockResolvedValue([work()]);
    mockGetChat.mockResolvedValue({
      chat_id: "chat-1",
      messages: [],
      meta: {},
      runtime_state: {
        run_id: "run-1",
        status: "completed",
        delivery_status: "verified",
      },
    });

    renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        onClose={vi.fn()}
        onPrepareArtifactEdit={onPrepareArtifactEdit}
      />,
    );

    expect(
      await screen.findByRole("heading", { name: "季度复盘" }),
    ).toBeVisible();
    expect(mockListWorks).toHaveBeenCalledWith("chat-1");
    await waitFor(() =>
      expect(mockPreviewWork).toHaveBeenCalledWith("work-deck-v1", "chat-1"),
    );
    const status = screen.getByRole("region", { name: "成果状态" });
    await waitFor(() => expect(status).toHaveTextContent("内容预览可用"));
    expect(status).toHaveTextContent("检查未提供独立检查记录");
    expect(status).toHaveTextContent("交付已正式交付");
    expect(status).toHaveTextContent("任务已结束");

    await user.click(screen.getByRole("button", { name: "第 3 页" }));
    // The preview effect may replace the image between findByRole resolving
    // and the assertion. Check a fresh DOM node and visibility atomically.
    await waitFor(() => {
      expect(
        screen.getByRole("img", { name: "季度复盘 第 3 页" }),
      ).toBeVisible();
    });
    expect(mockPreviewPageBlob).toHaveBeenLastCalledWith(
      "work-deck-v1",
      3,
      "sha256:deck-v1",
      "chat-1",
    );
    await user.click(screen.getByRole("button", { name: /让沈行知修改/ }));

    await waitFor(() => expect(onPrepareArtifactEdit).toHaveBeenCalledTimes(1));
    expect(mockCreateEditCopy).toHaveBeenCalledTimes(1);
    expect(onPrepareArtifactEdit).toHaveBeenCalledWith({
      title: "季度复盘",
      target: {
        schema_version: 1,
        chat_id: "chat-1",
        asset_id: "asset-deck",
        work_id: "work-deck-v1",
        edit_path: "/tmp/lantai/topic/.lantai/works/edits/edit-1/deck.pptx",
        base_revision: { version: 1, digest: "sha256:deck-v1" },
        selection: { kind: "page", page: 3 },
      },
    });
  });

  it("exports a presentation as an explicitly related PDF without replacing the primary", async () => {
    const user = userEvent.setup();
    const presentation = work();
    const pdf = work({
      id: "work-deck-pdf",
      artifact_id: "asset-deck-pdf",
      title: "季度复盘（PDF）",
      kind: "pdf",
      path: "/managed/work-deck-pdf/季度复盘.pptx.pdf",
      source_path: "/derived/季度复盘.pptx.pdf",
      source_digest: "sha256:pdf-work-digest",
      artifact_role: null,
      completion_bound: false,
      delivery_status: null,
      lineage_root_id: "work-deck-pdf",
      derivation: {
        schema_version: 1,
        operation: "office_conversion",
        target_format: "pdf",
        source_work_id: presentation.id,
        source_artifact_id: presentation.artifact_id,
        source_version: presentation.version,
        source_digest: presentation.source_digest || "",
        output_digest: "sha256:pdf-work-digest",
        converter_output_digest: "sha256:pdf-byte-digest",
        converter: "libreoffice",
      },
    });
    mockListWorks.mockResolvedValue([presentation]);
    mockDeriveWork.mockResolvedValue(pdf);

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    expect(
      await screen.findByRole("heading", { name: "季度复盘" }),
    ).toBeVisible();
    mockListWorks.mockResolvedValue([presentation, pdf]);
    await user.click(screen.getByRole("button", { name: "导出并关联 PDF" }));

    await waitFor(() => expect(mockDeriveWork).toHaveBeenCalledTimes(1));
    expect(mockDeriveWork).toHaveBeenCalledWith("work-deck-v1", {
      target_format: "pdf",
      chat_id: "chat-1",
    });
    expect(
      await screen.findByRole("heading", { name: "季度复盘" }),
    ).toBeVisible();
    expect(await screen.findByText("季度复盘（PDF）")).toBeVisible();
    expect(screen.getByText("派生 PDF（来自 V1） · V1 · pdf")).toBeVisible();
  });

  it.each([
    ["passed", "必需检查范围通过"],
    ["failed", "所列检查有问题"],
    ["partial", "仅完成部分检查"],
    ["not_run", "尚未执行检查"],
    ["error", "检查执行出错"],
  ] as const)("shows the exact %s validation status", async (status, label) => {
    mockListWorks.mockResolvedValue([
      work({ validation: validation({ status }) }),
    ]);

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    const outcomeStatus = await screen.findByRole("region", {
      name: "成果状态",
    });
    expect(outcomeStatus).toHaveTextContent(`检查${label}`);
  });

  it("lists the exact covered scope and every recorded check outcome", async () => {
    mockListWorks.mockResolvedValue([
      work({
        validation: validation({
          status: "partial",
          required_scope: ["deliverable_openable", "visual_quality"],
          observed_scope: ["deliverable_openable", "release_approval"],
          checks: [
            {
              id: "deliverable_openable",
              outcome: "passed",
              required: true,
              detail: "The presentation package opens as OOXML.",
              evidence_refs: [],
            },
            {
              id: "visual_quality",
              outcome: "not_run",
              required: true,
              detail: "No renderer is available in this environment.",
              evidence_refs: [],
            },
            {
              id: "release_approval",
              outcome: "waived",
              required: false,
              detail: "Release approval is outside this candidate run.",
              evidence_refs: ["approval:release-not-requested"],
            },
          ],
        }),
      }),
    ]);

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    const detail = await screen.findByRole("region", {
      name: "检查范围与结果",
    });
    expect(detail).toHaveTextContent("runtime-completion-gate v2");
    expect(detail).toHaveTextContent("delivery-readiness v2");
    expect(detail).toHaveTextContent("已覆盖deliverable_openable");
    expect(detail).toHaveTextContent("待检查visual_quality");
    expect(detail).toHaveTextContent("deliverable_openable通过必需");
    expect(detail).toHaveTextContent("visual_quality未检查必需");
    expect(detail).toHaveTextContent("release_approval已豁免可选");
    expect(detail).toHaveTextContent("证据：approval:release-not-requested");
  });

  it("shows a managed document body without opening an editor", async () => {
    mockListWorks.mockResolvedValue([
      work({
        id: "work-report-v1",
        artifact_id: "asset-report",
        title: "项目周报",
        kind: "document",
        path: "/managed/work-report-v1/项目周报.docx",
        source_path: "/tmp/lantai/topic/项目周报.docx",
        source_digest: "sha256:report-v1",
      }),
    ]);

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    expect(
      await screen.findByRole("heading", { name: "项目周报" }),
    ).toBeVisible();
    expect(await screen.findByLabelText("项目周报 正文预览")).toHaveTextContent(
      "本周完成成果面板正文预览。",
    );
    expect(mockTextPreviewWork).toHaveBeenCalledWith(
      "work-report-v1",
      "chat-1",
    );
    expect(mockContentBlob).not.toHaveBeenCalled();
    expect(window.electronAPI?.openDocxEditor).not.toHaveBeenCalled();
  });

  it.each([
    ["audio", "访谈录音.mp3"],
    ["video", "演示录像.mp4"],
    ["file", "素材归档.zip"],
  ] as const)(
    "honestly degrades an unsupported %s outcome without reading or executing it",
    async (kind, title) => {
      mockListWorks.mockResolvedValue([
        work({
          id: `work-${kind}-v1`,
          artifact_id: `asset-${kind}`,
          title,
          kind,
          path: `/managed/work-${kind}-v1/${title}`,
          source_path: `/tmp/lantai/topic/${title}`,
          source_digest: `sha256:${kind}-v1`,
          lineage_root_id: `work-${kind}-v1`,
        }),
      ]);

      renderWithProviders(
        <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
      );

      expect(
        await screen.findByText("此格式暂不支持内嵌预览"),
      ).toBeInTheDocument();
      expect(
        screen.getByText(
          "成果仍可下载；支持的 Office 格式可用澜台编辑器打开。",
        ),
      ).toBeInTheDocument();
      expect(mockContentBlob).not.toHaveBeenCalled();
      expect(mockPreviewWork).not.toHaveBeenCalled();
      expect(mockTextPreviewWork).not.toHaveBeenCalled();
      expect(window.electronAPI?.openPath).not.toHaveBeenCalled();
    },
  );

  it("drops a slow edit-copy result after switching canonical chats", async () => {
    const user = userEvent.setup();
    const onPrepareArtifactEdit = vi.fn();
    let resolveEdit:
      | ((value: {
          work_id: string;
          asset_id: string;
          edit_path: string;
          base_version: number;
          base_digest: string;
          lineage_root_id: string;
        }) => void)
      | undefined;
    mockCreateEditCopy.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveEdit = resolve;
      }),
    );
    mockListWorks.mockImplementation(async (chatId: string) =>
      chatId === "chat-1" ? [work()] : [],
    );
    mockGetChat.mockImplementation(async (chatId: string) => ({
      chat_id: chatId,
      messages: [],
      meta: {},
      runtime_state: null,
    }));

    const { rerender } = renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        onClose={vi.fn()}
        onPrepareArtifactEdit={onPrepareArtifactEdit}
      />,
    );
    await screen.findByRole("heading", { name: "季度复盘" });
    await user.click(screen.getByRole("button", { name: /让沈行知修改/ }));

    rerender(
      <ChatArtifactPanel
        open
        chatId="chat-2"
        onClose={vi.fn()}
        onPrepareArtifactEdit={onPrepareArtifactEdit}
      />,
    );
    await waitFor(() => expect(mockListWorks).toHaveBeenCalledWith("chat-2"));
    resolveEdit?.({
      work_id: "work-deck-v1",
      asset_id: "asset-deck",
      edit_path: "/tmp/lantai/topic/.lantai/works/edits/late/deck.pptx",
      base_version: 1,
      base_digest: "sha256:deck-v1",
      lineage_root_id: "work-deck-v1",
    });

    await waitFor(() => expect(onPrepareArtifactEdit).not.toHaveBeenCalled());
  });

  it("restores the selected page for the same Work after reopening", async () => {
    const user = userEvent.setup();
    const deck = work();
    window.localStorage.setItem(
      "lantai:artifact-workbench:v1:chat-1",
      JSON.stringify({
        openPaths: [],
        activePath: null,
        expanded: [],
        view: "files",
        selectedWorkId: deck.id,
        selectedPageByWorkId: { [deck.id]: 3 },
      }),
    );
    mockListWorks.mockResolvedValue([deck]);

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await waitFor(() => {
      expect(
        screen.getByRole("img", { name: "季度复盘 第 3 页" }),
      ).toBeVisible();
    });
    expect(mockPreviewPageBlob).toHaveBeenLastCalledWith(
      deck.id,
      3,
      deck.source_digest,
      "chat-1",
    );

    await user.click(screen.getByRole("button", { name: "第 2 页" }));
    await waitFor(() => {
      const saved = JSON.parse(
        window.localStorage.getItem("lantai:artifact-workbench:v1:chat-1") ||
          "{}",
      ) as { selectedPageByWorkId?: Record<string, number> };
      expect(saved.selectedPageByWorkId?.[deck.id]).toBe(2);
    });
  });

  it("does not promote a generation script without a Work relationship", async () => {
    const user = userEvent.setup();
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "演示文稿任务",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: [
          {
            name: "build-deck.cjs",
            path: "build-deck.cjs",
            kind: "file",
            size: 42,
            children: [],
          },
        ],
        total_entries: 1,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    expect(await screen.findByText("暂无可确认的主成果")).toBeVisible();
    expect(screen.queryByText("已验证")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "其他文件" }));
    expect(await screen.findByText("build-deck.cjs")).toBeVisible();
  });

  it("drops a cross-chat cached Work selection without deleting the original file tab", async () => {
    const user = userEvent.setup();
    setActiveTopicWorkspace({
      agentId: "default",
      sessionId: "console:chat-b",
      chatId: "chat-b",
      topicWorkspaceDir: "/tmp/lantai/topic-b",
      effectiveWorkspaceDir: "/tmp/lantai/topic-b",
    });
    window.localStorage.setItem(
      "lantai:artifact-workbench:v1:chat-b",
      JSON.stringify({
        openPaths: ["legacy-output.html"],
        activePath: "legacy-output.html",
        expanded: [],
        view: "preview",
        selectedWorkId: "work-chat-a",
        selectedPageByWorkId: { "work-chat-a": 3 },
      }),
    );
    mockListWorks.mockResolvedValue([
      work({
        id: "work-chat-a",
        title: "A 会话旧成果",
        source_chat_id: "chat-a",
      }),
    ]);
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic-b",
      workspace_name: "会话 B",
      file_tree: {
        root_path: "/tmp/lantai/topic-b",
        root_name: "topic-b",
        path: "",
        children: [
          {
            name: "legacy-output.html",
            path: "legacy-output.html",
            kind: "file",
            size: 48,
            children: [],
          },
        ],
        total_entries: 1,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-b" onClose={vi.fn()} />,
    );

    expect(await screen.findByText("暂无可确认的主成果")).toBeVisible();
    expect(screen.queryByText("A 会话旧成果")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "其他文件" }));
    expect((await screen.findAllByText("legacy-output.html"))[0]).toBeVisible();
    await waitFor(() => {
      const saved = JSON.parse(
        window.localStorage.getItem("lantai:artifact-workbench:v1:chat-b") ||
          "{}",
      ) as { openPaths?: string[]; selectedWorkId?: string | null };
      expect(saved.openPaths).toEqual(["legacy-output.html"]);
      expect(saved.selectedWorkId).toBeNull();
    });
  });

  it("keeps candidate and unclassified Works visible without claiming a primary outcome", async () => {
    const user = userEvent.setup();
    mockListWorks.mockResolvedValue([
      work({
        id: "work-candidate-v1",
        artifact_id: "asset-candidate",
        title: "候选汇报",
        artifact_role: "candidate",
        completion_bound: false,
        delivery_status: null,
        lineage_root_id: "work-candidate-v1",
      }),
      work({
        id: "work-unclassified-v1",
        artifact_id: "asset-unclassified",
        title: "背景数据",
        kind: "file",
        artifact_role: null,
        completion_bound: false,
        delivery_status: null,
        lineage_root_id: "work-unclassified-v1",
      }),
    ]);

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    expect(await screen.findByText("暂无可确认的主成果")).toBeVisible();
    const candidateButton = screen.getByRole("button", {
      name: /候选汇报.*候选成果（角色尚未确认为主交付）/,
    });
    expect(candidateButton).toBeVisible();
    expect(
      screen.getByRole("button", { name: /背景数据.*未分类/ }),
    ).toBeVisible();
    expect(screen.queryByText("当前成果")).not.toBeInTheDocument();

    await user.click(candidateButton);
    expect(await screen.findByText("当前成果")).toBeVisible();
    expect(screen.getByRole("heading", { name: "候选汇报" })).toBeVisible();
  });

  it("refreshes Works on file watch without stealing an explicit unclassified selection", async () => {
    const user = userEvent.setup();
    const script = work({
      id: "work-script-v1",
      artifact_id: "asset-script",
      title: "生成脚本",
      kind: "file",
      path: "/managed/work-script-v1/build-deck.cjs",
      source_path: "/tmp/lantai/topic/build-deck.cjs",
      source_digest: "sha256:script-v1",
      artifact_role: null,
      completion_bound: false,
      delivery_status: null,
      lineage_root_id: "work-script-v1",
    });
    const deck = work({
      id: "work-deck-v1",
      artifact_id: "asset-deck",
      title: "有效演示文稿",
    });
    let currentWorks = [script];
    mockListWorks.mockImplementation(async () => currentWorks);

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await user.click(
      await screen.findByRole("button", { name: /生成脚本.*未分类/ }),
    );
    expect(screen.getByRole("heading", { name: "生成脚本" })).toBeVisible();

    currentWorks = [script, deck];
    const watchOptions = mockWatchFiles.mock.calls.at(-1)?.[0] as
      | { onChange?: () => void }
      | undefined;
    expect(watchOptions?.onChange).toBeTypeOf("function");
    watchOptions?.onChange?.();

    await waitFor(() => expect(mockListWorks).toHaveBeenCalledTimes(2), {
      timeout: 1_500,
    });
    expect(screen.getByRole("heading", { name: "生成脚本" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: /有效演示文稿.*主成果/ }),
    ).toBeVisible();
  });

  it("keeps the last scoped file tree when a refresh temporarily fails", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "其他文件" }));
    expect(await screen.findByText("index.html")).toBeVisible();

    mockGetWorkbenchState.mockRejectedValueOnce(new Error("暂时不可用"));
    await user.click(screen.getByRole("button", { name: "刷新成果与文件" }));

    expect(await screen.findByText(/本次刷新失败.*暂时不可用/)).toBeVisible();
    expect(screen.getByText("index.html")).toBeVisible();
  });

  it("shows an explicitly recorded cjs Work as a legitimate outcome", async () => {
    mockListWorks.mockResolvedValue([
      work({
        id: "work-script-v1",
        artifact_id: "asset-script",
        title: "可复用生成脚本",
        kind: "file",
        path: "/managed/work-script-v1/build-deck.cjs",
        source_path: "/tmp/lantai/topic/build-deck.cjs",
        source_digest: "sha256:script-v1",
        lineage_root_id: "work-script-v1",
      }),
    ]);
    mockContentBlob.mockResolvedValue(
      new Blob(["module.exports = async function buildDeck() {};"], {
        type: "text/javascript",
      }),
    );

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    expect(
      await screen.findByRole("heading", { name: "可复用生成脚本" }),
    ).toBeVisible();
    expect(
      await screen.findByText(
        "module.exports = async function buildDeck() {};",
      ),
    ).toBeVisible();
    expect(screen.getAllByText("V1")[0]).toBeVisible();
  });

  it("saves a manual Office edit as the next version in the same lineage", async () => {
    const user = userEvent.setup();
    let currentWorks: WorkItem[];
    const first = work();
    const second = work({
      id: "work-deck-v2",
      parent_work_id: first.id,
      parent_digest: first.source_digest,
      lineage_root_id: first.id,
      version: 2,
      source_digest: "sha256:deck-v2",
      updated_at: "2026-09-20T01:00:00Z",
    });
    window.localStorage.setItem(
      "lantai:artifact-workbench:v1:chat-1",
      JSON.stringify({
        openPaths: [],
        activePath: null,
        expanded: [],
        view: "files",
        selectedWorkId: first.id,
      }),
    );
    currentWorks = [first];
    mockListWorks.mockImplementation(async () => currentWorks);
    mockAddWork.mockResolvedValue(second);
    mockPreviewWork.mockImplementation(async (workId: string) =>
      preview(
        workId,
        workId === second.id ? "sha256:deck-v2" : "sha256:deck-v1",
      ),
    );

    renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        onClose={vi.fn()}
        onPrepareArtifactEdit={vi.fn()}
      />,
    );

    await screen.findByRole("heading", { name: "季度复盘" });
    await user.click(screen.getByRole("button", { name: "用澜台编辑" }));
    await waitFor(() =>
      expect(window.electronAPI?.openOfficeEditor).toHaveBeenCalledWith(
        "/tmp/lantai/topic/.lantai/works/edits/edit-1/deck.pptx",
        expect.objectContaining({ source_chat_id: "chat-1" }),
      ),
    );
    currentWorks = [first, second];
    await user.click(
      await screen.findByRole("button", { name: "编辑保存后，存为新版本" }),
    );

    await waitFor(() => expect(mockAddWork).toHaveBeenCalledTimes(1));
    expect(mockAddWork).toHaveBeenCalledWith({
      path: "/tmp/lantai/topic/.lantai/works/edits/edit-1/deck.pptx",
      title: "季度复盘",
      source_chat_id: "chat-1",
      source_run_id: null,
    });
    await waitFor(() => expect(screen.getAllByText("V2")[0]).toBeVisible());
    expect(screen.getByRole("heading", { name: "季度复盘" })).toBeVisible();
  });

  it("keeps a viewed revision stable and exposes real lineage history", async () => {
    const user = userEvent.setup();
    let currentWorks: WorkItem[];
    const first = work();
    const second = work({
      id: "work-deck-v2",
      parent_work_id: first.id,
      parent_digest: first.source_digest,
      lineage_root_id: first.id,
      version: 2,
      source_digest: "sha256:deck-v2",
      created_at: "2026-09-20T01:00:00Z",
      updated_at: "2026-09-20T01:00:00Z",
    });
    currentWorks = [first];
    mockListWorks.mockImplementation(async () => currentWorks);

    renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        onClose={vi.fn()}
        onPrepareArtifactEdit={vi.fn()}
      />,
    );

    await screen.findByRole("heading", { name: "季度复盘" });
    await waitFor(() => {
      const saved = JSON.parse(
        window.localStorage.getItem("lantai:artifact-workbench:v1:chat-1") ||
          "{}",
      ) as { selectedWorkId?: string };
      expect(saved.selectedWorkId).toBe(first.id);
    });

    currentWorks = [first, second];
    await user.click(screen.getByRole("button", { name: "刷新成果与文件" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "预览与下载保持 V1 的精确字节",
    );
    expect(screen.getByRole("button", { name: /让沈行知修改/ })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "下载已交付版本" }),
    ).toBeVisible();
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    await user.click(screen.getByRole("button", { name: "下载已交付版本" }));
    await waitFor(() =>
      expect(mockContentBlob).toHaveBeenCalledWith(first.id, "chat-1"),
    );
    expect(anchorClick).toHaveBeenCalledTimes(1);
    anchorClick.mockRestore();

    await user.click(screen.getByText("版本与来源详情"));
    const history = screen.getByRole("list", { name: "成果版本历史" });
    expect(within(history).getAllByRole("listitem")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "切到最新版本修改" }));
    await waitFor(() => expect(screen.getAllByText("V2")[0]).toBeVisible());
    expect(screen.getByRole("button", { name: /让沈行知修改/ })).toBeEnabled();
  });

  it("does not let a slow V1 preview replace the selected V2 preview", async () => {
    const user = userEvent.setup();
    let resolveV1Preview:
      | ((value: ReturnType<typeof preview>) => void)
      | undefined;
    const first = work();
    const second = work({
      id: "work-deck-v2",
      parent_work_id: first.id,
      parent_digest: first.source_digest,
      lineage_root_id: first.id,
      version: 2,
      source_digest: "sha256:deck-v2",
      created_at: "2026-09-20T01:00:00Z",
      updated_at: "2026-09-20T01:00:00Z",
    });
    let currentWorks = [first];
    mockListWorks.mockImplementation(async () => currentWorks);
    mockPreviewWork.mockImplementation((workId: string) => {
      if (workId === first.id) {
        return new Promise((resolve) => {
          resolveV1Preview = resolve;
        });
      }
      return Promise.resolve(preview(second.id, second.source_digest));
    });

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await screen.findByRole("heading", { name: "季度复盘" });
    await waitFor(() =>
      expect(mockPreviewWork).toHaveBeenCalledWith(first.id, "chat-1"),
    );

    currentWorks = [first, second];
    await user.click(screen.getByRole("button", { name: "刷新成果与文件" }));
    await user.click(screen.getByText("版本与来源详情"));
    await user.click(screen.getByRole("button", { name: "切到最新版本修改" }));

    await waitFor(() =>
      expect(mockPreviewPageBlob).toHaveBeenCalledWith(
        second.id,
        1,
        second.source_digest,
        "chat-1",
      ),
    );
    resolveV1Preview?.(preview(first.id, first.source_digest));
    await Promise.resolve();
    await Promise.resolve();

    expect(
      mockPreviewPageBlob.mock.calls.some(([workId]) => workId === first.id),
    ).toBe(false);
    expect(screen.getAllByText("V2")[0]).toBeVisible();
  });

  it("does not restore a preview after the selected revision loses permission", async () => {
    const user = userEvent.setup();
    let resolvePreview:
      | ((value: ReturnType<typeof preview>) => void)
      | undefined;
    const available = work();
    const permissionDenied = work({
      availability_status: "permission_denied",
      integrity_status: "unknown",
    });
    let currentWorks = [available];
    mockListWorks.mockImplementation(async () => currentWorks);
    mockPreviewWork.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePreview = resolve;
        }),
    );

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await screen.findByRole("heading", { name: "季度复盘" });
    await waitFor(() =>
      expect(mockPreviewWork).toHaveBeenCalledWith(available.id, "chat-1"),
    );

    currentWorks = [permissionDenied];
    await user.click(screen.getByRole("button", { name: "刷新成果与文件" }));

    expect(await screen.findByText("预览不可用")).toBeVisible();
    expect(screen.getByText("当前版本不可用或完整性未确认")).toBeVisible();

    await act(async () => {
      resolvePreview?.(preview(available.id, available.source_digest));
      await Promise.resolve();
    });

    expect(mockPreviewPageBlob).not.toHaveBeenCalled();
    expect(screen.getByText("预览不可用")).toBeVisible();
    expect(
      screen.queryByRole("img", { name: "季度复盘 第 1 页" }),
    ).not.toBeInTheDocument();
  });

  it("does not create an editable copy when no desktop editor is available", async () => {
    const user = userEvent.setup();
    mockListWorks.mockResolvedValue([work()]);
    window.electronAPI = { ...window.electronAPI, isDesktop: false };

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await screen.findByRole("heading", { name: "季度复盘" });
    await user.click(screen.getByRole("button", { name: "用澜台编辑" }));
    expect(mockCreateEditCopy).not.toHaveBeenCalled();
  });

  it("ignores a slow outcome response after the panel switches chats", async () => {
    let resolveSlowWorks: ((value: WorkItem[]) => void) | undefined;
    let resolveSlowChat: ((value: unknown) => void) | undefined;
    const slowWorks = new Promise<WorkItem[]>((resolve) => {
      resolveSlowWorks = resolve;
    });
    const slowChat = new Promise((resolve) => {
      resolveSlowChat = resolve;
    });
    const workA = work({ title: "A 会话旧成果", source_chat_id: "chat-a" });
    const workB = work({
      id: "work-b",
      artifact_id: "asset-b",
      title: "B 会话当前成果",
      source_chat_id: "chat-b",
      source_digest: "sha256:b",
      lineage_root_id: "work-b",
    });
    mockListWorks.mockReturnValueOnce(slowWorks).mockResolvedValue([workB]);
    mockGetChat.mockReturnValueOnce(slowChat).mockResolvedValue({
      chat_id: "chat-b",
      messages: [],
      meta: {},
      runtime_state: null,
    });

    const rendered = renderWithProviders(
      <ChatArtifactPanel open chatId="chat-a" onClose={vi.fn()} />,
    );
    rendered.rerender(
      <ChatArtifactPanel open chatId="chat-b" onClose={vi.fn()} />,
    );

    expect(
      await screen.findByRole("heading", { name: "B 会话当前成果" }),
    ).toBeVisible();
    resolveSlowWorks?.([workA]);
    resolveSlowChat?.({
      chat_id: "chat-a",
      messages: [],
      meta: {},
      runtime_state: null,
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(
      screen.getByRole("heading", { name: "B 会话当前成果" }),
    ).toBeVisible();
    expect(screen.queryByText("A 会话旧成果")).not.toBeInTheDocument();
  });

  it("does not let a slow chat A tree replace the resolved chat B tree", async () => {
    const user = userEvent.setup();
    let resolveSlowTree: ((value: unknown) => void) | undefined;
    const slowTree = new Promise((resolve) => {
      resolveSlowTree = resolve;
    });
    const treeResponse = (chat: "a" | "b") => ({
      workspace_dir: `/tmp/lantai/topic-${chat}`,
      workspace_name: `会话 ${chat.toUpperCase()}`,
      file_tree: {
        root_path: `/tmp/lantai/topic-${chat}`,
        root_name: `topic-${chat}`,
        path: "",
        children: [
          {
            name: `${chat}-only.md`,
            path: `${chat}-only.md`,
            kind: "file" as const,
            size: 12,
            children: [],
          },
        ],
        total_entries: 1,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });
    setActiveTopicWorkspace({
      agentId: "default",
      sessionId: "console:chat-a",
      chatId: "chat-a",
      topicWorkspaceDir: "/tmp/lantai/topic-a",
      effectiveWorkspaceDir: "/tmp/lantai/topic-a",
    });
    mockGetWorkbenchState
      .mockReturnValueOnce(slowTree)
      .mockResolvedValue(treeResponse("b"));

    const rendered = renderWithProviders(
      <ChatArtifactPanel open chatId="chat-a" onClose={vi.fn()} />,
    );
    await waitFor(() => expect(mockGetWorkbenchState).toHaveBeenCalledTimes(1));
    expect(mockGetWorkbenchState).toHaveBeenNthCalledWith(1, {
      workspaceDir: "/tmp/lantai/topic-a",
      chatId: "chat-a",
      treeDepth: 4,
      treeLimit: 500,
    });

    setActiveTopicWorkspace({
      agentId: "default",
      sessionId: "console:chat-b",
      chatId: "chat-b",
      topicWorkspaceDir: "/tmp/lantai/topic-b",
      effectiveWorkspaceDir: "/tmp/lantai/topic-b",
    });
    rendered.rerender(
      <ChatArtifactPanel open chatId="chat-b" onClose={vi.fn()} />,
    );
    await user.click(screen.getByRole("button", { name: "其他文件" }));

    expect(await screen.findByText("b-only.md")).toBeVisible();
    expect(mockGetWorkbenchState).toHaveBeenNthCalledWith(2, {
      workspaceDir: "/tmp/lantai/topic-b",
      chatId: "chat-b",
      treeDepth: 4,
      treeLimit: 500,
    });
    expect(screen.getByTitle("/tmp/lantai/topic-b")).toBeVisible();
    await act(async () => {
      resolveSlowTree?.(treeResponse("a"));
      await Promise.resolve();
    });

    expect(screen.getByText("b-only.md")).toBeVisible();
    expect(screen.getByTitle("/tmp/lantai/topic-b")).toBeVisible();
    expect(screen.queryByText("a-only.md")).not.toBeInTheDocument();
  });

  it("does not let a slow chat A file read switch chat B back to the file list", async () => {
    const user = userEvent.setup();
    let resolveSlowRead: ((value: Record<string, unknown>) => void) | undefined;
    const slowRead = new Promise<Record<string, unknown>>((resolve) => {
      resolveSlowRead = resolve;
    });
    const treeResponse = (chat: "a" | "b") => ({
      workspace_dir: `/tmp/lantai/topic-${chat}`,
      workspace_name: `会话 ${chat.toUpperCase()}`,
      file_tree: {
        root_path: `/tmp/lantai/topic-${chat}`,
        root_name: `topic-${chat}`,
        path: "",
        children: [
          {
            name: `${chat}-only.md`,
            path: `${chat}-only.md`,
            kind: "file" as const,
            size: 12,
            children: [],
          },
        ],
        total_entries: 1,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });
    for (const chat of ["a", "b"] as const) {
      window.localStorage.setItem(
        `lantai:artifact-workbench:v1:chat-${chat}`,
        JSON.stringify({
          openPaths: [`${chat}-only.md`],
          activePath: `${chat}-only.md`,
          expanded: [],
          view: "source",
        }),
      );
    }
    mockGetWorkbenchState.mockImplementation(
      ({ chatId: requestedChatId }: { chatId?: string }) =>
        Promise.resolve(treeResponse(requestedChatId === "chat-a" ? "a" : "b")),
    );
    mockReadFile.mockImplementation((path: string) => {
      if (path === "a-only.md") return slowRead;
      return Promise.resolve({
        path,
        content: "B CURRENT CONTENT",
        size: 17,
        modified_time: "2026-09-21T00:00:00Z",
        sha256: "sha-b-current",
        encoding: "utf-8",
        binary: false,
        truncated: false,
      });
    });
    setActiveTopicWorkspace({
      agentId: "default",
      sessionId: "console:chat-a",
      chatId: "chat-a",
      topicWorkspaceDir: "/tmp/lantai/topic-a",
      effectiveWorkspaceDir: "/tmp/lantai/topic-a",
    });

    const rendered = renderWithProviders(
      <ChatArtifactPanel open chatId="chat-a" onClose={vi.fn()} />,
    );
    await waitFor(() =>
      expect(mockReadFile).toHaveBeenCalledWith("a-only.md", {
        workspaceDir: "/tmp/lantai/topic-a",
      }),
    );

    setActiveTopicWorkspace({
      agentId: "default",
      sessionId: "console:chat-b",
      chatId: "chat-b",
      topicWorkspaceDir: "/tmp/lantai/topic-b",
      effectiveWorkspaceDir: "/tmp/lantai/topic-b",
    });
    rendered.rerender(
      <ChatArtifactPanel open chatId="chat-b" onClose={vi.fn()} />,
    );
    await waitFor(() =>
      expect(mockReadFile).toHaveBeenCalledWith("b-only.md", {
        workspaceDir: "/tmp/lantai/topic-b",
      }),
    );
    await user.click(screen.getByRole("button", { name: "其他文件" }));
    expect(await screen.findByText("B CURRENT CONTENT")).toBeVisible();
    expect(screen.getByRole("button", { name: "内容" })).toHaveAttribute(
      "aria-current",
      "page",
    );

    await act(async () => {
      resolveSlowRead?.({
        path: "a-only.md",
        content: "A STALE CONTENT",
        size: 15,
        modified_time: "2026-09-21T00:00:00Z",
        sha256: "sha-a-stale",
        encoding: "utf-8",
        binary: false,
        truncated: false,
      });
      await Promise.resolve();
    });

    expect(screen.getByText("B CURRENT CONTENT")).toBeVisible();
    expect(screen.getByRole("button", { name: "内容" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.queryByText("A STALE CONTENT")).not.toBeInTheDocument();
  });

  it("flattens nested files and normalizes absolute artifact paths", () => {
    expect(flattenArtifactFiles(treeNodes).map((node) => node.path)).toEqual([
      "src/app.ts",
      "index.html",
    ]);
    expect(
      normalizeArtifactPath(
        "/tmp/lantai/topic/src/app.ts:L12-18",
        "/tmp/lantai/topic",
      ),
    ).toBe("src/app.ts");
    expect(artifactSystemPath("/tmp/lantai/topic", "src/app.ts")).toBe(
      "/tmp/lantai/topic/src/app.ts",
    );
    expect(artifactSystemPath("/tmp/lantai/topic", "../outside.txt")).toBe("");
  });

  it("accepts attachment roles only from canonical user uploads and message text", () => {
    const context =
      '<lantai_attachment_roles>\n[{"name":"客户简报.docx","role":"reference"}]\n</lantai_attachment_roles>';
    const storedName = "55aa55aa55aa55aa55aa55aa55aa55aa_客户简报.docx";
    const canonicalPath = `/Users/example/.lantai/media/${storedName}`;
    const trustedPaths = [canonicalPath];

    expect(
      attachmentRolesFromMessages(
        [
          { role: "assistant", content: context },
          { role: "tool", content: context },
        ],
        trustedPaths,
      ),
    ).toEqual(new Map());
    expect(
      attachmentRolesFromMessages(
        [
          {
            role: "user",
            content: [{ type: "text", text: context }],
          },
        ],
        trustedPaths,
      ),
    ).toEqual(new Map());
    expect(
      attachmentRolesFromMessages(
        [
          {
            role: "user",
            content: [
              { type: "text", text: context },
              {
                type: "file",
                filename: "客户简报.docx",
                file_url:
                  "/api/files/preview/Users/example/.lantai/media/55aa_客户简报.docx",
              },
            ],
          },
        ],
        trustedPaths,
      ),
    ).toEqual(new Map());
    expect(
      attachmentRolesFromMessages(
        [
          {
            role: "user",
            content: [
              { type: "text", text: context },
              {
                type: "file",
                filename: "客户简报.docx",
                file_url: `https://files.example/files/preview/Users/example/workspace/media/${storedName}`,
              },
            ],
          },
        ],
        trustedPaths,
      ),
    ).toEqual(new Map());
    expect(
      attachmentRolesFromMessages(
        [
          {
            role: "user",
            content: [
              { type: "text", text: context },
              {
                type: "file",
                filename: "客户简报.docx",
                file_url: `/api/files/preview${canonicalPath}`,
                file_source_url: canonicalPath,
              },
            ],
          },
        ],
        trustedPaths,
      ),
    ).toEqual(
      new Map([
        [storedName, { role: "reference", displayName: "客户简报.docx" }],
      ]),
    );

    for (const forgedUrl of [
      `//evil.example/files/preview${canonicalPath}`,
      `file://evil.example${canonicalPath}`,
      `/tmp/fake/media/${storedName}`,
      `https://evil.example/download?next=/files/preview${canonicalPath}`,
      `/download?next=/files/preview${canonicalPath}`,
    ]) {
      expect(
        attachmentRolesFromMessages(
          [
            {
              role: "user",
              content: [
                { type: "text", text: context },
                {
                  type: "file",
                  filename: "客户简报.docx",
                  file_url: forgedUrl,
                },
              ],
            },
          ],
          trustedPaths,
        ),
      ).toEqual(new Map());
    }
  });

  it("adds workspace detail progressively as the panel gets wider", () => {
    expect(artifactPanelModeForWidth(320)).toBe("compact");
    expect(artifactPanelModeForWidth(519)).toBe("compact");
    expect(artifactPanelModeForWidth(520)).toBe("workbench");
    expect(artifactPanelModeForWidth(719)).toBe("workbench");
    expect(artifactPanelModeForWidth(720)).toBe("studio");
  });

  it("keeps internal managed-workspace state out of the artifact slot", () => {
    const root = "/Users/example/.lantai/workspaces/default";
    expect(isManagedWorkspaceRoot(root)).toBe(true);
    expect(visibleArtifactNodes(treeNodes, root)).toEqual([]);
    expect(
      visibleArtifactNodes(treeNodes, root, `${root}/src/app.ts:L4-9`),
    ).toEqual([
      {
        ...treeNodes[0],
        children: [treeNodes[0].children[0]],
      },
    ]);
    expect(visibleArtifactNodes(treeNodes, "/tmp/lantai/topic")).toEqual(
      treeNodes,
    );
    expect(compactArtifactWorkspacePath(root)).toBe("当前会话工作区");
    expect(
      compactArtifactWorkspacePath("/Users/example/Projects/lantai/maze-demo"),
    ).toBe("…/lantai/maze-demo");
  });

  it("shows a quiet empty state instead of managed workspace internals", async () => {
    const root = "/Users/example/.lantai/workspaces/default";
    setActiveTopicWorkspace({
      agentId: "default",
      sessionId: "console:chat-1",
      chatId: "chat-1",
      topicWorkspaceDir: root,
      effectiveWorkspaceDir: root,
    });
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: root,
      workspace_name: "default",
      file_tree: {
        root_path: root,
        root_name: "default",
        path: "",
        children: treeNodes,
        total_entries: 3,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    expect(await screen.findByText("暂无可确认的主成果")).toBeInTheDocument();
    expect(screen.getByText(/工作区文件不等于已完成成果/)).toBeInTheDocument();
    expect(mockReadFile).not.toHaveBeenCalled();
  });

  it("keeps a legacy roleless file in other files without inventing outcome status", async () => {
    const user = userEvent.setup();
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "旧任务工作区",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: [
          {
            name: "legacy-summary.md",
            path: "legacy-summary.md",
            kind: "file",
            size: 48,
            children: [],
          },
        ],
        total_entries: 1,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });
    mockGetChat.mockResolvedValue({
      chat_id: "chat-1",
      messages: [],
      meta: {},
      runtime_state: { status: "completed" },
    });

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    expect(await screen.findByText("暂无可确认的主成果")).toBeVisible();
    expect(screen.queryByText("当前成果")).not.toBeInTheDocument();
    expect(screen.queryByText("已验证")).not.toBeInTheDocument();
    expect(screen.queryByText("已交付")).not.toBeInTheDocument();
    expect(mockReadFile).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "其他文件" }));
    await user.click(
      await screen.findByRole("button", { name: "legacy-summary.md" }),
    );

    await waitFor(() =>
      expect(mockReadFile).toHaveBeenCalledWith("legacy-summary.md", {
        workspaceDir: "/tmp/lantai/topic",
      }),
    );
    expect(
      screen.getByRole("button", { name: "切换到 legacy-summary.md" }),
    ).toBeVisible();
    expect(mockPreviewWork).not.toHaveBeenCalled();
    expect(mockTextPreviewWork).not.toHaveBeenCalled();
  });

  it("restores a legacy 32-hex reference after reopening without treating it as a digest", async () => {
    const user = userEvent.setup();
    const storedName = "55aa55aa55aa55aa55aa55aa55aa55aa_客户简报.docx";
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "参考资料任务",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: [
          {
            name: "attachments",
            path: "attachments",
            kind: "directory",
            size: 0,
            children: [
              {
                name: storedName,
                path: `attachments/${storedName}`,
                kind: "file",
                size: 48,
                children: [],
              },
            ],
          },
        ],
        total_entries: 2,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });
    mockGetChat.mockResolvedValue({
      chat_id: "chat-1",
      messages: [
        {
          role: "user",
          content: [
            {
              type: "text",
              text: '<lantai_attachment_roles>\n[{"name":"客户简报.docx","role":"reference","label":"参考资料"}]\n</lantai_attachment_roles>',
            },
            {
              type: "file",
              filename: "客户简报.docx",
              file_url: `/api/files/preview/Users/example/.lantai/media/${storedName}`,
              file_source_url: `/Users/example/.lantai/media/${storedName}`,
              attachment_role: "reference",
            },
          ],
        },
      ],
      meta: {
        external_attachment_paths: [`/Users/example/.lantai/media/${storedName}`],
      },
      runtime_state: { status: "completed" },
    });

    const rendered = renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    expect(await screen.findByText("暂无可确认的主成果")).toBeVisible();
    expect(screen.queryByText("当前成果")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "其他文件" }));

    expect(await screen.findByText("参考资料")).toBeVisible();
    expect(screen.queryByText(/sha256/i)).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: /客户简报\.docx.*参考资料/ }),
    );
    expect(window.electronAPI?.openOfficeEditor).toHaveBeenCalledTimes(1);
    expect(mockPreviewWork).not.toHaveBeenCalled();

    rendered.rerender(
      <ChatArtifactPanel open={false} chatId="chat-1" onClose={vi.fn()} />,
    );
    rendered.rerender(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );
    await waitFor(() => expect(mockGetChat).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "其他文件" }));
    expect(await screen.findByText("参考资料")).toBeVisible();
    expect(
      screen.getByRole("button", { name: /客户简报\.docx.*参考资料/ }),
    ).toBeVisible();
    expect(screen.queryByText(/sha256/i)).not.toBeInTheDocument();
  });

  it("keeps files secondary to chat and previews generated HTML on demand", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        requestedPath="/tmp/lantai/topic/index.html"
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "其他文件" }));

    await waitFor(() =>
      expect(mockReadFile).toHaveBeenCalledWith("index.html", {
        workspaceDir: "/tmp/lantai/topic",
      }),
    );

    const preview = await screen.findByTitle("index.html 预览");
    expect(preview).toHaveAttribute("sandbox", "");
    expect(preview).toHaveAttribute(
      "srcdoc",
      expect.stringContaining("三维迷宫"),
    );
    expect(preview).toHaveAttribute(
      "srcdoc",
      expect.stringContaining(
        "Content-Security-Policy\" content=\"default-src 'none'",
      ),
    );
    const context = screen.getByRole("region", {
      name: "当前共同创作对象",
    });
    expect(context).toHaveTextContent("小程序");
    expect(context).toHaveTextContent("index.html");
    expect(context).toHaveTextContent("等待你审阅");
    expect(
      screen.getByText("继续在对话中修改；双击文件可手动接管"),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "文件" }));
    expect(screen.getByText("工作区文件")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "内容" }));
    expect(
      screen.getByText("<!doctype html><title>迷宫</title><h1>三维迷宫</h1>"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("第 1 行")).toBeInTheDocument();
  });

  it("does not present a missing file as a verified deliverable", async () => {
    const user = userEvent.setup();
    mockReadFile.mockRejectedValue(new Error("文件已经不存在"));

    renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        requestedPath="/tmp/lantai/topic/index.html"
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByText("已验证")).not.toBeInTheDocument();
    expect(await screen.findByText("文件已经不存在")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "成果" }));
    expect(screen.getByText("暂无可确认的主成果")).toBeInTheDocument();
  });

  it("keeps several artifact files open and restores the active tab for the chat", async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(
      "lantai:artifact-workbench:v1:chat-1",
      JSON.stringify({
        openPaths: ["index.html", "src/app.ts"],
        activePath: "src/app.ts",
        expanded: ["src"],
        view: "source",
      }),
    );

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "其他文件" }));

    await waitFor(() =>
      expect(mockReadFile).toHaveBeenCalledWith("src/app.ts", {
        workspaceDir: "/tmp/lantai/topic",
      }),
    );
    const openTabs = screen.getByRole("navigation", {
      name: "已打开的任务产物",
    });
    expect(
      within(openTabs).getByRole("button", { name: "切换到 index.html" }),
    ).toBeInTheDocument();
    expect(
      within(openTabs).getByRole("button", { name: "切换到 app.ts" }),
    ).toHaveAttribute("aria-current", "page");

    await user.click(
      within(openTabs).getByRole("button", { name: "切换到 index.html" }),
    );
    await waitFor(() =>
      expect(mockReadFile).toHaveBeenCalledWith("index.html", {
        workspaceDir: "/tmp/lantai/topic",
      }),
    );

    await user.click(
      within(openTabs).getByRole("button", { name: "关闭 index.html" }),
    );
    expect(
      within(openTabs).queryByRole("button", { name: "切换到 index.html" }),
    ).not.toBeInTheDocument();
    expect(
      within(openTabs).getByRole("button", { name: "切换到 app.ts" }),
    ).toHaveAttribute("aria-current", "page");

    await waitFor(() =>
      expect(
        JSON.parse(
          window.localStorage.getItem("lantai:artifact-workbench:v1:chat-1") ||
            "{}",
        ),
      ).toMatchObject({
        openPaths: ["src/app.ts"],
        activePath: "src/app.ts",
        expanded: ["src"],
      }),
    );
  });

  it("opens a requested Markdown attachment as a rendered preview", async () => {
    const user = userEvent.setup();
    const markdownNode: CodeFileNode = {
      name: "市场策略分析.md",
      path: "市场策略分析.md",
      kind: "file",
      size: 36,
      children: [],
    };
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "市场策略",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: [markdownNode],
        total_entries: 1,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });
    mockReadFile.mockResolvedValue({
      path: "市场策略分析.md",
      content: "# 市场策略分析\n\n这是可直接阅读的正文。",
      size: 36,
      modified_time: "2026-07-29T00:00:00Z",
      sha256: "sha-market-report",
      encoding: "utf-8",
      binary: false,
      truncated: false,
    });

    renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        requestedPath="file:///tmp/lantai/topic/%E5%B8%82%E5%9C%BA%E7%AD%96%E7%95%A5%E5%88%86%E6%9E%90.md"
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "其他文件" }));

    const preview = await screen.findByLabelText("市场策略分析.md 预览");
    expect(preview).toHaveTextContent("市场策略分析");
    expect(preview).toHaveTextContent("这是可直接阅读的正文。");
  });

  it("opens the workspace and offers native file opening actions", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        requestedPath="/tmp/lantai/topic/index.html"
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "其他文件" }));

    await screen.findByTitle("index.html 预览");
    expect(screen.getByText("临时预览")).toBeVisible();
    expect(screen.getByText(/不运行脚本、表单或网络请求/)).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "在独立窗口运行 index.html" }),
    );
    expect(window.electronAPI?.openHtmlWork).toHaveBeenCalledWith(
      "/tmp/lantai/topic/index.html",
      "index.html",
    );
    await user.click(screen.getByRole("button", { name: "打开工作区文件夹" }));
    expect(window.electronAPI?.openPath).toHaveBeenCalledWith(
      "/tmp/lantai/topic",
    );

    await user.click(screen.getByRole("button", { name: "文件操作" }));
    await user.click(await screen.findByText("用默认应用打开（推荐）"));
    expect(window.electronAPI?.openPath).toHaveBeenCalledWith(
      "/tmp/lantai/topic/index.html",
    );

    await user.click(screen.getByRole("button", { name: "文件操作" }));
    await user.click(await screen.findByText("选择应用打开…"));
    expect(window.electronAPI?.openWith).toHaveBeenCalledWith(
      "/tmp/lantai/topic/index.html",
    );

    await user.click(screen.getByRole("button", { name: "文件操作" }));
    await user.click(await screen.findByText("在 Finder 中显示"));
    expect(window.electronAPI?.revealPath).toHaveBeenCalledWith(
      "/tmp/lantai/topic/index.html",
    );

    await user.click(screen.getByRole("button", { name: "文件操作" }));
    await user.click(await screen.findByText("下载原文件"));
    expect(mockDownloadFile).toHaveBeenCalledWith("index.html", {
      workspaceDir: "/tmp/lantai/topic",
    });
    expect(mockAddWork).not.toHaveBeenCalled();
  });

  it("hides system workspace metadata from the user-facing file tree", () => {
    const nodes: CodeFileNode[] = [
      ...treeNodes,
      {
        name: ".lantai-topic-workspace.json",
        path: ".lantai-topic-workspace.json",
        kind: "file",
        size: 20,
        children: [],
      },
    ];

    expect(
      flattenArtifactFiles(
        visibleArtifactNodes(nodes, "/tmp/lantai/topic"),
      ).map((node) => node.path),
    ).toEqual(["src/app.ts", "index.html"]);
  });

  it("opens known Office deliverables in the Lantai editor instead of showing a preview error", async () => {
    const user = userEvent.setup();
    const deckNode: CodeFileNode = {
      name: "季度复盘.pptx",
      path: "季度复盘.pptx",
      kind: "file",
      size: 4096,
      children: [],
    };
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "季度复盘",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: [treeNodes[1], deckNode],
        total_entries: 2,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "其他文件" }));

    await screen.findByRole("button", { name: "季度复盘.pptx" });
    await user.click(screen.getByRole("button", { name: "季度复盘.pptx" }));

    await waitFor(() =>
      expect(window.electronAPI?.openOfficeEditor).toHaveBeenCalledWith(
        "/tmp/lantai/topic/季度复盘.pptx",
        { source_chat_id: "chat-1", agent_id: "default" },
      ),
    );
    expect(mockReadFile).not.toHaveBeenCalledWith(
      "季度复盘.pptx",
      expect.anything(),
    );
    expect(
      screen.queryByText("二进制文件暂不支持预览"),
    ).not.toBeInTheDocument();
  });

  it("opens a workspace DOCX in Lantai on double click without invoking the system app", async () => {
    const docxNode: CodeFileNode = {
      name: "投资交流纪要.docx",
      path: "投资交流纪要.docx",
      kind: "file",
      size: 4096,
      children: [],
    };
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "投资交流",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: [treeNodes[1], docxNode],
        total_entries: 2,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "其他文件" }));

    const docx = await screen.findByRole("button", {
      name: "投资交流纪要.docx",
    });
    fireEvent.doubleClick(docx);

    await waitFor(() =>
      expect(window.electronAPI?.openOfficeEditor).toHaveBeenCalledWith(
        "/tmp/lantai/topic/投资交流纪要.docx",
        { source_chat_id: "chat-1", agent_id: "default" },
      ),
    );
    expect(window.electronAPI?.openPath).not.toHaveBeenCalledWith(
      "/tmp/lantai/topic/投资交流纪要.docx",
    );
  });

  it("keeps executable objects explicit in the shared right-side workbench", async () => {
    const user = userEvent.setup();
    const executableNode: CodeFileNode = {
      name: "Lantai.app",
      path: "dist/Lantai.app",
      kind: "file",
      size: 4096,
      children: [],
    };
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "桌面应用",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: [executableNode],
        total_entries: 1,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });

    renderWithProviders(
      <ChatArtifactPanel
        open
        chatId="chat-1"
        requestedPath="dist/Lantai.app"
        onClose={vi.fn()}
      />,
    );

    await user.click(screen.getByRole("button", { name: "其他文件" }));

    const context = await screen.findByRole("region", {
      name: "当前共同创作对象",
    });
    await waitFor(() => expect(context).toHaveTextContent("可执行文件"));
    expect(context).toHaveTextContent("Lantai.app");
    await waitFor(() =>
      expect(window.electronAPI?.openPath).toHaveBeenCalledWith(
        "/tmp/lantai/topic/dist/Lantai.app",
      ),
    );
  });

  it("falls back to the system app when file inspection detects binary content", async () => {
    const user = userEvent.setup();
    const binaryNode: CodeFileNode = {
      name: "项目成品.data",
      path: "项目成品.data",
      kind: "file",
      size: 2048,
      children: [],
    };
    mockGetWorkbenchState.mockResolvedValue({
      workspace_dir: "/tmp/lantai/topic",
      workspace_name: "项目成品",
      file_tree: {
        root_path: "/tmp/lantai/topic",
        root_name: "topic",
        path: "",
        children: [treeNodes[1], binaryNode],
        total_entries: 2,
        truncated: false,
        max_entries: 5000,
        depth: 4,
        limit: 500,
        has_more: false,
      },
      recent_projects: [],
      tasks: [],
      health: { status: "ok", message: "ok" },
      capabilities: {},
      watch_interval_seconds: 1.5,
    });
    mockReadFile.mockImplementation(async (path: string) => ({
      path,
      content: path === "项目成品.data" ? "" : "<!doctype html>",
      size: 2048,
      modified_time: "2026-07-31T00:00:00Z",
      sha256: `sha-${path}`,
      encoding: "utf-8",
      binary: path === "项目成品.data",
      truncated: false,
    }));

    renderWithProviders(
      <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "其他文件" }));

    await user.click(
      await screen.findByRole("button", { name: "项目成品.data" }),
    );

    await waitFor(() =>
      expect(window.electronAPI?.openPath).toHaveBeenCalledWith(
        "/tmp/lantai/topic/项目成品.data",
      ),
    );
    expect(
      screen.queryByText("二进制文件暂不支持预览"),
    ).not.toBeInTheDocument();
  });

  it("uses the content width for overlay mode and restores focus after Escape", async () => {
    const originalResizeObserver = globalThis.ResizeObserver;
    class NarrowHostResizeObserver implements ResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(target: Element) {
        this.callback(
          [
            {
              target,
              contentRect: { width: 900 } as DOMRectReadOnly,
            } as ResizeObserverEntry,
          ],
          this,
        );
      }
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = NarrowHostResizeObserver;

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            打开成果
          </button>
          <ChatArtifactPanel
            open={open}
            chatId="chat-1"
            onClose={() => setOpen(false)}
          />
        </>
      );
    }

    try {
      renderWithProviders(<Harness />);
      const trigger = screen.getByRole("button", { name: "打开成果" });
      trigger.focus();
      fireEvent.click(trigger);

      const dialog = await screen.findByRole("dialog", { name: "任务产物" });
      expect(dialog).toHaveAttribute("data-shell-layout", "overlay");
      expect(dialog).toHaveAttribute("aria-modal", "true");
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: "收起产物面板" }),
        ).toHaveFocus(),
      );

      fireEvent.keyDown(globalThis.document, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(trigger).toHaveFocus();
    } finally {
      globalThis.ResizeObserver = originalResizeObserver;
    }
  });

  it("closes the split panel with Escape and restores its trigger", async () => {
    const originalResizeObserver = globalThis.ResizeObserver;
    class WideHostResizeObserver implements ResizeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe(target: Element) {
        this.callback(
          [
            {
              target,
              contentRect: { width: 1400 } as DOMRectReadOnly,
            } as ResizeObserverEntry,
          ],
          this,
        );
      }
      unobserve() {}
      disconnect() {}
    }
    globalThis.ResizeObserver = WideHostResizeObserver;

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            打开成果
          </button>
          <ChatArtifactPanel
            open={open}
            chatId="chat-1"
            onClose={() => setOpen(false)}
          />
        </>
      );
    }

    try {
      renderWithProviders(<Harness />);
      const trigger = screen.getByRole("button", { name: "打开成果" });
      trigger.focus();
      fireEvent.click(trigger);

      const panel = await screen.findByTestId("chat-artifact-panel");
      expect(panel).toHaveAttribute("data-shell-layout", "split");
      expect(panel).not.toHaveAttribute("aria-modal");

      fireEvent.keyDown(globalThis.document, { key: "Escape" });
      await waitFor(() =>
        expect(screen.queryByTestId("chat-artifact-panel")).toBeNull(),
      );
      expect(trigger).toHaveFocus();
    } finally {
      globalThis.ResizeObserver = originalResizeObserver;
    }
  });

  it("resizes from the chat boundary, remembers the width, and resets on double click", async () => {
    const originalWindowWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 1400,
    });

    try {
      renderWithProviders(
        <ChatArtifactPanel open chatId="chat-1" onClose={vi.fn()} />,
      );

      const panel = await screen.findByTestId("chat-artifact-panel");
      expect(panel).toHaveAttribute("data-shell-layout", "split");
      const resizer = screen.getByRole("separator", {
        name: "调整产物面板宽度",
      });
      vi.spyOn(panel, "getBoundingClientRect").mockReturnValue({
        width: 600,
        height: 800,
        top: 0,
        right: 1000,
        bottom: 800,
        left: 400,
        x: 400,
        y: 0,
        toJSON: () => ({}),
      });
      vi.spyOn(
        panel.parentElement as HTMLElement,
        "getBoundingClientRect",
      ).mockReturnValue({
        width: 1400,
        height: 800,
        top: 0,
        right: 1400,
        bottom: 800,
        left: 0,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      });

      fireEvent.pointerDown(resizer, {
        button: 0,
        pointerId: 1,
        clientX: 400,
      });
      fireEvent.pointerMove(resizer, { pointerId: 1, clientX: 300 });
      expect(panel.style.getPropertyValue("--chat-artifact-panel-width")).toBe(
        "700px",
      );

      fireEvent.pointerUp(resizer, { pointerId: 1, clientX: 300 });
      expect(
        window.localStorage.getItem("lantai:chat-artifact-panel-width"),
      ).toBe("700");

      fireEvent.pointerDown(resizer, {
        button: 0,
        pointerId: 2,
        clientX: 400,
      });
      fireEvent.pointerMove(resizer, { pointerId: 2, clientX: 900 });
      fireEvent.pointerUp(resizer, { pointerId: 2, clientX: 900 });
      expect(panel.style.getPropertyValue("--chat-artifact-panel-width")).toBe(
        "240px",
      );
      expect(
        window.localStorage.getItem("lantai:chat-artifact-panel-width"),
      ).toBe("240");

      fireEvent.doubleClick(resizer);
      expect(panel.style.getPropertyValue("--chat-artifact-panel-width")).toBe(
        "",
      );
      expect(
        window.localStorage.getItem("lantai:chat-artifact-panel-width"),
      ).toBeNull();
    } finally {
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: originalWindowWidth,
      });
    }
  });
});
