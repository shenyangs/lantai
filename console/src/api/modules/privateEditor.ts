import { getApiUrl } from "../config";
import { request } from "../request";
import type {
  AdoptPrivateEditorCandidateRequest,
  CapturePrivateEditorWorkingCopyRequest,
  InvalidatePrivateEditorSourceRequest,
  PrivateEditorAdoptionReceipt,
  PrivateEditorAngleProposal,
  PrivateEditorCandidate,
  PrivateEditorCurrentBody,
  PrivateEditorDeletionReceipt,
  PrivateEditorProject,
  PrivateEditorRecordEntry,
  PrivateEditorRevertReceipt,
  PrivateEditorSession,
  PrivateEditorSettings,
  RevertPrivateEditorProjectRequest,
  ResolvePrivateEditorRecordRequest,
  ResolvePrivateEditorAngleProposalRequest,
  UpdatePrivateEditorSettingsRequest,
} from "../types/privateEditor";

export const privateEditorApi = {
  getSettings: () => request<PrivateEditorSettings>("/private-editor/settings"),

  updateSettings: (payload: UpdatePrivateEditorSettingsRequest) =>
    request<PrivateEditorSettings>("/private-editor/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  listProjects: () =>
    request<PrivateEditorProject[]>("/private-editor/projects"),

  startOrContinue: (workId: string) =>
    request<PrivateEditorSession>(
      `/private-editor/projects/from-work/${encodeURIComponent(workId)}`,
      { method: "POST" },
    ),

  getProject: (projectId: string) =>
    request<PrivateEditorProject>(
      `/private-editor/projects/${encodeURIComponent(projectId)}`,
    ),

  getCurrentBody: (projectId: string) =>
    request<PrivateEditorCurrentBody>(
      `/private-editor/projects/${encodeURIComponent(projectId)}/current-body`,
    ),

  resolveRecord: (
    projectId: string,
    recordId: string,
    payload: ResolvePrivateEditorRecordRequest,
  ) =>
    request<PrivateEditorRecordEntry>(
      `/private-editor/projects/${encodeURIComponent(projectId)}/records/${encodeURIComponent(recordId)}/resolve`,
      { method: "POST", body: JSON.stringify(payload) },
    ),

  resolveAngleProposal: (
    projectId: string,
    proposalId: string,
    payload: ResolvePrivateEditorAngleProposalRequest,
  ) =>
    request<PrivateEditorAngleProposal>(
      `/private-editor/projects/${encodeURIComponent(projectId)}/angle-proposals/${encodeURIComponent(proposalId)}/resolve`,
      { method: "POST", body: JSON.stringify(payload) },
    ),

  invalidateSource: (
    projectId: string,
    payload: InvalidatePrivateEditorSourceRequest,
  ) =>
    request<PrivateEditorProject>(
      `/private-editor/projects/${encodeURIComponent(projectId)}/sources/invalidate`,
      { method: "POST", body: JSON.stringify(payload) },
    ),

  captureWorkingCopy: (
    projectId: string,
    payload: CapturePrivateEditorWorkingCopyRequest,
  ) =>
    request<PrivateEditorCandidate>(
      `/private-editor/projects/${encodeURIComponent(projectId)}/candidates/from-working-copy`,
      { method: "POST", body: JSON.stringify(payload) },
    ),

  adoptCandidate: (
    projectId: string,
    candidateId: string,
    payload: AdoptPrivateEditorCandidateRequest,
  ) =>
    request<PrivateEditorAdoptionReceipt>(
      `/private-editor/projects/${encodeURIComponent(projectId)}/candidates/${encodeURIComponent(candidateId)}/adopt`,
      { method: "POST", body: JSON.stringify(payload) },
    ),

  revertProject: (
    projectId: string,
    payload: RevertPrivateEditorProjectRequest,
  ) =>
    request<PrivateEditorRevertReceipt>(
      `/private-editor/projects/${encodeURIComponent(projectId)}/revert`,
      { method: "POST", body: JSON.stringify(payload) },
    ),

  exportCurrentUrl: (projectId: string) =>
    getApiUrl(
      `/private-editor/projects/${encodeURIComponent(projectId)}/export-current`,
    ),

  diagnosticsUrl: (projectId: string) =>
    getApiUrl(
      `/private-editor/projects/${encodeURIComponent(projectId)}/diagnostics`,
    ),

  researchExportUrl: (projectId: string) =>
    getApiUrl(
      `/private-editor/projects/${encodeURIComponent(projectId)}/research-export`,
    ),

  deleteProject: (projectId: string) =>
    request<PrivateEditorDeletionReceipt>(
      `/private-editor/projects/${encodeURIComponent(projectId)}`,
      { method: "DELETE" },
    ),
};
