import { useEffect, useMemo, useRef, useState } from "react";
import { Tooltip } from "antd";
import { AlertTriangle, BarChart3, Check, Copy, Download } from "lucide-react";
import * as echarts from "echarts/core";
import {
  BarChart,
  LineChart,
  PieChart,
  RadarChart,
  ScatterChart,
} from "echarts/charts";
import {
  DataZoomComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  TransformComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

import { useAppMessage } from "../../../hooks/useAppMessage";
import { copyText } from "../utils";
import { parseEChartsArtifact } from "./echartsArtifactProtocol";
import styles from "./EChartsArtifact.module.less";

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  RadarChart,
  ScatterChart,
  DataZoomComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  MarkPointComponent,
  TitleComponent,
  ToolboxComponent,
  TooltipComponent,
  TransformComponent,
  VisualMapComponent,
  CanvasRenderer,
]);

export function EChartsArtifact({ source }: { source: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.EChartsType | null>(null);
  const { message } = useAppMessage();
  const [copied, setCopied] = useState(false);
  const [renderError, setRenderError] = useState("");
  const [ready, setReady] = useState(false);
  const parsed = useMemo(() => {
    try {
      return { value: parseEChartsArtifact(source), error: "" };
    } catch (error) {
      return {
        value: null,
        error: error instanceof Error ? error.message : "无法解析图表配置",
      };
    }
  }, [source]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !parsed.value) return;

    setRenderError("");
    setReady(false);
    let chart: echarts.EChartsType | null = null;
    let observer: ResizeObserver | null = null;
    const resize = () => chart?.resize();

    try {
      chart = echarts.init(container, undefined, { renderer: "canvas" });
      chart.setOption(parsed.value.option);
      chartRef.current = chart;
      setReady(true);
      if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(resize);
        observer.observe(container);
      } else {
        window.addEventListener("resize", resize);
      }
    } catch (error) {
      setRenderError(error instanceof Error ? error.message : "图表渲染失败");
    }

    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", resize);
      chart?.dispose();
      if (chartRef.current === chart) chartRef.current = null;
      setReady(false);
    };
  }, [parsed]);

  const copy = async () => {
    try {
      await copyText(source);
      setCopied(true);
      message.success("图表配置已复制");
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      message.error("复制失败");
    }
  };

  const download = () => {
    const chart = chartRef.current;
    if (!chart) return;
    try {
      const anchor = document.createElement("a");
      anchor.href = chart.getDataURL({
        type: "png",
        pixelRatio: 2,
        backgroundColor: "#ffffff",
      });
      anchor.download = `${parsed.value?.title || "澜台图表"}.png`.replace(
        /[<>:"/\\|?*]/g,
        "-",
      );
      anchor.click();
    } catch {
      message.error("图表导出失败");
    }
  };

  const error = parsed.error || renderError;
  return (
    <section
      className={styles.artifact}
      data-lantai-artifact="echarts"
      aria-label={parsed.value?.title || "数据图表"}
    >
      <header className={styles.header}>
        <div className={styles.heading}>
          <div className={styles.title}>
            <BarChart3 size={16} aria-hidden="true" />
            <span>{parsed.value?.title || "数据图表"}</span>
          </div>
          {parsed.value?.description ? (
            <p className={styles.description}>{parsed.value.description}</p>
          ) : null}
        </div>
        <div className={styles.actions}>
          <Tooltip title={copied ? "已复制配置" : "复制配置"}>
            <button
              type="button"
              className={styles.action}
              aria-label={copied ? "已复制配置" : "复制图表配置"}
              onClick={copy}
            >
              {copied ? (
                <Check size={15} aria-hidden="true" />
              ) : (
                <Copy size={15} aria-hidden="true" />
              )}
            </button>
          </Tooltip>
          <Tooltip title="下载 PNG">
            <button
              type="button"
              className={styles.action}
              aria-label="下载图表 PNG"
              onClick={download}
              disabled={!ready || Boolean(error)}
            >
              <Download size={15} aria-hidden="true" />
            </button>
          </Tooltip>
        </div>
      </header>
      {error ? (
        <div className={styles.error} role="alert">
          <div className={styles.errorTitle}>
            <AlertTriangle size={16} aria-hidden="true" />
            <span>{error}</span>
          </div>
          <pre>{source}</pre>
        </div>
      ) : (
        <div
          ref={containerRef}
          className={styles.canvas}
          style={{ height: parsed.value?.height }}
          role="img"
          aria-label={`${parsed.value?.title || "数据图表"}，交互式图表`}
        />
      )}
    </section>
  );
}
