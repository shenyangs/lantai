import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "../request";
import { privateEditorApi } from "./privateEditor";

vi.mock("../request", () => ({ request: vi.fn() }));

const mockedRequest = vi.mocked(request);

describe("privateEditorApi", () => {
  beforeEach(() => mockedRequest.mockReset());

  it("uses bounded project operations instead of arbitrary chat metadata", async () => {
    await privateEditorApi.getSettings();
    await privateEditorApi.updateSettings({ enabled: true });
    await privateEditorApi.startOrContinue("work/with space");
    await privateEditorApi.getProject("project/one");
    await privateEditorApi.getCurrentBody("project/one");
    await privateEditorApi.resolveRecord("project/one", "record/one", {
      expected_current_work_id: "work-1",
      expected_current_digest: "sha256:base",
      expected_record_revision: 2,
      expected_revocation_epoch: 0,
      resolution: "rejected",
    });
    await privateEditorApi.captureWorkingCopy("project/one", {
      expected_base_work_id: "work-1",
      expected_base_digest: "sha256:base",
      expected_record_revision: 2,
      expected_revocation_epoch: 0,
    });
    await privateEditorApi.adoptCandidate("project/one", "candidate/one", {
      operation_id: "op-1",
      candidate_id: "candidate/one",
      expected_base_work_id: "work-1",
      expected_base_digest: "sha256:base",
      expected_record_revision: 2,
      revocation_epoch: 0,
      selected_change_groups: ["change-1"],
    });
    await privateEditorApi.revertProject("project/one", {
      operation_id: "revert-1",
      expected_current_work_id: "work-2",
      expected_current_digest: "sha256:current",
      expected_record_revision: 3,
      revocation_epoch: 0,
      target_work_id: "work-1",
    });
    await privateEditorApi.deleteProject("project/one");

    expect(mockedRequest).toHaveBeenNthCalledWith(1, "/private-editor/settings");
    expect(mockedRequest).toHaveBeenNthCalledWith(
      2,
      "/private-editor/settings",
      { method: "PUT", body: JSON.stringify({ enabled: true }) },
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      3,
      "/private-editor/projects/from-work/work%2Fwith%20space",
      { method: "POST" },
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      4,
      "/private-editor/projects/project%2Fone",
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      5,
      "/private-editor/projects/project%2Fone/current-body",
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      6,
      "/private-editor/projects/project%2Fone/records/record%2Fone/resolve",
      {
        method: "POST",
        body: JSON.stringify({
          expected_current_work_id: "work-1",
          expected_current_digest: "sha256:base",
          expected_record_revision: 2,
          expected_revocation_epoch: 0,
          resolution: "rejected",
        }),
      },
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      7,
      "/private-editor/projects/project%2Fone/candidates/from-working-copy",
      {
        method: "POST",
        body: JSON.stringify({
          expected_base_work_id: "work-1",
          expected_base_digest: "sha256:base",
          expected_record_revision: 2,
          expected_revocation_epoch: 0,
        }),
      },
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      8,
      "/private-editor/projects/project%2Fone/candidates/candidate%2Fone/adopt",
      {
        method: "POST",
        body: JSON.stringify({
          operation_id: "op-1",
          candidate_id: "candidate/one",
          expected_base_work_id: "work-1",
          expected_base_digest: "sha256:base",
          expected_record_revision: 2,
          revocation_epoch: 0,
          selected_change_groups: ["change-1"],
        }),
      },
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      9,
      "/private-editor/projects/project%2Fone/revert",
      {
        method: "POST",
        body: JSON.stringify({
          operation_id: "revert-1",
          expected_current_work_id: "work-2",
          expected_current_digest: "sha256:current",
          expected_record_revision: 3,
          revocation_epoch: 0,
          target_work_id: "work-1",
        }),
      },
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      10,
      "/private-editor/projects/project%2Fone",
      { method: "DELETE" },
    );
    expect(privateEditorApi.diagnosticsUrl("project/one")).toContain(
      "/private-editor/projects/project%2Fone/diagnostics",
    );
    expect(privateEditorApi.researchExportUrl("project/one")).toContain(
      "/private-editor/projects/project%2Fone/research-export",
    );
  });
});
