export interface AnalysisChartReference {
  analysis_id: string;
  x_column: string;
  y_column: string;
  type: "bar" | "line";
}

const ALLOWED_KEYS = new Set(["analysis_id", "x_column", "y_column", "type"]);

export function parseAnalysisChartReference(source: string): AnalysisChartReference {
  if (!source.trim() || source.length > 2048) {
    throw new Error("分析图表引用为空或过长");
  }
  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    throw new Error("分析图表引用不是有效 JSON");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("分析图表引用必须是对象");
  }
  const fields = value as Record<string, unknown>;
  if (Object.keys(fields).some((key) => !ALLOWED_KEYS.has(key))) {
    throw new Error("分析图表引用不能携带模型提供的数据或其他配置");
  }
  if (typeof fields.analysis_id !== "string" || !/^[0-9a-f]{64}$/.test(fields.analysis_id)) {
    throw new Error("分析结果 ID 无效");
  }
  for (const key of ["x_column", "y_column"] as const) {
    if (typeof fields[key] !== "string" || !fields[key].trim() || fields[key].length > 120) {
      throw new Error("分析图表列名无效");
    }
  }
  if (fields.x_column === fields.y_column || !["bar", "line"].includes(String(fields.type))) {
    throw new Error("分析图表类型或列名无效");
  }
  return fields as unknown as AnalysisChartReference;
}
