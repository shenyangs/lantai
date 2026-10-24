import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Form } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import api from "../../../api";
import {
  ChannelCard,
  ChannelDrawer,
  useChannels,
  getChannelLabel,
  type ChannelKey,
} from "./components";
import { PageHeader } from "@/components/PageHeader";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { useAgentStore } from "../../../stores/agentStore";
import { createAsyncStateScope } from "../../../utils/latestRequest";
import AppState from "../../../components/AppState";
import {
  confirmDiscardChanges,
  useBeforeUnload,
} from "../../../hooks/useUnsavedChanges";
import styles from "./index.module.less";

type FilterType = "all" | "builtin" | "custom";

function ChannelsPage() {
  const { t } = useTranslation();
  const { message, modal } = useAppMessage();
  const { selectedAgent } = useAgentStore();
  const {
    channels,
    descriptors,
    orderedKeys,
    isBuiltin,
    loading,
    error,
    fetchChannels,
  } = useChannels();
  const [filter, setFilter] = useState<FilterType>("all");
  const [saving, setSaving] = useState(false);
  const [activeKey, setActiveKey] = useState<ChannelKey | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [dirty, setDirty] = useState(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [form] = Form.useForm<any>();
  const mutationScopeRef = useRef(createAsyncStateScope());
  const previousAgentRef = useRef(selectedAgent);
  const dirtyRef = useRef(dirty);
  const activeKeyRef = useRef(activeKey);
  dirtyRef.current = dirty;
  activeKeyRef.current = activeKey;
  useBeforeUnload(drawerOpen && dirty);

  useEffect(() => {
    const previousAgent = previousAgentRef.current;
    if (
      previousAgent !== selectedAgent &&
      dirtyRef.current &&
      activeKeyRef.current
    ) {
      try {
        window.sessionStorage.setItem(
          `lantai:channel-draft:${previousAgent}:${activeKeyRef.current}`,
          JSON.stringify(form.getFieldsValue(true)),
        );
        message.warning(
          "已为上一个 Agent 暂存未保存的渠道草稿；返回后会自动恢复。",
        );
      } catch {
        message.warning(
          "Agent 已切换；渠道草稿无法暂存，请返回原 Agent 检查。 ",
        );
      }
    }
    setSaving(false);
    setDrawerOpen(false);
    setActiveKey(null);
    if (activeKeyRef.current) form.resetFields();
    setDirty(false);
    previousAgentRef.current = selectedAgent;
    return () => mutationScopeRef.current.invalidate();
  }, [form, message, selectedAgent]);

  // Sort cards: enabled first, then disabled (preserve orderedKeys order within each group)
  const cards = useMemo(() => {
    const enabledCards: { key: ChannelKey; config: Record<string, unknown> }[] =
      [];
    const disabledCards: {
      key: ChannelKey;
      config: Record<string, unknown>;
    }[] = [];

    orderedKeys.forEach((key) => {
      const config = channels[key] || { enabled: false, bot_prefix: "" };
      const builtin = isBuiltin(key);
      if (filter === "builtin" && !builtin) return;
      if (filter === "custom" && builtin) return;
      if (config.enabled) {
        enabledCards.push({ key, config });
      } else {
        disabledCards.push({ key, config });
      }
    });

    return [...enabledCards, ...disabledCards];
  }, [channels, orderedKeys, filter, isBuiltin]);

  const handleCardClick = (key: ChannelKey) => {
    setActiveKey(key);
    setDrawerOpen(true);
    const channelConfig = channels[key] || { enabled: false, bot_prefix: "" };
    form.resetFields();
    form.setFieldsValue({
      ...channelConfig,
      filter_tool_messages: !channelConfig.filter_tool_messages,
      filter_thinking: !channelConfig.filter_thinking,
    });
    try {
      const draftKey = `lantai:channel-draft:${selectedAgent}:${key}`;
      const rawDraft = window.sessionStorage.getItem(draftKey);
      if (rawDraft) {
        form.setFieldsValue(JSON.parse(rawDraft));
        window.sessionStorage.removeItem(draftKey);
        setDirty(true);
        message.info("已恢复此前未保存的渠道草稿。 ");
      } else {
        setDirty(false);
      }
    } catch {
      setDirty(false);
    }
  };

  const handleDrawerClose = () => {
    confirmDiscardChanges(modal, dirty, () => {
      setDrawerOpen(false);
      setActiveKey(null);
      setDirty(false);
      form.resetFields();
    });
  };

  const handleSubmit = async (values: Record<string, unknown>) => {
    if (!activeKey) return;
    const ownsState = mutationScopeRef.current.capture();

    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { isBuiltin: _isBuiltin, ...savedConfig } = channels[activeKey] || {};
    const updatedChannel: Record<string, unknown> = {
      ...savedConfig,
      ...values,
      filter_tool_messages: !values.filter_tool_messages,
      filter_thinking: !values.filter_thinking,
    };

    const opensAccess =
      (values.dm_policy === "open" &&
        channels[activeKey]?.dm_policy !== "open") ||
      (values.group_policy === "open" &&
        channels[activeKey]?.group_policy !== "open");
    if (opensAccess) {
      const confirmed = await new Promise<boolean>((resolve) => {
        modal.confirm({
          title: "确认开放渠道访问？",
          content:
            "公开策略会允许不在允许名单中的私聊或群聊触发 Agent。请确认机器人前缀、提及要求和渠道平台权限已经正确配置。",
          okText: "确认开放并保存",
          cancelText: "返回检查",
          okButtonProps: { danger: true },
          onOk: () => resolve(true),
          onCancel: () => resolve(false),
        });
      });
      if (!confirmed) return;
    }

    setSaving(true);
    try {
      await api.updateChannelConfig(
        activeKey,
        updatedChannel as unknown as Parameters<
          typeof api.updateChannelConfig
        >[1],
      );
      if (!ownsState()) return;
      await fetchChannels();
      if (!ownsState()) return;

      setDrawerOpen(false);
      setDirty(false);
      message.success(t("channels.configSaved"));
    } catch (error) {
      if (!ownsState()) return;
      console.error("❌ Failed to update channel config:", error);
      message.error(t("channels.configFailed"));
    } finally {
      if (ownsState()) setSaving(false);
    }
  };

  const activeLabel = activeKey ? getChannelLabel(activeKey, t) : "";

  const FILTER_TABS: { key: FilterType; label: string }[] = [
    { key: "all", label: t("channels.filterAll") },
    { key: "builtin", label: t("channels.builtin") },
    { key: "custom", label: t("channels.custom") },
  ];

  return (
    <div className={styles.channelsPage}>
      <PageHeader
        current={t("channels.title")}
        description={t("channels.description")}
        center={
          <div className={styles.filterTabs}>
            {FILTER_TABS.map(({ key, label }) => (
              <button
                key={key}
                type="button"
                aria-pressed={filter === key}
                className={`${styles.filterTab} ${
                  filter === key ? styles.filterTabActive : ""
                }`}
                onClick={() => setFilter(key)}
              >
                {label}
              </button>
            ))}
          </div>
        }
      />
      <div className={styles.channelsContainer}>
        {loading ? (
          <div className={styles.loading}>
            <span className={styles.loadingText}>{t("channels.loading")}</span>
          </div>
        ) : error ? (
          <AppState
            compact
            tone="error"
            title="无法加载渠道配置"
            description={error}
            actions={
              <Button onClick={() => void fetchChannels()}>重新加载</Button>
            }
          />
        ) : (
          <div className={styles.channelsGrid}>
            {cards.map(({ key, config }) => (
              <ChannelCard
                key={key}
                channelKey={key}
                config={config}
                onClick={() => handleCardClick(key)}
              />
            ))}
          </div>
        )}
      </div>
      <ChannelDrawer
        open={drawerOpen}
        activeKey={activeKey}
        activeLabel={activeLabel}
        form={form}
        saving={saving}
        initialValues={activeKey ? channels[activeKey] : undefined}
        isBuiltin={activeKey ? isBuiltin(activeKey) : true}
        descriptor={activeKey ? descriptors[activeKey] : undefined}
        onClose={handleDrawerClose}
        onSubmit={handleSubmit}
        onDirtyChange={setDirty}
      />
    </div>
  );
}

export default ChannelsPage;
