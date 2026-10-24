import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import { FactSources } from "./FactSources";

const openMemoryFactSources = vi.hoisted(() => vi.fn());
const deleteMemoryFactSource = vi.hoisted(() => vi.fn());
vi.mock("../../../api/modules/workspace", () => ({
  workspaceApi: { openMemoryFactSources, deleteMemoryFactSource },
}));

beforeEach(() => {
  vi.clearAllMocks();
});

it("opens exact quote and separately expands the original context", async () => {
  openMemoryFactSources.mockResolvedValue({
    found: true,
    fact_id: "fact-one",
    sources: [{
      event_id: "event-one",
      source_message_id: "message-one",
      session_id: "session-one",
      source_role: "user",
      content_hash: "hash-one",
      quote: "这周先别提醒我周报",
      start_codepoint: 2,
      end_codepoint: 11,
      status: "located",
      content: "🙂\n这周先别提醒我周报，下周恢复提醒。",
      created_at: "2026-09-27T00:00:00Z",
      source_created_at: "2026-09-26 09:30:00.000",
    }],
  });
  renderWithProviders(<FactSources factId="fact-one" agentId="default" />);

  fireEvent.click(screen.getByText("查看来源"));
  expect(await screen.findByText("这周先别提醒我周报")).toBeInTheDocument();
  expect(screen.getByText("聊天原话")).toBeInTheDocument();
  expect(screen.getByText(/原消息时间/)).toBeInTheDocument();
  expect(openMemoryFactSources).toHaveBeenCalledWith("fact-one");
  fireEvent.click(screen.getByText("查看完整原文"));
  expect(screen.getByText(/下周恢复提醒/)).toBeInTheDocument();
});

it("shows unavailable and derived sources honestly", async () => {
  openMemoryFactSources.mockResolvedValue({
    found: true,
    fact_id: "fact-two",
    sources: [
      { event_id: "missing", status: "missing", quote: "" },
      {
        event_id: "derived", source_role: "derived", status: "source_only",
        quote: "", content: "模型整理的句子", created_at: "2026-09-27T00:00:00Z",
      },
    ],
  });
  renderWithProviders(<FactSources factId="fact-two" agentId="default" />);

  fireEvent.click(screen.getByText("查看来源"));
  await waitFor(() => expect(screen.getByText("来源已不可用")).toBeInTheDocument());
  expect(screen.getByText("模型整理结果，不能当作用户原话")).toBeInTheDocument();
  expect(screen.getByText("查看完整整理文本")).toBeInTheDocument();
});

it("deletes an exact source and refreshes the fact", async () => {
  openMemoryFactSources
    .mockResolvedValueOnce({ found: true, fact_id: "fact-one", sources: [{
      event_id: "event-one", source_role: "user", status: "source_only",
      quote: "", content: "原话",
    }] })
    .mockResolvedValueOnce({ found: true, fact_id: "fact-one", sources: [] });
  deleteMemoryFactSource.mockResolvedValue({ deleted: true });
  const onChanged = vi.fn();
  renderWithProviders(<FactSources factId="fact-one" agentId="default" onChanged={onChanged} />);

  fireEvent.click(screen.getByText("查看来源"));
  fireEvent.click(await screen.findByText("删除记忆来源"));
  fireEvent.click(await screen.findByText("删除来源"));

  await waitFor(() => expect(deleteMemoryFactSource).toHaveBeenCalledWith("fact-one", "event-one"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
  expect(screen.getByText("目前无法追溯到原文。")).toBeInTheDocument();
});
