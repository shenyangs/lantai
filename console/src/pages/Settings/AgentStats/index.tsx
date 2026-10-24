import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  ApiOutlined,
  ArrowRightOutlined,
  CheckCircleOutlined,
  DatabaseOutlined,
  MessageOutlined,
  ReloadOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { Column } from "@ant-design/plots";
import { DatePicker, Progress, Spin, Table, Tag, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import type { Dayjs } from "dayjs";
import dayjs from "dayjs";
import { useTranslation } from "react-i18next";
import { Button, Card } from "@/vendor/lantaiDesign";
import api from "../../../api";
import {
  settingsApi,
  type StorageRetentionCleanupPreview,
  type StorageRetentionSettings,
} from "../../../api/modules/settings";
import type {
  AgentStatsSummary,
  ModelUsageStats,
  ToolUsageStats,
} from "../../../api/types/agentStats";
import { PageHeader } from "@/components/PageHeader";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { formatCompact } from "../../../utils/formatNumber";
import { useTheme } from "../../../contexts/ThemeContext";
import { useAgentStore } from "../../../stores/agentStore";
import { createLatestRequestGate } from "../../../utils/latestRequest";
import { SettingsEmptyState } from "../components/SettingsEmptyState";
import { toolSuccessRateLabel } from "./presentation";
import { GeneratedImageRetentionRule } from "./GeneratedImageRetentionRule";
import styles from "./index.module.less";

type SectionKey = "overview" | "usage" | "tools" | "storage";

const sectionKeys = new Set<SectionKey>([
  "overview",
  "usage",
  "tools",
  "storage",
]);

const storageColors = [
  "#5f7ea3",
  "#d8a13a",
  "#8b7bb4",
  "#4f9b8f",
  "#55a7d8",
  "#d875a3",
  "#8292a7",
  "#b8a98b",
];

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(
    Math.floor(Math.log(value) / Math.log(1024)),
    units.length - 1,
  );
  const result = value / 1024 ** exponent;
  return `${result >= 100 || exponent === 0 ? result.toFixed(0) : result.toFixed(1)} ${units[exponent]}`;
}

