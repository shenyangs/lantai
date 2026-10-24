import { useTranslation } from "react-i18next";
import { Button, Input, Select, Space, Switch, Tag, Typography } from "antd";
import dayjs from "dayjs";
import { PageHeader } from "@/components/PageHeader";
import { useDebugLogs, backendLevelColor } from "./useDebugLogs";
import { LogViewer } from "./components";
import { SettingsEmptyState } from "../components/SettingsEmptyState";
import { SettingsNotice } from "../components/SettingsNotice";
import styles from "./index.module.less";
import { useEffect, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

const { Text } = Typography;

export default function DebugPage() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const {
    backendLogs,
    turnEvents,
    initialLoading,
    backendError,
    turnEventsError,
    autoRefresh,
    setAutoRefresh,
    backendNewestFirst,
    setBackendNewestFirst,
    backendLevel,
    setBackendLevel,
    backendQuery,
    setBackendQuery,
    filteredBackendLines,
    loadBackendLogs,
    handleCopyBackend,
    turnEventItems,
    loadTurnEvents,
    handleCopyTurnEvents,
  } = useDebugLogs();
  const backendEndpointDisabled = backendError
    .toLowerCase()
    .includes("debug log endpoint is disabled");
  useEffect(() => {
    const query = searchParams.get("query")?.trim();
    if (query) setBackendQuery(query);
  }, [searchParams, setBackendQuery]);
  const filteredTurnEventItems = useMemo(() => {
    const query = backendQuery.trim().toLocaleLowerCase();
    if (!query) return turnEventItems;
    return turnEventItems.filter((item) =>
      [item.label, item.detail, item.chat_id, item.scope, item.workspace_dir]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase()
        .includes(query),
    );
  }, [backendQuery, turnEventItems]);

  return (
    <div className={styles.debugPage}>
      <PageHeader
        parent={t("nav.settings")}
        current={t("debug.title", "Debug")}
        description={t("debug.desc")}
      />

      <div className={styles.content}>
        <section className={styles.debugPanel}>
          <header className={styles.debugPanelHeader}>
            <h2>{t("debug.backend.title", "Backend logs")}</h2>
            {!backendEndpointDisabled ? (
              <Space size="middle">
                <Text type="secondary">
                  {t("debug.backend.newestFirst", "Newest first")}
                </Text>
                <Switch
                  checked={backendNewestFirst}
                  onChange={setBackendNewestFirst}
                  aria-label={t("debug.backend.newestFirst", "Newest first")}
                />
                <Text type="secondary">
                  {t("debug.backend.autoRefresh", "Auto refresh")}
                </Text>
                <Switch
                  checked={autoRefresh}
                  onChange={setAutoRefresh}
                  aria-label={t("debug.backend.autoRefresh", "Auto refresh")}
                />
              </Space>
            ) : (
              <Tag color="warning">当前不可用</Tag>
            )}
          </header>
          <div className={styles.debugPanelBody}>
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
              {!backendEndpointDisabled ? (
                <div className={styles.toolbar}>
                  <div className={styles.toolbarLeft}>
                    <Select
                      className={styles.levelSelect}
                      value={backendLevel}
                      onChange={(v) => setBackendLevel(v)}
                      options={[
                        { value: "all", label: t("debug.level.all", "All") },
                        {
                          value: "error",
                          label: (
                            <Tag color={backendLevelColor("error")}>ERROR</Tag>
                          ),
                        },
                        {
                          value: "warning",
                          label: (
                            <Tag color={backendLevelColor("warning")}>
                              WARNING
                            </Tag>
                          ),
                        },
                        {
                          value: "info",
                          label: (
                            <Tag color={backendLevelColor("info")}>INFO</Tag>
                          ),
                        },
                        {
                          value: "debug",
                          label: (
                            <Tag color={backendLevelColor("debug")}>DEBUG</Tag>
                          ),
                        },
                      ]}
                    />
                    <Input
                      className={styles.searchInput}
                      value={backendQuery}
                      onChange={(e) => setBackendQuery(e.target.value)}
                      placeholder={t(
                        "debug.backend.searchPlaceholder",
                        "Search backend logs...",
                      )}
                      allowClear
                    />
                    {backendLogs?.updated_at && (
                      <Text type="secondary">
                        {t("debug.backend.updatedAt", "Updated at")}:{" "}
                        {dayjs(backendLogs.updated_at * 1000).format(
                          "YYYY-MM-DD HH:mm:ss",
                        )}
                      </Text>
                    )}
                  </div>
                  <div className={styles.toolbarRight}>
                    <Button
                      onClick={() =>
                        void loadBackendLogs({ successToast: true })
                      }
                    >
                      {t(
                        "debug.actions.refreshBackend",
                        "Refresh backend logs",
                      )}
                    </Button>
                    <Button onClick={() => void handleCopyBackend()}>
                      {t("debug.actions.copyBackend", "Copy backend logs")}
                    </Button>
                  </div>
                </div>
              ) : null}

              {!backendEndpointDisabled && backendLogs?.path && (
                <div className={styles.logPath}>
                  <Text type="secondary" className={styles.logPathLabel}>
                    {t("debug.backend.path", "Log file")}
                  </Text>
                  <code className={styles.logPathValue}>
                    {backendLogs.path}
                  </code>
                </div>
              )}

              {backendError ? (
                <SettingsNotice
                  tone={backendEndpointDisabled ? "warning" : "error"}
                  title={
                    backendEndpointDisabled
                      ? t("debug.backend.disabledTitle", "后端日志接口未启用")
                      : t("debug.backend.loadFailed", "加载后端日志失败")
                  }
                >
                  {backendEndpointDisabled ? (
                    <div className={styles.disabledRecovery}>
                      <span>
                        {t(
                          "debug.backend.disabledDesc",
                          "当前运行模式未开放后端日志读取，不影响正常聊天和任务执行。",
                        )}
                      </span>
                      <div>
                        <Button
                          size="small"
                          onClick={() =>
                            document
                              .getElementById("turn-events-panel")
                              ?.scrollIntoView({
                                behavior: "smooth",
                                block: "start",
                              })
                          }
                        >
                          查看可用的运行事件
                        </Button>
                        <Button
                          size="small"
                          disabled={!turnEventItems.length}
                          onClick={() => void handleCopyTurnEvents()}
                        >
                          复制运行事件
                        </Button>
                      </div>
                    </div>
                  ) : (
                    backendError
                  )}
                </SettingsNotice>
              ) : !backendLogs?.exists ? (
                <SettingsNotice
                  title={t(
                    "debug.backend.notFound",
                    "Backend log file was not found yet.",
                  )}
                  tone="warning"
                />
              ) : null}

              {!backendEndpointDisabled ? (
                <LogViewer
                  lines={filteredBackendLines}
                  query={backendQuery}
                  loading={initialLoading}
                />
              ) : null}
            </Space>
          </div>
        </section>

        <section className={styles.debugPanel} id="turn-events-panel">
          <header className={styles.debugPanelHeader}>
            <h2>{t("debug.turnEvents.title", "Recent turn events")}</h2>
            <Space size="middle">
              <Input
                className={styles.eventSearchInput}
                value={backendQuery}
                onChange={(event) => setBackendQuery(event.target.value)}
                placeholder="筛选运行事件"
                allowClear
                aria-label="筛选运行事件"
              />
              {turnEvents?.updated_at && (
                <Text type="secondary">
                  {t("debug.backend.updatedAt", "Updated at")}:{" "}
                  {dayjs(turnEvents.updated_at * 1000).format(
                    "YYYY-MM-DD HH:mm:ss",
                  )}
                </Text>
              )}
              <Button
                onClick={() => void loadTurnEvents({ successToast: true })}
              >
                {t("debug.turnEvents.refresh", "Refresh events")}
              </Button>
              <Button onClick={() => void handleCopyTurnEvents()}>
                {t("debug.turnEvents.copy", "Copy events")}
              </Button>
            </Space>
          </header>
          <div className={styles.debugPanelBody}>
            <Space direction="vertical" size="middle" style={{ width: "100%" }}>
              {turnEvents?.paths?.length ? (
                <details className={styles.pathDisclosure}>
                  <summary>
                    <span>{t("debug.turnEvents.paths", "Ledger files")}</span>
                    <small>
                      {t("debug.turnEvents.pathCount", {
                        count: turnEvents.paths.length,
                        defaultValue: "{{count}} 个文件",
                      })}
                    </small>
                  </summary>
                  <code>{turnEvents.paths.join("\n")}</code>
                </details>
              ) : null}

              {turnEventsError ? (
                <SettingsNotice
                  title={t("debug.turnEvents.loadFailed", "加载运行事件失败")}
                  tone="error"
                >
                  {turnEventsError}
                </SettingsNotice>
              ) : null}

              {!filteredTurnEventItems.length ? (
                <SettingsEmptyState
                  title={t(
                    "debug.turnEvents.empty",
                    "No turn events recorded yet.",
                  )}
                  description={t(
                    "debug.turnEvents.emptyHint",
                    "完成一次聊天或任务后，关键运行事件会显示在这里。",
                  )}
                />
              ) : (
                <div className={styles.turnEventList}>
                  {filteredTurnEventItems.map((item) => (
                    <div className={styles.turnEventItem} key={item.id}>
                      <div className={styles.turnEventTime}>
                        {dayjs(item.ts).format("MM/DD HH:mm:ss")}
                      </div>
                      <div className={styles.turnEventBody}>
                        <div className={styles.turnEventLabelRow}>
                          <strong>{item.label}</strong>
                          <Tag color={backendLevelColor("info")}>
                            {item.status}
                          </Tag>
                        </div>
                        <div className={styles.turnEventDetail}>
                          {item.detail}
                        </div>
                        <div className={styles.turnEventMeta}>
                          {item.chat_id || item.scope || "task"} ·{" "}
                          {item.workspace_dir}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Space>
          </div>
        </section>
      </div>
    </div>
  );
}
