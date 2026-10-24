import { request } from "../request";
import type {
  BriefActivationRequest,
  BriefActivationResult,
  BriefCycle,
  BriefDiscussionDraft,
  BriefDiscussionDraftView,
  BriefDeliveryReceipt,
  BriefEdition,
  BriefFeedback,
  BriefFeedbackCreateRequest,
  BriefGrant,
  BriefGrantCreateRequest,
  BriefModelCapability,
  BriefSavedItem,
  BriefSubscription,
  BriefSubscriptionCreateRequest,
  BriefSubscriptionUpdateRequest,
  BriefWorkspaceSnapshot,
  DailyBriefHomeSnapshot,
  NativeBriefNotification,
} from "../types/dailyBrief";

const briefPath = (path = "") => `/daily-brief${path}`;

export const dailyBriefApi = {
  /** Read-only compact metadata. It never starts research or generation. */
  getHomeSnapshot: () =>
    request<DailyBriefHomeSnapshot>(
      briefPath("/home"),
      {},
      {
        operation: "read",
      },
    ),

  /** Read-only workbench payload. Opening an edition has no execution effect. */
  getWorkspaceSnapshot: () =>
    request<BriefWorkspaceSnapshot>(
      briefPath("/workspace"),
      {},
      {
        operation: "read",
      },
    ),

  /** One explicit product-level opt-in; provider and budget plumbing stay internal. */
  activate: (payload: BriefActivationRequest) =>
    request<BriefActivationResult>(
      briefPath("/activate"),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  getModelCapability: () =>
    request<BriefModelCapability>(
      briefPath("/model-capability"),
      {},
      { operation: "read" },
    ),

  getEditions: (limit = 50, offset = 0) =>
    request<BriefEdition[]>(
      briefPath(`/editions?limit=${limit}&offset=${offset}`),
      {},
      { operation: "read" },
    ),

  getEdition: (editionId: string, revision?: number) =>
    request<BriefEdition>(
      briefPath(
        `/editions/${encodeURIComponent(editionId)}${revision ? `?revision=${revision}` : ""}`,
      ),
      {},
      { operation: "read" },
    ),

  retryCycle: (
    cycleId: string,
    payload: { explicit_consent: boolean; action_id: string },
  ) =>
    request<BriefCycle>(
      briefPath(`/cycles/${encodeURIComponent(cycleId)}/retry`),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  markEditionRead: (editionId: string, revision: number) =>
    request<void>(
      briefPath(`/editions/${encodeURIComponent(editionId)}/read`),
      { method: "POST", body: JSON.stringify({ revision }) },
      { operation: "mutation" },
    ),

  getSavedItems: () =>
    request<BriefSavedItem[]>(briefPath("/saved"), {}, { operation: "read" }),
  removeSavedItem: (savedId: string) =>
    request<void>(
      briefPath(`/saved/${encodeURIComponent(savedId)}`),
      { method: "DELETE" },
      { operation: "mutation" },
    ),

  getDeliveryReceipt: (receiptId: string) =>
    request<BriefDeliveryReceipt>(
      briefPath(`/deliveries/${encodeURIComponent(receiptId)}`),
      {},
      { operation: "read" },
    ),

  getDiscussionDraft: (draftId: string) =>
    request<BriefDiscussionDraftView>(
      briefPath(`/discussion-drafts/${encodeURIComponent(draftId)}`),
      {},
      { operation: "read" },
    ),

  setInvitationHidden: (hidden: boolean) =>
    request<void>(
      briefPath("/invitation"),
      { method: "PUT", body: JSON.stringify({ hidden }) },
      { operation: "mutation" },
    ),

  createGrant: (payload: BriefGrantCreateRequest) =>
    request<BriefGrant>(
      briefPath("/grants"),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  revokeGrant: (
    grantId: string,
    payload: { expected_revision: number; action_id: string },
  ) =>
    request<BriefGrant>(
      briefPath(`/grants/${encodeURIComponent(grantId)}/revoke`),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  createSubscription: (payload: BriefSubscriptionCreateRequest) =>
    request<BriefSubscription>(
      briefPath("/subscriptions"),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  updateSubscription: (
    subscriptionId: string,
    payload: BriefSubscriptionUpdateRequest,
  ) =>
    request<BriefSubscription>(
      briefPath(`/subscriptions/${encodeURIComponent(subscriptionId)}`),
      { method: "PATCH", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  createPreview: (payload: {
    grant_id: string;
    local_date: string;
    timezone: string;
    action_id: string;
  }) =>
    request<BriefCycle>(
      briefPath("/previews"),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  claimNextNativeDelivery: () =>
    request<NativeBriefNotification | null>(
      briefPath("/deliveries/native/claim-next"),
      { method: "POST" },
      { operation: "mutation" },
    ),

  resolveNativeDelivery: (
    receiptId: string,
    payload: {
      state: "submitted" | "failed";
      adapter_message_id?: string | null;
      error?: string;
    },
  ) =>
    request<BriefDeliveryReceipt>(
      briefPath(`/deliveries/${encodeURIComponent(receiptId)}/resolve`),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  markDeliveryRead: (receiptId: string) =>
    request<BriefDeliveryReceipt>(
      briefPath(`/deliveries/${encodeURIComponent(receiptId)}/read`),
      { method: "POST" },
      { operation: "mutation" },
    ),

  addFeedback: (
    editionId: string,
    itemId: string | null,
    payload: BriefFeedbackCreateRequest,
  ) =>
    request<BriefFeedback>(
      briefPath(
        `/editions/${encodeURIComponent(editionId)}/feedback${
          itemId ? `?item_id=${encodeURIComponent(itemId)}` : ""
        }`,
      ),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),

  createDiscussionDraft: (
    editionId: string,
    payload: {
      edition_revision: number;
      item_id: string | null;
      text: string;
      action_id: string;
    },
  ) =>
    request<BriefDiscussionDraft>(
      briefPath(`/editions/${encodeURIComponent(editionId)}/discussion-drafts`),
      { method: "POST", body: JSON.stringify(payload) },
      { operation: "mutation" },
    ),
};
