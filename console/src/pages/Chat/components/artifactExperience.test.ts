import { describe, expect, it } from "vitest";
import type { WorkItem } from "../../../api/types/works";
import {
  attachmentToContent,
  type AttachmentItem,
} from "../../../vendor/lantaiChat";
import {
  attachmentRolesFromMessages,
  buildArtifactEditTarget,
  projectArtifactOutcome,
  selectPrimaryOutcome,
} from "./artifactExperience";

function work(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    id: "work-1",
    artifact_id: "artifact-1",
    title: "季度复盘",
    kind: "presentation",
    path: "/managed/work-1/季度复盘.pptx",
    source_path: "/topic/季度复盘.pptx",
    source_chat_id: "chat-1",
    source_run_id: "run-1",
    source_digest: "sha256:deck-v1",
    artifact_role: "primary",
    completion_bound: true,
    delivery_status: "partial",
    lineage_root_id: "work-1",
    version: 1,
    availability_status: "available",
    integrity_status: "intact",
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
    ...overrides,
  };
}

describe("artifact outcome projection", () => {
  it("selects the real current-run work instead of index.html or tree order", () => {
    const selection = selectPrimaryOutcome({
      works: [
        work({
          id: "old",
          artifact_id: "old-artifact",
          source_run_id: "run-0",
        }),
        work({ id: "current", artifact_id: "current-artifact" }),
      ],
      chatId: "chat-1",
      currentRunId: "run-1",
    });

    expect(selection.primary?.id).toBe("current");
    expect(selection.reason).toBe("current-run");
  });

  it("keeps a cjs file eligible when Works records it as the explicit outcome", () => {
    const script = work({
      id: "script",
      artifact_id: "script-artifact",
      title: "生成脚本",
      kind: "file",
      path: "/managed/script/build-deck.cjs",
      source_path: "/topic/build-deck.cjs",
      source_digest: "sha256:script",
    });

    expect(
      selectPrimaryOutcome({
        works: [script],
        chatId: "chat-1",
        currentRunId: "run-1",
      }).primary,
    ).toEqual(script);
  });

  it("does not promote a lone process script without canonical role evidence", () => {
    const script = work({
      id: "script",
      artifact_id: "script-artifact",
      title: "生成脚本",
      kind: "file",
      path: "/managed/script/build-deck.cjs",
      source_path: "/topic/build-deck.cjs",
      source_digest: "sha256:script",
      artifact_role: null,
      completion_bound: false,
      delivery_status: null,
    });

    const selection = selectPrimaryOutcome({
      works: [script],
      chatId: "chat-1",
      currentRunId: "run-1",
    });

    expect(selection.primary).toBeNull();
    expect(selection.reason).toBe("none");
    expect(selection.alternatives).toEqual([script]);
  });

  it("does not guess when two unrelated outcomes have equal evidence", () => {
    const selection = selectPrimaryOutcome({
      works: [
        work({ id: "deck", artifact_id: "deck-artifact" }),
        work({
          id: "report",
          artifact_id: "report-artifact",
          kind: "pdf",
          path: "/managed/report/report.pdf",
          source_path: "/topic/report.pdf",
          lineage_root_id: "report",
          source_digest: "sha256:report",
        }),
      ],
      chatId: "chat-1",
      currentRunId: "run-1",
    });

    expect(selection.primary).toBeNull();
    expect(selection.reason).toBe("ambiguous");
    expect(selection.alternatives).toHaveLength(2);
  });

  it("keeps an explicitly related PDF as a derivative, not a competing primary", () => {
    const presentation = work({ id: "deck", artifact_id: "deck-artifact" });
    const pdf = work({
      id: "deck-pdf",
      artifact_id: "pdf-artifact",
      title: "季度复盘（PDF）",
      kind: "pdf",
      path: "/managed/deck-pdf/季度复盘.pptx.pdf",
      source_path: "/derived/季度复盘.pptx.pdf",
      source_digest: "sha256:pdf-v1",
      lineage_root_id: "deck-pdf",
      derivation: {
        schema_version: 1,
        operation: "office_conversion",
        target_format: "pdf",
        source_work_id: presentation.id,
        source_artifact_id: presentation.artifact_id,
        source_version: presentation.version,
        source_digest: presentation.source_digest || "",
        output_digest: "sha256:pdf-v1",
        converter_output_digest: "sha256:raw-pdf-v1",
        converter: "libreoffice",
      },
    });

    const selection = selectPrimaryOutcome({
      works: [presentation, pdf],
      chatId: "chat-1",
      currentRunId: "run-1",
    });

    expect(selection.primary?.id).toBe("deck");
    expect(selection.reason).toBe("current-run");
    expect(selection.alternatives.map((item) => item.id)).toContain("deck-pdf");
  });

  it("keeps an explicitly selected revision stable when a newer version exists", () => {
    const selection = selectPrimaryOutcome({
      works: [
        work({ id: "v1", lineage_root_id: "v1", version: 1 }),
        work({
          id: "v2",
          lineage_root_id: "v1",
          parent_work_id: "v1",
          version: 2,
          source_digest: "sha256:deck-v2",
        }),
      ],
      chatId: "chat-1",
      pinnedWorkId: "v1",
    });

    expect(selection.primary?.id).toBe("v1");
    expect(selection.reason).toBe("user-selection");
  });

  it("keeps availability, preview, validation, delivery and execution separate", () => {
    const outcome = projectArtifactOutcome({
      work: work(),
      runtime: {
        status: "completed",
        run_id: "run-1",
        delivery_status: "partial",
        outcome_status: "incomplete",
      },
      previewStatus: "ready",
    });

    expect(outcome.availability).toBe("available");
    expect(outcome.preview).toBe("ready");
    expect(outcome.validation).toBeNull();
    expect(outcome.delivery).toBe("partial");
    expect(outcome.execution).toBe("completed");
  });

  it("projects the exact versioned completion-gate validation record", () => {
    const validation = {
      schema_version: 1 as const,
      record_id: "validation:run-1:attempt-1:work-1:sha256:deck-v1",
      status: "passed" as const,
      source_run_id: "run-1",
      source_attempt_id: "attempt-1",
      work_id: "work-1",
      artifact_id: "artifact-1",
      artifact_version: 1,
      artifact_digest: "sha256:deck-v1",
      validator_id: "runtime-completion-gate",
      validator_version: "1",
      ruleset_id: "delivery-readiness",
      ruleset_version: "1",
      checked_at: "2026-09-21T00:00:00Z",
      required_scope: ["deliverable_openable"],
      observed_scope: ["deliverable_openable"],
      checks: [
        {
          id: "deliverable_openable",
          outcome: "passed" as const,
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
    };
    const validatedWork = { ...work(), validation } as WorkItem & {
      validation: typeof validation;
    };

    const outcome = projectArtifactOutcome({
      work: validatedWork,
      previewStatus: "ready",
    });

    expect(outcome.validation).toEqual(validation);
  });

  it("does not project chat-wide delivery onto an unbound revision", () => {
    const outcome = projectArtifactOutcome({
      work: work({ completion_bound: false }),
      runtime: {
        status: "completed",
        run_id: "run-1",
        delivery_status: "delivered",
      },
      previewStatus: "ready",
    });

    expect(outcome.delivery).toBe("unknown");
  });

  it("keeps exact historical delivery when a different run is current", () => {
    const outcome = projectArtifactOutcome({
      work: work({ source_run_id: "run-a", delivery_status: "verified" }),
      runtime: {
        status: "running",
        run_id: "run-b",
        delivery_status: "pending",
      },
      previewStatus: "ready",
    });

    expect(outcome.delivery).toBe("verified");
    expect(outcome.execution).toBe("unknown");
  });

  it("restores exact historical delivery without a live runtime", () => {
    const outcome = projectArtifactOutcome({
      work: work({ source_run_id: "run-a", delivery_status: "verified" }),
      runtime: null,
      previewStatus: "ready",
    });

    expect(outcome.delivery).toBe("verified");
    expect(outcome.execution).toBe("unknown");
  });

  it("recognizes a structured attachment role from the real 64-hex upload format without fake text", () => {
    const storedName = `${"a".repeat(64)}_用户图片.png`;
    const canonicalPath = `/Users/example/.lantai/media/${storedName}`;
    const attachment: AttachmentItem = {
      id: "upload-1",
      name: "用户图片.png",
      type: "image/png",
      size: 1024,
      url: `/api/files/preview${canonicalPath}`,
      status: "ready",
      canonicalPath,
      storedName,
      role: "reference",
      roleConfirmed: true,
    };
    const persistedContent = JSON.parse(
      JSON.stringify(attachmentToContent(attachment)),
    );

    expect(persistedContent.type).toBe("image");

    expect(
      attachmentRolesFromMessages(
        [
          {
            role: "user",
            content: [persistedContent],
          },
        ],
        [canonicalPath],
      ),
    ).toEqual(
      new Map([
        [storedName, { role: "reference", displayName: "用户图片.png" }],
      ]),
    );
  });

  it("keeps the exact trusted-path fence for legacy 32-hex attachment names", () => {
    const storedName = `${"b".repeat(32)}_历史资料.pdf`;
    const canonicalPath = `/Users/example/.lantai/media/${storedName}`;
    const message = {
      role: "user" as const,
      content: [
        {
          type: "file",
          filename: "历史资料.pdf",
          file_source_url: canonicalPath,
          attachment_role: "reference",
        },
      ],
    };

    expect(attachmentRolesFromMessages([message], [canonicalPath])).toEqual(
      new Map([
        [storedName, { role: "reference", displayName: "历史资料.pdf" }],
      ]),
    );
    expect(
      attachmentRolesFromMessages(
        [message],
        [`/Users/example/other-chat/media/${storedName}`],
      ),
    ).toEqual(new Map());
  });

  it("binds page selection to canonical chat, asset and base revision", () => {
    expect(
      buildArtifactEditTarget({
        chatId: "chat-1",
        work: work(),
        editPath: "/workspace/.lantai/works/edits/edit-1/季度复盘.pptx",
        page: 3,
      }),
    ).toEqual({
      schema_version: 1,
      chat_id: "chat-1",
      asset_id: "artifact-1",
      work_id: "work-1",
      edit_path: "/workspace/.lantai/works/edits/edit-1/季度复盘.pptx",
      base_revision: {
        version: 1,
        digest: "sha256:deck-v1",
      },
      selection: { kind: "page", page: 3 },
    });
  });
});
