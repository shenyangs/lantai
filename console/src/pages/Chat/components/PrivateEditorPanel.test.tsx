import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { privateEditorApi } from "../../../api/modules/privateEditor";
import type { PrivateEditorProject } from "../../../api/types/privateEditor";
import PrivateEditorPanel from "./PrivateEditorPanel";

vi.mock("../../../api/modules/privateEditor", () => ({
  privateEditorApi: {
    getSettings: vi.fn(),
    getProject: vi.fn(),
    getCurrentBody: vi.fn(),
    captureWorkingCopy: vi.fn(),
    adoptCandidate: vi.fn(),
    resolveRecord: vi.fn(),
    resolveAngleProposal: vi.fn(),
    invalidateSource: vi.fn(),
    revertProject: vi.fn(),
    exportCurrentUrl: vi.fn(() => "/api/private-editor/export"),
    diagnosticsUrl: vi.fn(() => "/api/private-editor/diagnostics"),
    researchExportUrl: vi.fn(() => "/api/private-editor/research"),
    deleteProject: vi.fn(),
  },
}));

const project: PrivateEditorProject = {
  id: "project-1",
  title: "夜校不是另一张绩效表",
  original_work_id: "work-1",
  original_digest: "sha256:original",
  current_work_id: "work-1",
  current_digest: "sha256:original",
  record_revision: 2,
  revocation_epoch: 0,
  status: "active",
  chat_id: "chat-1",
  working_copy_path: "/managed/edit.md",
  working_copy_owned_by_experiment: true,
  import_scope: "plain_text_body",
  import_warnings: [],
  invalidated_source_refs: [],
  record_entries: [
    {
      id: "record-1",
      kind: "author_intent",
      text: "写给想去夜校又担心自己坚持不下来的人。",
      source: "user_explicit",
      source_ref: "message:1",
      scope: "project",
      status: "accepted",
      supersedes: [],
      created_at: "2026-09-19T00:00:00Z",
    },
    {
      id: "record-2",
      kind: "author_intent",
      text: "作者可能希望结尾更具体。",
      source: "model_inferred",
      source_ref: "assistant:2",
      scope: "project",
      status: "proposed",
      supersedes: [],
      created_at: "2026-09-19T00:00:00Z",
    },
    {
      id: "record-material",
      kind: "material_note",
      text: "采访中的下班后时间线索。",
      source: "system_observed",
      source_ref: "source:interview-01",
      scope: "project",
      status: "open",
      supersedes: [],
      created_at: "2026-09-19T00:00:00Z",
    },
  ],
  candidates: [
    {
      id: "candidate-1",
      base_work_id: "work-1",
      base_digest: "sha256:original",
      record_revision: 2,
      revocation_epoch: 0,
      status: "staged",
      current_draft_changed: false,
      created_at: "2026-09-19T00:00:00Z",
      change_groups: [
        {
          id: "group-1",
          target_anchor: "paragraph:2",
          before: "原来过长的第二段。",
          after: "压缩后的第二段。",
          observation: "同一层意思重复了两次。",
          impact: "主张到得太慢。",
          rationale: "删去重复解释，保留原判断。",
          evidence_refs: ["current-draft:paragraph:2"],
          costs: ["语气会更直接"],
          depends_on: [],
        },
      ],
    },
  ],
  angle_proposals: [
    {
      id: "angle-1",
      source_refs: ["source:interview-01", "record:record-material"],
      key_idea: "从下班后时间的归属切入。",
      why_now: "采访材料出现了重复线索。",
      differs_from: "不再从课程设计展开。",
      next_action: "先对照三位受访者的原话。",
      open_questions: ["样本是否足够？"],
      generated_run_id: "run-angle",
      applicable_work_id: "work-1",
      applicable_digest: "sha256:original",
      status: "proposed",
      resolution_reason: null,
      development_work_id: null,
      created_at: "2026-09-19T00:00:00Z",
      updated_at: "2026-09-19T00:00:00Z",
    },
  ],
  adoption_receipts: [],
  revert_receipts: [],
  history: [],
  created_at: "2026-09-19T00:00:00Z",
  updated_at: "2026-09-19T00:00:00Z",
};

