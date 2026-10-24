import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AnalysisChartArtifact } from "./AnalysisChartArtifact";
import { chatApi } from "@/api";

vi.mock("@/api", () => ({
  chatApi: { getAnalysisChart: vi.fn() },
}));
vi.mock("./EChartsArtifact", () => ({
  EChartsArtifact: ({ source }: { source: string }) => (
    <pre data-testid="chart-option">{source}</pre>
  ),
}));

const ANALYSIS_ID = "a".repeat(64);
const reference = JSON.stringify({
  analysis_id: ANALYSIS_ID,
  x_column: "region",
  y_column: "revenue",
  type: "bar",
});

describe("AnalysisChartArtifact", () => {
  beforeEach(() => {
    window.location.hash = "/chat/chart-owner";
    vi.mocked(chatApi.getAnalysisChart).mockReset();
  });

  it("renders server-bound values and provenance, not numbers from the answer", async () => {
    vi.mocked(chatApi.getAnalysisChart).mockResolvedValue({
      analysis_id: ANALYSIS_ID,
      dataset_id: "b".repeat(64),
      source_name: "sales.csv",
      source_sha256: "c".repeat(64),
      source_row_count: 2,
      sheet_name: null,
      analysis_plan: { group_by: ["region"] },
      calculated_at: "2026-09-27T00:00:00Z",
      source_status: "current",
      semantic_review: "pending",
      warnings: [],
      x_column: "region",
      y_column: "revenue",
      points: [{ label: "East", value: "10.20" }],
    });

    render(<AnalysisChartArtifact source={reference} />);
    await waitFor(() => expect(screen.getByText("10.20")).toBeTruthy());
    expect(chatApi.getAnalysisChart).toHaveBeenCalledWith(
      "chart-owner", ANALYSIS_ID, "region", "revenue", expect.anything(),
    );
    expect(screen.getByText("sales.csv")).toBeTruthy();
    expect(screen.getByTestId("chart-option").textContent).toContain("10.2");
    expect(screen.getByText(/业务语义待审阅/)).toBeTruthy();
  });

  it("keeps a zero baseline for bar magnitudes and discloses a focused line scale", async () => {
    vi.mocked(chatApi.getAnalysisChart).mockResolvedValue({
      analysis_id: ANALYSIS_ID,
      dataset_id: "b".repeat(64),
      source_name: "sales.csv",
      source_sha256: "c".repeat(64),
      source_row_count: 2,
      sheet_name: null,
      analysis_plan: { group_by: ["region"] },
      calculated_at: "2026-09-27T00:00:00Z",
      source_status: "current",
      semantic_review: "pending",
      warnings: [],
      x_column: "region",
      y_column: "revenue",
      points: [{ label: "East", value: "95" }, { label: "West", value: "98" }],
    });

    const { rerender } = render(<AnalysisChartArtifact source={reference} />);
    await waitFor(() => expect(screen.getByTestId("chart-option").textContent).toContain("95"));
    const bar = JSON.parse(screen.getByTestId("chart-option").textContent || "{}");
    expect(bar.option.yAxis.scale).toBe(false);

    rerender(<AnalysisChartArtifact source={JSON.stringify({ ...JSON.parse(reference), type: "line" })} />);
    await waitFor(() => expect(screen.getByTestId("chart-option").textContent).toContain('"type":"line"'));
    const line = JSON.parse(screen.getByTestId("chart-option").textContent || "{}");
    expect(line.option.yAxis.scale).toBe(true);
    expect(line.description).toContain("可能不从零开始");
  });

  it("does not fetch model-supplied chart data", () => {
    render(<AnalysisChartArtifact source={JSON.stringify({
      ...JSON.parse(reference), points: [{ label: "East", value: "999" }],
    })} />);
    expect(chatApi.getAnalysisChart).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toContain("不能携带模型提供的数据");
  });

  it("does not describe a server-reviewed result as still pending", async () => {
    vi.mocked(chatApi.getAnalysisChart).mockResolvedValue({
      analysis_id: ANALYSIS_ID,
      dataset_id: "b".repeat(64),
      source_name: "sales.csv",
      source_sha256: "c".repeat(64),
      source_row_count: 1,
      sheet_name: null,
      analysis_plan: {},
      calculated_at: "2026-09-27T00:00:00Z",
      source_status: "current",
      semantic_review: "passed",
      warnings: [],
      x_column: "region",
      y_column: "revenue",
      points: [{ label: "East", value: "10.20" }],
    });

    render(<AnalysisChartArtifact source={reference} />);
    await waitFor(() => expect(screen.getByText("10.20")).toBeTruthy());
    expect(screen.queryByText(/业务语义待审阅/)).toBeNull();
    expect(screen.getByText(/业务语义已审阅/)).toBeTruthy();
  });
});
