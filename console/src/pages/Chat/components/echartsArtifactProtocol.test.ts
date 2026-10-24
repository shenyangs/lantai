import { describe, expect, it } from "vitest";

import {
  ECHARTS_DEPTH_LIMIT,
  ECHARTS_NODE_LIMIT,
  ECHARTS_SOURCE_LIMIT,
  parseEChartsArtifact,
} from "./echartsArtifactProtocol";

describe("parseEChartsArtifact", () => {
  it("accepts a plain ECharts option and infers its title", () => {
    const result = parseEChartsArtifact(
      JSON.stringify({
        title: { text: "月度收入" },
        xAxis: { type: "category", data: ["一月", "二月"] },
        yAxis: { type: "value" },
        series: [{ type: "bar", data: [12, 18] }],
      }),
    );

    expect(result.title).toBe("月度收入");
    expect(result.height).toBe(360);
    expect(result.option.series).toEqual([{ type: "bar", data: [12, 18] }]);
  });

  it("accepts the Lantai envelope and clamps its height", () => {
    const result = parseEChartsArtifact(
      JSON.stringify({
        title: "渠道占比",
        description: "按有效订单统计",
        height: 9999,
        option: {
          series: [{ type: "pie", data: [{ name: "自然", value: 7 }] }],
        },
      }),
    );

    expect(result.title).toBe("渠道占比");
    expect(result.description).toBe("按有效订单统计");
    expect(result.height).toBe(720);
  });

  it.each([
    ['{"series":[{"type":"custom","constructor":{}}]}', "不安全字段"],
    [
      '{"series":[{"type":"pie","label":{"formatter":"<script>x"}}]}',
      "不安全文本",
    ],
    ['{"series":[{"symbol":"image://https://example.com/a.png"}]}', "外部图像"],
    ["[]", "JSON 对象"],
    ["not json", "有效的 JSON"],
  ])("rejects unsafe or invalid input: %s", (source, message) => {
    expect(() => parseEChartsArtifact(source)).toThrow(message);
  });

  it("enforces source, depth, and node budgets", () => {
    expect(() =>
      parseEChartsArtifact(`{"value":"${"x".repeat(ECHARTS_SOURCE_LIMIT)}"}`),
    ).toThrow("200 KB");

    let deep: Record<string, unknown> = {};
    const root = deep;
    for (let index = 0; index <= ECHARTS_DEPTH_LIMIT; index += 1) {
      deep.next = {};
      deep = deep.next as Record<string, unknown>;
    }
    expect(() => parseEChartsArtifact(JSON.stringify(root))).toThrow(
      "嵌套过深",
    );

    expect(() =>
      parseEChartsArtifact(
        JSON.stringify({
          data: Array.from({ length: ECHARTS_NODE_LIMIT + 1 }, () => 1),
        }),
      ),
    ).toThrow("过于复杂");
  });
});
