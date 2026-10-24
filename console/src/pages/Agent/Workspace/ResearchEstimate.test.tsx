import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { ResearchProject } from "@/api/modules/research";
import ResearchEstimate from "./ResearchEstimate";

const estimate = vi.hoisted(() => vi.fn());
vi.mock("@/api/modules/research", () => ({ researchApi: { estimate } }));
const project = { project_id: "one", revision: 1, report_id: null, source_versions: ["source"] } as ResearchProject;
const result = {
  binding: { project_id: "one", revision: 1, report_id: null, source_versions: ["source"] },
  generated_at: 1700000000, fingerprint: "fixture",
  reading: { status: "indexed", remaining_blocks: 2, remaining_chars: 7000, additional_budget_chars: 7000,
    budget_remaining_chars: 6000, fits_budget: false, incomplete_sources: [], stale_sources: [] },
  workflow: { status: "unknown", model_calls: null, tokens: null, seconds: null, cost_cny: null },
  historical_calls: [], excluded_incomplete_calls: 0, exhausted_budgets: [],
};
beforeEach(() => { estimate.mockReset().mockResolvedValue(result); });

it("shows indexed workload, budget shortfall and unknown whole-run costs", async () => {
  render(<ResearchEstimate project={project} />);
  expect(await screen.findByText(/共 7,000 字符/)).toBeTruthy();
  expect(screen.getByText(/阅读预算不足/)).toBeTruthy();
  expect(screen.getByText(/完整流程.*耗时.*未知/)).toBeTruthy();
  expect(estimate).toHaveBeenCalledWith("one", 1);
});

it("discards stale responses on revision switch and clears figures after refresh failure", async () => {
  let resolveOld!: (value: typeof result) => void;
  estimate.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }));
  const view = render(<ResearchEstimate project={project} />);
  await waitFor(() => expect(estimate).toHaveBeenCalledTimes(1));
  const next = { ...project, revision: 2 };
  estimate.mockResolvedValue({ ...result, binding: { ...result.binding, revision: 2 },
    reading: { ...result.reading, remaining_chars: 123 } });
  view.rerender(<ResearchEstimate project={next} />);
  expect(await screen.findByText(/123 字符/)).toBeTruthy();
  await act(async () => resolveOld(result));
  expect(screen.queryByText(/共 7,000 字符/)).toBeNull();
  estimate.mockRejectedValue(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新估算" }));
  expect(await screen.findByText(/估算暂不可用/)).toBeTruthy();
  expect(screen.queryByText(/123 字符/)).toBeNull();
});

it("labels partial and historical data without treating missing costs as zero", async () => {
  estimate.mockResolvedValue({ ...result, reading: { ...result.reading, status: "partial" },
    excluded_incomplete_calls: 1, historical_calls: [{ config_hash: "abc", surface: "chat", provider: "fixture", model: "model",
      samples: 2, tokens: { min: 100, max: 300, known_samples: 2 }, seconds: { min: 1, max: 3, known_samples: 2 },
      cost_cny: { min: null, max: null, known_samples: 0 } }] });
  render(<ResearchEstimate project={project} />);
  expect(await screen.findByText(/资料未完整解析或已变化/)).toBeTruthy();
  fireEvent.click(screen.getByText(/查看单次调用历史范围/));
  expect(screen.getByText(/费用：未知（0\/2 次有记录）/)).toBeTruthy();
  expect(screen.getByText(/不代表下一次或完整研究的预测区间/)).toBeTruthy();
});

it("rejects a response bound to a different report or source set", async () => {
  estimate.mockResolvedValue({ ...result, binding: { ...result.binding, report_id: "new-report" } });
  render(<ResearchEstimate project={project} />);
  expect(await screen.findByText(/估算暂不可用/)).toBeTruthy();
  expect(screen.queryByText(/7,000 字符/)).toBeNull();
});
