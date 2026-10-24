import { describe, expect, it } from "vitest";
import { parseAnalysisChartReference } from "./analysisChartProtocol";

const ANALYSIS_ID = "a".repeat(64);

describe("analysis chart reference", () => {
  it("accepts only a result reference and column choices", () => {
    expect(parseAnalysisChartReference(JSON.stringify({
      analysis_id: ANALYSIS_ID,
      x_column: "region",
      y_column: "revenue",
      type: "bar",
    }))).toEqual({
      analysis_id: ANALYSIS_ID,
      x_column: "region",
      y_column: "revenue",
      type: "bar",
    });
  });

  it("rejects model-supplied data and unknown display fields", () => {
    expect(() => parseAnalysisChartReference(JSON.stringify({
      analysis_id: ANALYSIS_ID,
      x_column: "region",
      y_column: "revenue",
      type: "bar",
      points: [{ label: "East", value: "999" }],
    }))).toThrow();
  });

  it("rejects malformed result IDs and chart types", () => {
    expect(() => parseAnalysisChartReference(JSON.stringify({
      analysis_id: "../../private",
      x_column: "region",
      y_column: "revenue",
      type: "scatter",
    }))).toThrow();
  });
});
