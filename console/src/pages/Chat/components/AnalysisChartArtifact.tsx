import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, BarChart3 } from "lucide-react";

import { chatApi } from "@/api";
import type { AnalysisChartResponse } from "../../../api/modules/chat";
import { EChartsArtifact } from "./EChartsArtifact";
import { parseAnalysisChartReference } from "./analysisChartProtocol";
import styles from "./AnalysisChartArtifact.module.less";

function currentChatId(): string {
  const match = (window.location.hash || "").match(/(?:^#)?\/chat\/([^/?#]+)/);
  if (!match?.[1]) return "";
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return "";
  }
}

function chartOption(chart: AnalysisChartResponse, type: "bar" | "line"): string {
  return JSON.stringify({
    title: `${chart.y_column} · ${chart.x_column}`,
    description: type === "line"
      ? "折线图纵轴可能不从零开始；下方数据依据表保留封存结果的精确值。"
      : "柱状图纵轴包含零基线；下方数据依据表保留封存结果的精确值。",
    option: {
      animation: false,
      grid: { left: 72, right: 28, top: 36, bottom: 76, containLabel: true },
      xAxis: { type: "category", name: chart.x_column, data: chart.points.map((point) => point.label), axisLabel: { rotate: chart.points.length > 8 ? 35 : 0 } },
      yAxis: { type: "value", name: chart.y_column, scale: type === "line" },
      series: [{ type, data: chart.points.map((point) => Number(point.value)), showSymbol: type === "line" }],
    },
  });
}

export function AnalysisChartArtifact({ source }: { source: string }) {
  const parsed = useMemo(() => {
    try {
      return { reference: parseAnalysisChartReference(source), error: "" };
    } catch (error) {
      return { reference: null, error: error instanceof Error ? error.message : "分析图表引用无效" };
    }
  }, [source]);
  const chatId = currentChatId();
  const [chart, setChart] = useState<AnalysisChartResponse | null>(null);
  const [fetchError, setFetchError] = useState("");

  useEffect(() => {
    if (!parsed.reference || !chatId) return;
    const controller = new AbortController();
    const { analysis_id, x_column, y_column } = parsed.reference;
    setChart(null);
    setFetchError("");
    chatApi.getAnalysisChart(chatId, analysis_id, x_column, y_column, controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return;
        if (response.analysis_id !== analysis_id || response.x_column !== x_column || response.y_column !== y_column) {
          setFetchError("图表结果引用与返回数据不一致");
          return;
        }
        setChart(response);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFetchError("无法读取这段会话的封存分析结果");
      });
    return () => controller.abort();
  }, [chatId, parsed.reference]);

  const error = parsed.error || (chatId ? fetchError : "当前页面没有对应的聊天会话");
  if (error) {
    return <section className={styles.error} role="alert"><AlertTriangle size={16} aria-hidden="true" />{error}</section>;
  }
  if (!chart || !parsed.reference) {
    return <section className={styles.loading} role="status">正在读取分析结果并绘图…</section>;
  }
  return (
    <section className={styles.artifact} data-lantai-artifact="analysis-chart">
      <div className={styles.heading}><BarChart3 size={16} aria-hidden="true" />数据图表 · 已绑定分析结果</div>
      {chart.source_status !== "current" ? (
        <p className={styles.notice} role="status">原始数据已{chart.source_status === "changed" ? "变化" : "不可用"}；图表仍显示原版本封存结果。刷新分析可创建新版本。</p>
      ) : null}
      <EChartsArtifact source={chartOption(chart, parsed.reference.type)} />
      <details className={styles.evidence} open>
        <summary>数据依据</summary>
        <dl>
          <dt>来源</dt><dd>{chart.source_name}{chart.sheet_name ? ` · ${chart.sheet_name}` : ""}</dd>
          <dt>源数据行数</dt><dd>{chart.source_row_count}</dd>
          <dt>分析 ID</dt><dd><code>{chart.analysis_id}</code></dd>
          <dt>源 SHA-256</dt><dd><code>{chart.source_sha256}</code></dd>
          <dt>计算时间</dt><dd>{chart.calculated_at}</dd>
        </dl>
        <p className={styles.notice}>
          {chart.semantic_review === "passed"
            ? "业务语义已审阅；具体结论仍应与计算计划及来源版本一起使用。"
            : chart.semantic_review === "failed"
              ? "业务语义审阅未通过：请先纠正口径、粒度或解释，不要据此下结论。"
              : "业务语义待审阅：口径、粒度和解释需要结合业务上下文核对。"}
        </p>
        {chart.warnings.length ? <p className={styles.notice}>{chart.warnings.join("；")}</p> : null}
        <table>
          <caption>封存分析结果的精确值；图形坐标可能因浏览器浮点绘制而近似</caption>
          <thead><tr><th scope="col">{chart.x_column}</th><th scope="col">{chart.y_column}</th></tr></thead>
          <tbody>{chart.points.map((point) => <tr key={point.label}><th scope="row">{point.label}</th><td>{point.value}</td></tr>)}</tbody>
        </table>
        <details><summary>计算计划</summary><pre>{JSON.stringify(chart.analysis_plan, null, 2)}</pre></details>
      </details>
    </section>
  );
}