describe("PrivateEditorPanel", () => {
  beforeEach(() => {
    vi.mocked(privateEditorApi.getSettings).mockResolvedValue({
      enabled: true,
      research_consent: false,
      revocation_epoch: 0,
      updated_at: "2026-09-19T00:00:00Z",
    });
    vi.mocked(privateEditorApi.getProject).mockResolvedValue(project);
    vi.mocked(privateEditorApi.getCurrentBody).mockResolvedValue({
      work_id: "work-1",
      digest: "sha256:original",
      body: "当前稿正文。",
      import_scope: "plain_text_body",
      warnings: [],
    });
    vi.mocked(privateEditorApi.adoptCandidate).mockResolvedValue({
      operation_id: "operation",
      request_fingerprint: "sha256:request",
      candidate_id: "candidate-1",
      authorization_source: "button:adopt-selected",
      expected_base_work_id: "work-1",
      expected_base_digest: "sha256:original",
      expected_record_revision: 2,
      revocation_epoch: 0,
      selected_change_groups: ["group-1"],
      status: "acknowledged",
      resulting_work_id: "work-2",
      resulting_digest: "sha256:next",
      created_at: "2026-09-19T00:00:00Z",
      updated_at: "2026-09-19T00:00:00Z",
    });
    vi.mocked(privateEditorApi.deleteProject).mockResolvedValue({
      deleted: true,
      saved_works_preserved: true,
      managed_working_copy_deleted: true,
      chat_transcript_preserved: true,
      provider_copies_out_of_scope: true,
      local_backup_copies_out_of_scope: true,
    });
    vi.mocked(privateEditorApi.resolveRecord).mockResolvedValue({
      id: "record-resolution",
      kind: "decision",
      text: "作者可能希望结尾更具体。",
      source: "user_explicit",
      source_ref: "button:record-resolution",
      scope: "project",
      status: "rejected",
      applicable_work_id: "work-1",
      applicable_digest: "sha256:original",
      supersedes: ["record-2"],
      created_at: "2026-09-19T00:00:00Z",
    });
    vi.mocked(privateEditorApi.resolveAngleProposal).mockResolvedValue({
      ...project.angle_proposals[0],
      status: "deferred",
    });
    vi.mocked(privateEditorApi.invalidateSource).mockResolvedValue(project);
  });

  it("shows an unapplied candidate with evidence and adopts only selected groups", async () => {
    render(<PrivateEditorPanel projectId="project-1" />);

    expect(await screen.findByText("当前稿未变")).toBeInTheDocument();
    expect(screen.getByText("同一层意思重复了两次。")).toBeInTheDocument();
    expect(screen.getByText("语气会更直接")).toBeInTheDocument();

    const adoptButton = screen.getByRole("button", { name: "采纳已选改动" });
    expect(adoptButton).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "选择该改动组" }));
    fireEvent.click(adoptButton);

    await waitFor(() =>
      expect(privateEditorApi.adoptCandidate).toHaveBeenCalledWith(
        "project-1",
        "candidate-1",
        expect.objectContaining({
          candidate_id: "candidate-1",
          expected_base_work_id: "work-1",
          expected_base_digest: "sha256:original",
          expected_record_revision: 2,
          revocation_epoch: 0,
          selected_change_groups: ["group-1"],
        }),
      ),
    );
  });

  it("separates author statements from model proposals", async () => {
    render(<PrivateEditorPanel projectId="project-1" />);
    await screen.findByText("当前稿未变");

    fireEvent.click(screen.getByRole("button", { name: "作品记录" }));
    expect(screen.getByText("作者已说明")).toBeInTheDocument();
    expect(screen.getByText("我的理解待确认")).toBeInTheDocument();
    expect(
      screen.getByText("写给想去夜校又担心自己坚持不下来的人。"),
    ).toBeInTheDocument();
    expect(screen.getByText("作者可能希望结尾更具体。")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "不采用：作者可能希望结尾更具体。" }));
    await waitFor(() =>
      expect(privateEditorApi.resolveRecord).toHaveBeenCalledWith(
        "project-1",
        "record-2",
        {
          expected_current_work_id: "work-1",
          expected_current_digest: "sha256:original",
          expected_record_revision: 2,
          expected_revocation_epoch: 0,
          resolution: "rejected",
        },
      ),
    );
  });

  it("hands a manual new-angle request back to the existing chat composer", async () => {
    const onRequestNewAngle = vi.fn();
    render(
      <PrivateEditorPanel
        projectId="project-1"
        onRequestNewAngle={onRequestNewAngle}
      />,
    );
    await screen.findByText("当前稿未变");

    fireEvent.click(screen.getByRole("button", { name: "找一个新切口" }));

    expect(onRequestNewAngle).toHaveBeenCalledTimes(1);
  });

  it("keeps grounded angle proposals inspectable and records author disposition", async () => {
    const onDevelopAngle = vi.fn();
    render(
      <PrivateEditorPanel
        projectId="project-1"
        onDevelopAngle={onDevelopAngle}
      />,
    );
    await screen.findByText("当前稿未变");

    fireEvent.click(screen.getByRole("button", { name: /^新切口/ }));
    expect(screen.getByText("从下班后时间的归属切入。")).toBeInTheDocument();
    expect(screen.getByText("source:interview-01；record:record-material")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "稍后" }));
    await waitFor(() =>
      expect(privateEditorApi.resolveAngleProposal).toHaveBeenCalledWith(
        "project-1",
        "angle-1",
        expect.objectContaining({
          expected_current_work_id: "work-1",
          expected_current_digest: "sha256:original",
          expected_record_revision: 2,
          expected_revocation_epoch: 0,
          resolution: "later",
        }),
      ),
    );
    expect(onDevelopAngle).not.toHaveBeenCalled();
  });

  it("develops an accepted angle through the existing chat without changing the current draft", async () => {
    const onDevelopAngle = vi.fn();
    render(
      <PrivateEditorPanel
        projectId="project-1"
        onDevelopAngle={onDevelopAngle}
      />,
    );
    await screen.findByText("当前稿未变");

    fireEvent.click(screen.getByRole("button", { name: /^新切口/ }));
    fireEvent.click(screen.getByRole("button", { name: "继续发展" }));

    await waitFor(() =>
      expect(privateEditorApi.resolveAngleProposal).toHaveBeenCalledWith(
        "project-1",
        "angle-1",
        expect.objectContaining({ resolution: "continue" }),
      ),
    );
    expect(onDevelopAngle).toHaveBeenCalledWith(project.angle_proposals[0]);
    expect(screen.getByText("已记录继续发展；当前稿仍未变。")).toBeInTheDocument();
  });

  it("lets the author invalidate a project material source", async () => {
    render(<PrivateEditorPanel projectId="project-1" />);
    await screen.findByText("当前稿未变");

    fireEvent.click(screen.getByRole("button", { name: "作品记录" }));
    fireEvent.click(screen.getByRole("button", { name: "移除来源：source:interview-01" }));
    await waitFor(() =>
      expect(privateEditorApi.invalidateSource).toHaveBeenCalledWith(
        "project-1",
        expect.objectContaining({
          source_ref: "source:interview-01",
          expected_current_work_id: "work-1",
          expected_record_revision: 2,
        }),
      ),
    );
  });

  it("keeps diagnostics local, gates research export, and confirms deletion boundaries", async () => {
    const onClose = vi.fn();
    render(<PrivateEditorPanel projectId="project-1" onClose={onClose} />);
    await screen.findByText("当前稿未变");

    fireEvent.click(screen.getByText("诊断、研究与退出"));
    expect(screen.getByRole("link", { name: /\u4e0b\u8f7d\u672c\u5730\u8bca\u65ad/ })).toHaveAttribute(
      "href",
      "/api/private-editor/diagnostics",
    );
    expect(screen.queryByRole("link", { name: /\u5bfc\u51fa\u7814\u7a76\u6458\u8981/ })).not.toBeInTheDocument();
    expect(screen.getByText(/\u7814\u7a76\u540c\u610f\u672a\u5f00\u542f/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "删除实验项目" }));
    expect(screen.getByText(/\u5df2\u4fdd\u5b58 Works \u7248\u672c\u4e0e\u804a\u5929\u8bb0\u5f55\u4fdd\u7559/)).toBeInTheDocument();
    expect(screen.getByText(/Provider \u4e0e\u672c\u5730\u5907\u4efd/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认删除项目数据" }));

    await waitFor(() => {
      expect(privateEditorApi.deleteProject).toHaveBeenCalledWith("project-1");
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });
});