function formatDuration(value: number): string {
  if (!value) return "—";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value >= 10_000 ? 0 : 1)} s`;
}

function safePercent(value: number): number {
  return Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
}

function AgentStatsPage() {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const { isDark: isDarkMode } = useTheme();
  const { selectedAgent } = useAgentStore();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSection = searchParams.get("section") as SectionKey | null;
  const [activeSection, setActiveSection] = useState<SectionKey>(
    requestedSection && sectionKeys.has(requestedSection)
      ? requestedSection
      : "overview",
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<AgentStatsSummary | null>(null);
  const [storagePolicy, setStoragePolicy] =
    useState<StorageRetentionSettings | null>(null);
  const [storagePreview, setStoragePreview] =
    useState<StorageRetentionCleanupPreview | null>(null);
  const [storageBusy, setStorageBusy] = useState(false);
  const [startDate, setStartDate] = useState<Dayjs>(dayjs().subtract(7, "day"));
  const [endDate, setEndDate] = useState<Dayjs>(dayjs());
  const dataLoadGateRef = useRef(createLatestRequestGate());

  const fetchData = useCallback(
    async (start: Dayjs, end: Dayjs) => {
      const ownsState = dataLoadGateRef.current.begin();
      setLoading(true);
      setError(null);
      try {
        const summary = await api.getAgentStats({
          start_date: start.format("YYYY-MM-DD"),
          end_date: end.format("YYYY-MM-DD"),
        });
        if (ownsState()) setData(summary);
      } catch (requestError) {
        if (!ownsState()) return;
        console.error("Failed to load runtime statistics:", requestError);
        const failureMessage = t("agentStats.loadFailed");
        message.error(failureMessage);
        setError(failureMessage);
      } finally {
        if (ownsState()) setLoading(false);
      }
    },
    [message, t],
  );

  useEffect(() => {
    setData(null);
    void fetchData(startDate, endDate);
    return () => dataLoadGateRef.current.invalidate();
  }, [endDate, fetchData, selectedAgent, startDate]);

  useEffect(() => {
    void settingsApi
      .getStorageRetentionSettings()
      .then(setStoragePolicy)
      .catch(() => undefined);
  }, []);

  const updateStoragePolicy = async (
    patch: Partial<StorageRetentionSettings>,
  ) => {
    if (!storagePolicy) return;
    const next = { ...storagePolicy, ...patch };
    setStoragePolicy(next);
    setStorageBusy(true);
    try {
      setStoragePolicy(await settingsApi.updateStorageRetentionSettings(next));
      setStoragePreview(null);
    } catch (storageError) {
      setStoragePolicy(storagePolicy);
      message.error(
        storageError instanceof Error
          ? storageError.message
          : "无法保存空间规则",
      );
    } finally {
      setStorageBusy(false);
    }
  };

  const previewStorageCleanup = async () => {
    setStorageBusy(true);
    try {
      setStoragePreview(await settingsApi.previewStorageCleanup());
    } catch (storageError) {
      message.error(
        storageError instanceof Error
          ? storageError.message
          : "无法生成清理预览",
      );
    } finally {
      setStorageBusy(false);
    }
  };

  const cleanStorage = async () => {
    if (!storagePreview) return;
    const reclaimable =
      storagePreview.logs_cache.bytes_reclaimable +
      storagePreview.runtime.bytes_reclaimable +
      storagePreview.generated_images.bytes_reclaimable;
    if (
      !window.confirm(
        `将清理约 ${formatBytes(reclaimable)} 的日志、缓存、已归档运行记录和过期生图临时文件。此操作不可恢复，是否继续？`,
      )
    )
      return;
    setStorageBusy(true);
    try {
      const result = await settingsApi.cleanStorage();
      const removed =
        result.logs_cache.files_removed +
        result.runtime.files_removed +
        result.generated_images.files_removed;
      message.success(
        removed ? `已清理 ${removed} 个文件` : "没有符合当前规则的可清理数据",
      );
      setStoragePreview(null);
      void fetchData(startDate, endDate);
    } catch (storageError) {
      message.error(
        storageError instanceof Error ? storageError.message : "清理失败",
      );
    } finally {
      setStorageBusy(false);
    }
  };

  useEffect(() => {
    if (requestedSection && sectionKeys.has(requestedSection)) {
      setActiveSection(requestedSection);
    }
  }, [requestedSection]);

  const changeSection = (section: SectionKey) => {
    setActiveSection(section);
    if (section === "overview") {
      setSearchParams({}, { replace: true });
    } else {
      setSearchParams({ section }, { replace: true });
    }
  };

  const handleDateChange = (dates: [Dayjs | null, Dayjs | null] | null) => {
    if (!dates?.[0] || !dates?.[1]) return;
    setStartDate(dates[0]);
    setEndDate(dates[1]);
  };

  const trendData = useMemo(
    () =>
      (data?.by_date ?? []).flatMap((item) => [
        {
          date: dayjs(item.date).format("MM-DD"),
          value: item.prompt_tokens,
          category: t("agentStats.promptTokens"),
        },
        {
          date: dayjs(item.date).format("MM-DD"),
          value: item.completion_tokens,
          category: t("agentStats.completionTokens"),
        },
      ]),
    [data?.by_date, t],
  );

  const activityData = useMemo(
    () =>
      (data?.by_date ?? []).flatMap((item) => [
        {
          date: dayjs(item.date).format("MM-DD"),
          value: item.llm_calls,
          category: t("agentStats.llmCalls"),
        },
        {
          date: dayjs(item.date).format("MM-DD"),
          value: item.tool_calls,
          category: t("agentStats.toolCalls"),
        },
      ]),
    [data?.by_date, t],
  );

  const totalToolCalls = data?.tool_health.total_calls ?? 0;

  const chartBase = useMemo(
    () => ({
      xField: "date",
      yField: "value",
      colorField: "category",
      seriesField: "category",
      isGroup: true,
      autoFit: true,
      height: 220,
      theme: isDarkMode ? "dark" : "light",
      legend: { position: "bottom" as const },
      axis: { y: { labelFormatter: formatCompact } },
      tooltip: {
        items: [
          (datum: { value: number; category: string }) => ({
            name: datum.category,
            value: datum.value.toLocaleString(),
          }),
        ],
      },
    }),
    [isDarkMode],
  );

  const modelColumns = useMemo<ColumnsType<ModelUsageStats>>(
    () => [
      {
        title: t("agentStats.model", "模型"),
        dataIndex: "model",
        key: "model",
        render: (value: string, row) => (
          <div className={styles.tableIdentity}>
            <span>{value}</span>
            <small>{row.provider_id}</small>
          </div>
        ),
      },
      {
        title: t("agentStats.calls", "调用"),
        dataIndex: "call_count",
        key: "call_count",
        align: "right",
        render: (value: number) => value.toLocaleString(),
      },
      {
        title: t("agentStats.input", "输入"),
        dataIndex: "prompt_tokens",
        key: "prompt_tokens",
        align: "right",
        render: (value: number) => formatCompact(value),
      },
      {
        title: t("agentStats.output", "输出"),
        dataIndex: "completion_tokens",
        key: "completion_tokens",
        align: "right",
        render: (value: number) => formatCompact(value),
      },
      {
        title: t("agentStats.total", "合计"),
        dataIndex: "total_tokens",
        key: "total_tokens",
        align: "right",
        render: (value: number) => <strong>{formatCompact(value)}</strong>,
      },
    ],
    [t],
  );

  const toolColumns = useMemo<ColumnsType<ToolUsageStats>>(
    () => [
      {
        title: t("agentStats.tool", "工具"),
        dataIndex: "name",
        key: "name",
        render: (value: string, row) => (
          <div className={styles.tableIdentity}>
            <span>{value}</span>
            {row.last_error ? (
              <Tooltip title={row.last_error}>
                <small className={styles.errorText}>
                  {t("agentStats.lastError", "最近有失败记录")}
                </small>
              </Tooltip>
            ) : (
              <small>{t("agentStats.healthy", "运行正常")}</small>
            )}
          </div>
        ),
      },
      {
        title: t("agentStats.calls", "调用"),
        dataIndex: "total_calls",
        key: "total_calls",
        align: "right",
      },
      {
        title: t("agentStats.successRate", "成功率"),
        key: "success_rate",
        align: "right",
        render: (_, row) => {
          const rate = row.total_calls
            ? (row.completed_calls / row.total_calls) * 100
            : 0;
          return (
            <Tag color={row.failed_calls ? "warning" : "success"}>
              {rate.toFixed(1)}%
            </Tag>
          );
        },
      },
      {
        title: t("agentStats.failures", "失败"),
        dataIndex: "failed_calls",
        key: "failed_calls",
        align: "right",
        render: (value: number) => (
          <span className={value ? styles.errorText : undefined}>{value}</span>
        ),
      },
      {
        title: t("agentStats.averageDuration", "平均耗时"),
        dataIndex: "average_duration_ms",
        key: "average_duration_ms",
        align: "right",
        render: (value: number) => formatDuration(value),
      },
      {
        title: "排查",
        key: "inspect",
        align: "right",
        render: (_, row) =>
          row.failed_calls ? (
            <Button
              type="text"
              size="small"
              onClick={() =>
                navigate(`/debug?query=${encodeURIComponent(row.name)}`)
              }
            >
              查看原因
            </Button>
          ) : (
            <span className={styles.healthyMark}>正常</span>
          ),
      },
    ],
    [navigate, t],
  );

  const totalTokens =
    (data?.total_prompt_tokens ?? 0) + (data?.total_completion_tokens ?? 0);
  const promptShare = totalTokens
    ? ((data?.total_prompt_tokens ?? 0) / totalTokens) * 100
    : 0;
  const completionShare = totalTokens ? 100 - promptShare : 0;
  const storage = data?.storage;
  const storageTotal = storage?.used_bytes ?? 0;
  const diskUsed =
    storage?.disk_total_bytes && storage.disk_free_bytes >= 0
      ? storage.disk_total_bytes - storage.disk_free_bytes
      : 0;
  const diskUsedPercent = storage?.disk_total_bytes
    ? (diskUsed / storage.disk_total_bytes) * 100
    : 0;

  const copyMetricScope = async () => {
    const summary = [
      `统计范围：${startDate.format("YYYY-MM-DD")} 至 ${endDate.format("YYYY-MM-DD")}`,
      `作用范围：当前智能体 ${selectedAgent}`,
      `Token：输入 ${data?.total_prompt_tokens ?? 0}，输出 ${data?.total_completion_tokens ?? 0}，缓存复用 ${data?.total_cache_tokens ?? 0}`,
      `工具调用：总计 ${data?.tool_health.total_calls ?? 0}，完成 ${data?.tool_health.completed_calls ?? 0}，失败 ${data?.tool_health.failed_calls ?? 0}，中断 ${data?.tool_health.interrupted_calls ?? 0}`,
      "说明：工具调用成功率不等于最终任务成功率。",
    ].join("\n");
    try {
      await navigator.clipboard.writeText(summary);
      message.success("已复制本期统计口径");
    } catch {
      message.error("复制失败");
    }
  };

  const renderOverview = () => (
    <>
      <div className={styles.heroGrid}>
        <Card className={`${styles.surfaceCard} ${styles.tokenHero}`}>
          <div className={styles.cardEyebrow}>
            <ThunderboltOutlined />
            {t("agentStats.tokenUsage", "模型用量")}
          </div>
          <div className={styles.heroValue}>{formatCompact(totalTokens)}</div>
          <div className={styles.heroUnit}>Token</div>
          <div className={styles.splitBar} aria-label="Token breakdown">
            <span style={{ width: `${promptShare}%` }} />
            <span style={{ width: `${completionShare}%` }} />
          </div>
          <div className={styles.splitLegend}>
            <span>
              <i className={styles.inputDot} />
              {t("agentStats.input", "输入")}{" "}
              <strong>{formatCompact(data?.total_prompt_tokens ?? 0)}</strong>
            </span>
            <span>
              <i className={styles.outputDot} />
              {t("agentStats.output", "输出")}{" "}
              <strong>
                {formatCompact(data?.total_completion_tokens ?? 0)}
              </strong>
            </span>
          </div>
          <button
            type="button"
            className={styles.inlineAction}
            onClick={() => changeSection("usage")}
          >
            {t("agentStats.viewUsageDetails", "查看模型与供应商明细")}
            <ArrowRightOutlined />
          </button>
        </Card>

        <Card className={styles.surfaceCard}>
          <div className={styles.cardEyebrow}>
            {totalToolCalls === 0 ? (
              <ApiOutlined />
            ) : (data?.tool_health.failed_calls ?? 0) > 0 ? (
              <WarningOutlined />
            ) : (
              <CheckCircleOutlined />
            )}
            {t("agentStats.toolHealth", "工具健康度")}
          </div>
          <div className={styles.healthRow}>
            <Progress
              type="circle"
              percent={safePercent(data?.tool_health.success_rate ?? 0)}
              size={92}
              strokeColor={
                (data?.tool_health.failed_calls ?? 0) > 0
                  ? "#d59b31"
                  : "#4f9b8f"
              }
              trailColor={isDarkMode ? "#2b3038" : "#edf0f3"}
              format={() =>
                toolSuccessRateLabel(
                  totalToolCalls,
                  data?.tool_health.success_rate ?? 0,
                )
              }
            />
            <div className={styles.healthCopy}>
              <strong>
                {totalToolCalls === 0
                  ? t("agentStats.noToolData", "所选时间内暂无工具调用")
                  : (data?.tool_health.failed_calls ?? 0) > 0
                    ? t("agentStats.needsAttention", "有少量调用需要留意")
                    : t("agentStats.allHealthy", "近期工具运行稳定")}
              </strong>
              <span>
                {t("agentStats.failures", "失败")}{" "}
                {data?.tool_health.failed_calls ?? 0} ·{" "}
                {t("agentStats.averageDuration", "平均耗时")}{" "}
                {formatDuration(data?.tool_health.average_duration_ms ?? 0)}
              </span>
            </div>
          </div>
          <button
            type="button"
            className={styles.inlineAction}
            onClick={() => changeSection("tools")}
          >
            {t("agentStats.inspectTools", "检查工具调用")}
            <ArrowRightOutlined />
          </button>
        </Card>
      </div>

      <div className={styles.metricStrip}>
        {[
          {
            icon: <ApiOutlined />,
            value: data?.total_llm_calls ?? 0,
            label: t("agentStats.llmCalls"),
          },
          {
            icon: <ThunderboltOutlined />,
            value: data?.total_tool_calls ?? 0,
            label: t("agentStats.toolCalls"),
          },
          {
            icon: <MessageOutlined />,
            value: data?.total_active_sessions ?? 0,
            label: t("agentStats.totalSessions"),
          },
          {
            icon: <MessageOutlined />,
            value: data?.total_messages ?? 0,
            label: t("agentStats.totalMessages"),
          },
        ].map((metric) => (
          <div className={styles.metricItem} key={metric.label}>
            <span className={styles.metricIcon}>{metric.icon}</span>
            <strong>{metric.value.toLocaleString()}</strong>
            <span>{metric.label}</span>
          </div>
        ))}
      </div>

      <div className={styles.twoColumnGrid}>
        <Card className={styles.surfaceCard} title={t("agentStats.tokenTrend")}>
          <div className={styles.chart}>
            <Column
              {...chartBase}
              data={trendData}
              meta={{ color: { range: ["#6887aa", "#5aa395"] } }}
            />
          </div>
        </Card>
        <Card
          className={styles.surfaceCard}
          title={t("agentStats.llmAndToolTrend")}
        >
          <div className={styles.chart}>
            <Column
              {...chartBase}
              data={activityData}
              meta={{ color: { range: ["#8b7bb4", "#d29a3a"] } }}
            />
          </div>
        </Card>
      </div>

      <div className={styles.twoColumnGrid}>
        <Card
          className={styles.surfaceCard}
          title={t("agentStats.providerOverview", "供应商概览")}
        >
          <div className={styles.rankList}>
            {(data?.provider_stats ?? []).slice(0, 5).map((provider) => (
              <div className={styles.rankRow} key={provider.provider_id}>
                <div>
                  <strong>{provider.provider_id}</strong>
                  <span>
                    {provider.model_count} {t("agentStats.models", "个模型")} ·{" "}
                    {provider.call_count.toLocaleString()}{" "}
                    {t("agentStats.calls", "次调用")}
                  </span>
                </div>
                <div className={styles.rankValue}>
                  {formatCompact(provider.total_tokens)}
                </div>
              </div>
            ))}
            {!data?.provider_stats?.length && (
              <div className={styles.mutedEmpty}>
                {t("agentStats.noProviderData", "暂无供应商用量记录")}
              </div>
            )}
          </div>
        </Card>
        <Card
          className={styles.surfaceCard}
          title={t("agentStats.cacheReuse", "上下文缓存复用")}
        >
          <div className={styles.cachePanel}>
            <div>
              <strong>{data?.cache_reuse_rate ?? 0}%</strong>
              <span>{t("agentStats.cacheReuseRate", "缓存复用率")}</span>
            </div>
            <Progress
              percent={safePercent(data?.cache_reuse_rate ?? 0)}
              showInfo={false}
              strokeColor="#8b7bb4"
              trailColor={isDarkMode ? "#2b3038" : "#edf0f3"}
            />
            <p>
              {t(
                "agentStats.cacheReuseHint",
                "复用了 {{tokens}} Token，可减少重复上下文传输。",
                {
                  tokens: formatCompact(data?.total_cache_tokens ?? 0),
                },
              )}
            </p>
          </div>
        </Card>
      </div>
    </>
  );

  const renderUsage = () => (
    <div className={styles.sectionStack}>
      <div className={styles.sectionIntro}>
        <div>
          <span>{t("agentStats.usageKicker", "模型与供应商")}</span>
          <h2>{t("agentStats.usageTitle", "看清用量花在了哪里")}</h2>
          <p>
            {t(
              "agentStats.usageDescription",
              "按模型、供应商和日期拆分调用量，不把 Token 用量与运行质量混在一起。",
            )}
          </p>
        </div>
        <div className={styles.sectionStat}>
          <strong>{formatCompact(totalTokens)}</strong>
          <span>{t("agentStats.totalTokens", "总 Token")}</span>
        </div>
      </div>
      <Card className={styles.surfaceCard}>
        <Table
          rowKey={(row) => `${row.provider_id}:${row.model}`}
          columns={modelColumns}
          dataSource={data?.model_stats ?? []}
          pagination={
            (data?.model_stats?.length ?? 0) > 8 ? { pageSize: 8 } : false
          }
          size="middle"
          scroll={{ x: 660 }}
          locale={{
            emptyText: t("agentStats.noUsageData", "暂无模型用量记录"),
          }}
        />
      </Card>
      <Card className={styles.surfaceCard} title={t("agentStats.tokenTrend")}>
        <div className={styles.chartLarge}>
          <Column
            {...chartBase}
            height={280}
            data={trendData}
            meta={{ color: { range: ["#6887aa", "#5aa395"] } }}
          />
        </div>
      </Card>
    </div>
  );

  const renderTools = () => (
    <div className={styles.sectionStack}>
      <div className={styles.sectionIntro}>
        <div>
          <span>{t("agentStats.diagnosticsKicker", "运行诊断")}</span>
          <h2>{t("agentStats.toolsTitle", "工具是否可靠、哪里变慢")}</h2>
          <p>
            {t(
              "agentStats.toolsDescription",
              "统计真实完成的工具调用、失败与耗时；失败记录保留最近一次错误摘要。",
            )}
          </p>
        </div>
        <div className={styles.sectionStat}>
          <strong>
            {toolSuccessRateLabel(
              totalToolCalls,
              data?.tool_health.success_rate ?? 0,
            )}
          </strong>
          <span>{t("agentStats.successRate", "成功率")}</span>
        </div>
      </div>
      <div className={styles.diagnosticStrip}>
        <div>
          <CheckCircleOutlined />
          <strong>{data?.tool_health.completed_calls ?? 0}</strong>
          <span>{t("agentStats.completed", "完成")}</span>
        </div>
        <div>
          <WarningOutlined />
          <strong>{data?.tool_health.failed_calls ?? 0}</strong>
          <span>{t("agentStats.failures", "失败")}</span>
        </div>
        <div>
          <ThunderboltOutlined />
          <strong>
            {formatDuration(data?.tool_health.average_duration_ms ?? 0)}
          </strong>
          <span>{t("agentStats.averageDuration", "平均耗时")}</span>
        </div>
      </div>
      {(data?.tool_health.by_tool.length ?? 0) > 0 ? (
        <Card className={styles.surfaceCard}>
          <Table
            rowKey="name"
            columns={toolColumns}
            dataSource={data?.tool_health.by_tool ?? []}
            pagination={
              (data?.tool_health.by_tool.length ?? 0) > 10
                ? { pageSize: 10 }
                : false
            }
            size="middle"
            scroll={{ x: 660 }}
          />
        </Card>
      ) : (
        <SettingsEmptyState
          title={t("agentStats.noToolData", "所选时间内暂无工具调用")}
          description={t(
            "agentStats.noToolDataHint",
            "工具完成后会在这里显示成功率、耗时与失败摘要。",
          )}
        />
      )}
      <button
        type="button"
        className={styles.inlineAction}
        onClick={() => navigate("/debug")}
      >
        {t("agentStats.openDebug", "打开完整运行日志")}
        <ArrowRightOutlined />
      </button>
    </div>
  );

  const renderStorage = () => (
    <div className={styles.sectionStack}>
      <div className={styles.sectionIntro}>
        <div>
          <span>{t("agentStats.localStorage", "本地数据空间")}</span>
          <h2>{t("agentStats.storageTitle", "澜台在这台设备上的占用")}</h2>
          <p>
            {t(
              "agentStats.storageDescription",
              "按备份、缓存、会话、记忆和工作区文件分类；清理由对应管理页执行。",
            )}
          </p>
        </div>
        <div className={styles.sectionStat}>
          <strong>{formatBytes(storageTotal)}</strong>
          <span>{t("agentStats.appUsed", "澜台已用")}</span>
        </div>
      </div>

      {storage ? (
        <>
          <Card className={styles.surfaceCard}>
            <div className={styles.storageHeader}>
              <div>
                <strong>
                  {formatBytes(storage.disk_free_bytes)}{" "}
                  {t("agentStats.available", "可用")}
                </strong>
                <span>
                  {storage.file_count.toLocaleString()}{" "}
                  {t("agentStats.files", "个文件")} ·{" "}
                  {storage.directory_count.toLocaleString()}{" "}
                  {t("agentStats.directories", "个目录")}
                </span>
              </div>
              <span>{safePercent(diskUsedPercent).toFixed(1)}%</span>
            </div>
            <Progress
              percent={safePercent(diskUsedPercent)}
              showInfo={false}
              strokeColor="#5f7ea3"
              trailColor={isDarkMode ? "#2b3038" : "#edf0f3"}
            />
            <div className={styles.storageBar}>
              {storage.categories
                .filter((category) => category.bytes > 0)
                .map((category, index) => (
                  <Tooltip
                    key={category.key}
                    title={`${category.label} · ${formatBytes(category.bytes)}`}
                  >
                    <span
                      style={{
                        width: `${
                          storageTotal
                            ? (category.bytes / storageTotal) * 100
                            : 0
                        }%`,
                        background: storageColors[index % storageColors.length],
                      }}
                    />
                  </Tooltip>
                ))}
            </div>
          </Card>

          <section
            className={styles.retentionPanel}
            aria-labelledby="retention-title"
          >
            <div>
              <h3 id="retention-title">自动清理</h3>
              <p>
                应用启动时按以下规则清理日志、缓存、已归档运行记录和生图临时文件。会话、记忆、作品、附件、已下载图片和备份始终保留。
              </p>
            </div>
            {storagePolicy ? (
              <div className={styles.retentionRules}>
                <label className={styles.retentionRule}>
                  <span>日志与缓存</span>
                  <select
                    value={storagePolicy.logs_cache_days}
                    disabled={storageBusy}
                    onChange={(event) =>
                      void updateStoragePolicy({
                        logs_cache_days: Number(event.target.value),
                      })
                    }
                  >
                    <option value={7}>保留 7 天</option>
                    <option value={14}>保留 14 天</option>
                    <option value={30}>保留 30 天</option>
                    <option value={0}>不按天清理</option>
                  </select>
                  <select
                    value={storagePolicy.logs_cache_max_bytes}
                    disabled={storageBusy}
                    onChange={(event) =>
                      void updateStoragePolicy({
                        logs_cache_max_bytes: Number(event.target.value),
                      })
                    }
                  >
                    <option value={104857600}>上限 100 MB</option>
                    <option value={209715200}>上限 200 MB</option>
                    <option value={524288000}>上限 500 MB</option>
                    <option value={0}>不设容量上限</option>
                  </select>
                </label>
                <label className={styles.retentionRule}>
                  <span>运行记录</span>
                  <select
                    value={storagePolicy.runtime_days}
                    disabled={storageBusy}
                    onChange={(event) =>
                      void updateStoragePolicy({
                        runtime_days: Number(event.target.value),
                      })
                    }
                  >
                    <option value={14}>保留 14 天</option>
                    <option value={30}>保留 30 天</option>
                    <option value={90}>保留 90 天</option>
                    <option value={0}>不按天清理</option>
                  </select>
                  <select
                    value={storagePolicy.runtime_max_bytes}
                    disabled={storageBusy}
                    onChange={(event) =>
                      void updateStoragePolicy({
                        runtime_max_bytes: Number(event.target.value),
                      })
                    }
                  >
                    <option value={262144000}>上限 250 MB</option>
                    <option value={524288000}>上限 500 MB</option>
                    <option value={2147483648}>上限 2 GB</option>
                    <option value={0}>不设容量上限</option>
                  </select>
                </label>
                <GeneratedImageRetentionRule
                  days={storagePolicy.generated_images_days}
                  disabled={storageBusy}
                  onChange={(days) =>
                    void updateStoragePolicy({ generated_images_days: days })
                  }
                />
                <div className={styles.retentionActions}>
                  <button
                    type="button"
                    onClick={() => void previewStorageCleanup()}
                    disabled={storageBusy}
                  >
                    查看可清理内容
                  </button>
                  <button
                    type="button"
                    className={styles.cleanupAction}
                    onClick={() => void cleanStorage()}
                    disabled={storageBusy || !storagePreview}
                  >
                    清理
                  </button>
                </div>
                {storagePreview ? (
                  <p className={styles.cleanupPreview}>
                    预计释放{" "}
                    {formatBytes(
                      storagePreview.logs_cache.bytes_reclaimable +
                        storagePreview.runtime.bytes_reclaimable +
                        storagePreview.generated_images.bytes_reclaimable,
                    )}{" "}
                    · 日志与缓存 {storagePreview.logs_cache.files_reclaimable}{" "}
                    个文件 · 运行记录 {storagePreview.runtime.files_reclaimable}{" "}
                    个文件 · 生图临时文件{" "}
                    {storagePreview.generated_images.files_reclaimable} 个文件
                  </p>
                ) : null}
              </div>
            ) : null}
          </section>

          <div className={styles.storageList}>
            {storage.categories.map((category, index) => (
              <div className={styles.storageRow} key={category.key}>
                <i
                  style={{
                    background: storageColors[index % storageColors.length],
                  }}
                />
                <div>
                  <strong>{category.label}</strong>
                  <span>
                    {category.file_count.toLocaleString()}{" "}
                    {t("agentStats.files", "个文件")} ·{" "}
                    {category.directory_count.toLocaleString()}{" "}
                    {t("agentStats.directories", "个目录")}
                  </span>
                </div>
                <b>{formatBytes(category.bytes)}</b>
                {category.key === "backups" ? (
                  <button type="button" onClick={() => navigate("/backups")}>
                    {t("agentStats.manage", "管理")}
                  </button>
                ) : category.key === "logs_cache" ? (
                  <button type="button" onClick={() => navigate("/debug")}>
                    {t("agentStats.inspect", "查看")}
                  </button>
                ) : null}
              </div>
            ))}
          </div>
          <div className={styles.pathNote}>
            <DatabaseOutlined />
            <span title={storage.root_path}>{storage.root_path}</span>
            {storage.truncated && (
              <Tag color="warning">
                {t("agentStats.partialScan", "文件较多，当前为抽样统计")}
              </Tag>
            )}
          </div>
        </>
      ) : (
        <SettingsEmptyState
          title={t("agentStats.noStorageData", "暂时无法读取本地空间")}
          description={t(
            "agentStats.noStorageDataHint",
            "刷新后仍无数据时，可前往运行调试查看目录访问状态。",
          )}
        />
      )}
    </div>
  );

  return (
    <div className={styles.page}>
      <PageHeader
        parent={t("nav.settings")}
        current={t("agentStats.title")}
        description={t("agentStats.description")}
      />
      <div className={styles.content}>
        <div className={styles.toolbar}>
          <div
            className={styles.sectionTabs}
            role="tablist"
            aria-label={t("agentStats.title")}
          >
            {(
              [
                ["overview", t("agentStats.overview", "总览")],
                ["usage", t("agentStats.usage", "用量")],
                ["tools", t("agentStats.tools", "工具")],
                ["storage", t("agentStats.storage", "本地空间")],
              ] as [SectionKey, string][]
            ).map(([key, label]) => (
              <button
                type="button"
                role="tab"
                aria-selected={activeSection === key}
                className={activeSection === key ? styles.activeTab : ""}
                key={key}
                onClick={() => changeSection(key)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className={styles.filters}>
            <DatePicker.RangePicker
              value={[startDate, endDate]}
              onChange={handleDateChange}
              className={styles.datePicker}
              disabled={loading}
              disabledDate={(current) =>
                current && current.isAfter(dayjs(), "day")
              }
              allowClear={false}
            />
            <Button
              icon={<ReloadOutlined spin={loading} />}
              onClick={() => void fetchData(startDate, endDate)}
              disabled={loading}
            >
              {t("agentStats.refresh", "刷新")}
            </Button>
          </div>
        </div>

        {activeSection !== "storage" ? (
          <aside className={styles.metricScope} aria-label="统计口径">
            <div>
              <strong>
                {startDate.format("YYYY-MM-DD")} 至{" "}
                {endDate.format("YYYY-MM-DD")}
              </strong>
              <span>当前智能体 · {selectedAgent}</span>
            </div>
            <p>
              Token
              合计只含模型输入与输出；缓存复用单独统计。工具健康度的分母是工具调用次数，重试中的单次失败也会计入，它不代表最终任务是否成功。
            </p>
            <div className={styles.metricScopeActions}>
              <button type="button" onClick={() => void copyMetricScope()}>
                复制口径
              </button>
            </div>
          </aside>
        ) : null}

        {error && !data ? (
          <div className={styles.error}>
            <WarningOutlined />
            <p>{error}</p>
            <Button
              type="primary"
              onClick={() => void fetchData(startDate, endDate)}
            >
              {t("agentStats.retry")}
            </Button>
          </div>
        ) : loading && !data ? (
          <div className={styles.loading}>
            <Spin size="large" />
            <p>{t("common.loading")}</p>
          </div>
        ) : data ? (
          <>
            {activeSection === "overview" && renderOverview()}
            {activeSection === "usage" && renderUsage()}
            {activeSection === "tools" && renderTools()}
            {activeSection === "storage" && renderStorage()}
          </>
        ) : (
          <SettingsEmptyState
            title={t("agentStats.noData")}
            description={t("agentStats.noDataHint")}
          />
        )}
      </div>
    </div>
  );
}

export default AgentStatsPage;
