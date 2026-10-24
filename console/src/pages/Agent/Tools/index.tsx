import { useMemo, useState } from "react";
import {
  Button,
  Alert,
  Dropdown,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Select,
  Switch,
  Tooltip,
} from "@/vendor/lantaiDesign";
import type { MenuProps } from "@/vendor/lantaiDesign";
import {
  ApiOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  DownOutlined,
  LockOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import type { ToolInfo } from "../../../api/modules/tools";
import { CapabilityPage } from "../../../components/CapabilityLayout";
import { useAgentStore } from "../../../stores/agentStore";
import { getLocalizedToolDescription } from "@/utils/displayDescriptions";
import { useTools } from "./useTools";
import { describeToolState, primaryToolDescription } from "./presentation";
import styles from "./index.module.less";

type ToolStatusFilter = "all" | "enabled" | "disabled" | "needsConfig";

interface ToolGroup {
  id: string;
  name: string;
  description: string;
  order: number;
  tools: ToolInfo[];
}

const DEFAULT_EXPANDED_GROUPS = new Set([
  "shell",
  "files",
  "search",
  "browser",
]);

const EXTENSION_LINKS = [
  {
    title: "MCP 工具",
    path: "/mcp",
    icon: <ApiOutlined />,
  },
  {
    title: "技能",
    path: "/skills",
    icon: <ThunderboltOutlined />,
  },
  {
    title: "Office 文档",
    path: "/office-documents",
    icon: <ToolOutlined />,
  },
];

/** Stable background colours for the initial-letter fallback icon. */
const ICON_PALETTE = [
  "#f56a00",
  "#7265e6",
  "#ffbf00",
  "#00a2ae",
  "#87d068",
  "#1890ff",
  "#eb2f96",
  "#722ed1",
];

function hashStringToIndex(value: string, mod: number): number {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    hash = (hash * 31 + value.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % mod;
}

function ToolIcon({ icon, name }: { icon: string; name: string }) {
  if (icon) {
    return <span className={styles.toolEmoji}>{icon}</span>;
  }
  const letter = name.charAt(0).toUpperCase();
  const backgroundColor =
    ICON_PALETTE[hashStringToIndex(name, ICON_PALETTE.length)];
  return (
    <span className={styles.toolIconFallback} style={{ backgroundColor }}>
      {letter}
    </span>
  );
}

function getToolDisplayName(tool: ToolInfo) {
  return tool.display_name?.trim() || tool.name;
}

function buildToolGroups(tools: ToolInfo[]): ToolGroup[] {
  const groups = new Map<string, ToolGroup>();
  for (const tool of tools) {
    const id = tool.category || "extensions";
    const current = groups.get(id) || {
      id,
      name: tool.category_name || "扩展工具",
      description: tool.category_description || "由插件注册的附加能力",
      order: tool.category_order ?? 100,
      tools: [],
    };
    current.tools.push(tool);
    groups.set(id, current);
  }
  return Array.from(groups.values())
    .sort((left, right) => left.order - right.order)
    .map((group) => ({
      ...group,
      tools: [...group.tools].sort((left, right) =>
        getToolDisplayName(left).localeCompare(
          getToolDisplayName(right),
          "zh-CN",
        ),
      ),
    }));
}

function ToolConfigModal({
  tool,
  visible,
  onClose,
  onSave,
}: {
  tool: ToolInfo;
  visible: boolean;
  onClose: () => void;
  onSave: (values: Record<string, unknown>) => Promise<void>;
}) {
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);
  const { t } = useTranslation();

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);
      await onSave(values);
      onClose();
    } catch (error) {
      console.error("Failed to save config:", error);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={`${t("tools.configure")} · ${getToolDisplayName(tool)}`}
      open={visible}
      onCancel={onClose}
      onOk={handleSave}
      confirmLoading={saving}
      okText={t("common.save")}
      cancelText={t("common.cancel")}
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={tool.config_values || {}}
      >
        {tool.config_fields?.map((field) => {
          let input;
          switch (field.type) {
            case "password":
              input = (
                <Input.Password
                  placeholder={field.placeholder}
                  autoComplete="off"
                />
              );
              break;
            case "number":
              input = (
                <InputNumber
                  placeholder={field.placeholder}
                  min={field.min}
                  max={field.max}
                  style={{ width: "100%" }}
                />
              );
              break;
            case "boolean":
              input = <Switch />;
              break;
            case "select":
              input = (
                <Select placeholder={field.placeholder}>
                  {field.options?.map((option) => (
                    <Select.Option key={option} value={option}>
                      {option}
                    </Select.Option>
                  ))}
                </Select>
              );
              break;
            case "textarea":
              input = (
                <Input.TextArea
                  placeholder={field.placeholder}
                  rows={4}
                  autoSize={{ minRows: 2, maxRows: 8 }}
                />
              );
              break;
            case "text":
            default:
              input = <Input placeholder={field.placeholder} />;
          }

          return (
            <Form.Item
              key={field.name}
              name={field.name}
              label={field.label}
              rules={[
                {
                  required: field.required,
                  message: `${field.label} is required`,
                },
              ]}
              help={field.help}
              valuePropName={field.type === "boolean" ? "checked" : "value"}
            >
              {input}
            </Form.Item>
          );
        })}
      </Form>
    </Modal>
  );
}

export default function ToolsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { selectedAgent, agents } = useAgentStore();
  const {
    tools,
    loading,
    loadError,
    batchLoading,
    pendingToolNames,
    toggleEnabled,
    toggleAsyncExecution,
    setEnabledForTools,
    loadTools,
    saveToolConfig,
  } = useTools();
  const [configModalVisible, setConfigModalVisible] = useState(false);
  const [currentTool, setCurrentTool] = useState<ToolInfo | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<ToolStatusFilter>("all");
  const [expandedGroups, setExpandedGroups] = useState(
    () => new Set(DEFAULT_EXPANDED_GROUPS),
  );
  const currentAgent = agents.find((agent) => agent.id === selectedAgent);
  const currentAgentLabel =
    currentAgent?.name ||
    currentAgent?.description ||
    selectedAgent ||
    "默认智能体";

  const handleConfigure = (tool: ToolInfo) => {
    setCurrentTool(tool);
    setConfigModalVisible(true);
  };

  const handleSaveConfig = async (values: Record<string, unknown>) => {
    if (!currentTool) return;
    await saveToolConfig(currentTool.name, values);
    await loadTools();
  };

  const lockedCount = useMemo(
    () => tools.filter((tool) => tool.locked).length,
    [tools],
  );

  const filteredTools = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase();
    return tools.filter((tool) => {
      if (statusFilter === "enabled" && !tool.enabled) return false;
      if (statusFilter === "disabled" && tool.enabled) return false;
      if (
        statusFilter === "needsConfig" &&
        describeToolState(tool).key !== "needs_configuration"
      ) {
        return false;
      }
      if (!query) return true;
      const haystack = [
        tool.name,
        getToolDisplayName(tool),
        tool.description,
        tool.category_name,
        tool.source_label,
        tool.risk_label,
      ]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase();
      return haystack.includes(query);
    });
  }, [searchQuery, statusFilter, tools]);

  const groups = useMemo(() => buildToolGroups(filteredTools), [filteredTools]);
  const searchActive = searchQuery.trim().length > 0;
  const extensionMenuItems = useMemo<MenuProps["items"]>(
    () =>
      EXTENSION_LINKS.map((item) => ({
        key: item.path,
        icon: item.icon,
        label: item.title,
      })),
    [],
  );

  const toggleGroup = (groupId: string) => {
    setExpandedGroups((current) => {
      const next = new Set(current);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  };

  const handleBatchToggle = (group: ToolGroup, checked: boolean) => {
    const mutableTools = group.tools.filter((tool) => !tool.locked);
    if (!checked) {
      void setEnabledForTools(mutableTools, false);
      return;
    }
    Modal.confirm({
      title: `启用“${group.name}”中的 ${mutableTools.length} 个工具？`,
      content: (
        <div style={{ display: "grid", gap: 8 }}>
          <div>
            当前智能体会被允许尝试调用这些工具；实际执行仍会单独检查权限。
          </div>
          <div>
            范围：
            {mutableTools.map((tool) => getToolDisplayName(tool)).join("、")}
          </div>
        </div>
      ),
      okText: "确认启用",
      cancelText: "取消",
      onOk: () => setEnabledForTools(mutableTools, true),
    });
  };

  return (
    <CapabilityPage
      title="工具"
      description="查看当前智能体可以尝试调用的动作、缺少的配置和执行风险。"
      actions={
        <div className={styles.headerActions}>
          <Dropdown
            menu={{
              items: extensionMenuItems,
              onClick: ({ key }) => navigate(String(key)),
            }}
            trigger={["click"]}
            placement="bottomRight"
          >
            <Button icon={<ApiOutlined />}>
              添加扩展工具
              <DownOutlined className={styles.menuChevron} />
            </Button>
          </Dropdown>
          <Tooltip title="刷新工具列表">
            <Button
              aria-label="刷新工具列表"
              icon={<ReloadOutlined spin={loading} />}
              onClick={() => void loadTools()}
              disabled={loading}
            />
          </Tooltip>
        </div>
      }
      toolbar={
        <div className={styles.capabilityToolbar}>
          <div className={styles.capabilityFilters}>
            <Input
              className={styles.capabilitySearch}
              placeholder="搜索工具"
              value={searchQuery}
              allowClear
              onChange={(event) => setSearchQuery(event.target.value)}
            />
            <Select
              className={styles.statusSelect}
              value={statusFilter}
              onChange={setStatusFilter}
            >
              <Select.Option value="all">全部状态</Select.Option>
              <Select.Option value="enabled">仅已启用</Select.Option>
              <Select.Option value="disabled">仅已停用</Select.Option>
              <Select.Option value="needsConfig">待配置</Select.Option>
            </Select>
          </div>
          <span className={styles.toolbarSummary}>
            显示 {filteredTools.length} / {tools.length}
          </span>
        </div>
      }
    >
      <section className={styles.scopeNotice} aria-label="当前工具范围">
        <span>
          <strong>当前管理：{currentAgentLabel}</strong>
          启用和停用只影响这个智能体；工具真正执行时仍按当前对话权限检查。
        </span>
        <Button type="link" size="small" onClick={() => navigate("/agents")}>
          切换智能体
        </Button>
      </section>
      <section className={styles.policyNotice}>
        <SafetyCertificateOutlined className={styles.policyIcon} />
        <span>
          {lockedCount} 个核心工具由系统保持启用；这不代表权限已放行。
        </span>
        <Button
          type="link"
          size="small"
          onClick={() => navigate("/permissions")}
        >
          权限规则
        </Button>
      </section>

      {loadError ? (
        <Alert
          type="error"
          showIcon
          message={tools.length ? "工具列表未更新" : "工具列表加载失败"}
          description={
            tools.length
              ? `${loadError}。下面保留的是上一次成功读取的状态。`
              : `${loadError}。当前无法确认是否已经配置工具。`
          }
          action={
            <Button size="small" onClick={() => void loadTools()}>
              重新读取
            </Button>
          }
        />
      ) : null}

      {loading && tools.length === 0 ? (
        <div className={styles.loading}>{t("common.loading")}</div>
      ) : tools.length === 0 ? (
        <div className={styles.emptyState}>
          <Empty description={t("tools.emptyState")} />
        </div>
      ) : groups.length === 0 ? (
        <div className={styles.emptyState}>
          <Empty description="没有匹配的工具" />
        </div>
      ) : (
        <section className={styles.groupList} aria-label="系统工具分类">
          {groups.map((group) => {
            const expanded = searchActive || expandedGroups.has(group.id);
            const mutableTools = group.tools.filter((tool) => !tool.locked);
            const mutableEnabled = mutableTools.filter(
              (tool) => tool.enabled,
            ).length;
            const allMutableEnabled =
              mutableTools.length > 0 && mutableEnabled === mutableTools.length;
            const lockedTools = group.tools.length - mutableTools.length;
            const groupPending = group.tools.some((tool) =>
              pendingToolNames.has(tool.name),
            );

            return (
              <article className={styles.toolGroup} key={group.id}>
                <header className={styles.groupHeader}>
                  <button
                    type="button"
                    className={styles.groupIdentity}
                    onClick={() => toggleGroup(group.id)}
                    aria-expanded={expanded}
                    aria-controls={`tool-group-${group.id}`}
                  >
                    <DownOutlined
                      className={`${styles.groupChevron} ${
                        expanded ? styles.groupChevronExpanded : ""
                      }`}
                    />
                    <span>
                      <strong>{group.name}</strong>
                      <em>{group.description}</em>
                    </span>
                  </button>
                  <div className={styles.groupControls}>
                    <span>
                      {group.tools.length} 个
                      {lockedTools > 0 ? ` · ${lockedTools} 个核心` : ""}
                    </span>
                    {mutableTools.length === 0 ? (
                      <span className={styles.allLocked}>
                        <LockOutlined />
                        系统强制启用
                      </span>
                    ) : (
                      <span className={styles.groupToggleControl}>
                        <span>此智能体启用</span>
                        <Switch
                          size="small"
                          checked={allMutableEnabled}
                          loading={batchLoading || groupPending}
                          disabled={batchLoading || groupPending}
                          aria-label={`${allMutableEnabled ? "关闭" : "启用"}${group.name}可选工具`}
                          onChange={(checked) =>
                            handleBatchToggle(group, checked)
                          }
                        />
                      </span>
                    )}
                  </div>
                </header>

                {expanded ? (
                  <div
                    className={styles.toolRows}
                    id={`tool-group-${group.id}`}
                  >
                    {group.tools.map((tool) => {
                      const state = describeToolState(tool);
                      const localizedDescription = getLocalizedToolDescription(
                        tool,
                        t,
                      );
                      return (
                        <div
                          className={`${styles.toolRow} ${
                            !tool.enabled ? styles.toolRowMuted : ""
                          }`}
                          key={tool.name}
                        >
                          <span className={styles.toolIcon}>
                            <ToolIcon icon={tool.icon} name={tool.name} />
                          </span>
                          <div className={styles.toolCopy}>
                            <div className={styles.toolTitleRow}>
                              <strong>{getToolDisplayName(tool)}</strong>
                            </div>
                            <p>
                              {primaryToolDescription(
                                tool,
                                localizedDescription,
                              )}
                            </p>
                            <div className={styles.toolMeta}>
                              <span data-state={state.key} title={state.detail}>
                                {state.key === "enabled" ? (
                                  <CheckCircleOutlined />
                                ) : state.key === "needs_configuration" ? (
                                  <SettingOutlined />
                                ) : null}
                                {state.label}
                              </span>
                              <span>{tool.source_label || "澜台内置"}</span>
                              <span data-risk={tool.risk_hint}>
                                {tool.risk_label || "按调用判断风险"}
                              </span>
                            </div>
                            <details className={styles.technicalDetails}>
                              <summary>技术信息</summary>
                              <code>{tool.name}</code>
                              {tool.description &&
                              tool.description !== localizedDescription ? (
                                <span>{tool.description}</span>
                              ) : null}
                            </details>
                          </div>
                          <div className={styles.toolActions}>
                            {tool.name === "execute_shell_command" ? (
                              <span
                                className={styles.executionControl}
                                role="group"
                                aria-label={`${getToolDisplayName(tool)}执行方式`}
                                title="关闭时前台等待，开启后允许后台运行"
                              >
                                {tool.async_execution ? (
                                  <ThunderboltOutlined />
                                ) : (
                                  <ClockCircleOutlined />
                                )}
                                <span>
                                  {tool.async_execution
                                    ? "后台运行"
                                    : "前台等待"}
                                </span>
                                <Switch
                                  size="small"
                                  checked={Boolean(tool.async_execution)}
                                  loading={pendingToolNames.has(tool.name)}
                                  disabled={
                                    !tool.enabled ||
                                    pendingToolNames.has(tool.name)
                                  }
                                  aria-label={`${tool.async_execution ? "关闭" : "开启"}${getToolDisplayName(tool)}后台运行`}
                                  onChange={() =>
                                    void toggleAsyncExecution(tool)
                                  }
                                />
                              </span>
                            ) : null}
                            {tool.requires_config ? (
                              <Button
                                size="small"
                                type="text"
                                icon={<SettingOutlined />}
                                onClick={() => handleConfigure(tool)}
                              >
                                配置
                              </Button>
                            ) : null}
                            {tool.locked && tool.enabled ? (
                              <span
                                className={styles.lockedLabel}
                                title="核心工具保持可用，执行仍受权限策略约束"
                              >
                                <LockOutlined />
                                系统强制启用
                              </span>
                            ) : tool.locked ? (
                              <Button
                                size="small"
                                type="primary"
                                ghost
                                loading={pendingToolNames.has(tool.name)}
                                disabled={pendingToolNames.has(tool.name)}
                                onClick={() => void toggleEnabled(tool, true)}
                              >
                                恢复启用
                              </Button>
                            ) : (
                              <span className={styles.toolToggleControl}>
                                <span>此智能体</span>
                                <Switch
                                  size="small"
                                  checked={tool.enabled}
                                  loading={pendingToolNames.has(tool.name)}
                                  disabled={pendingToolNames.has(tool.name)}
                                  aria-label={`${tool.enabled ? "关闭" : "启用"}${getToolDisplayName(tool)}`}
                                  onChange={(checked) =>
                                    void toggleEnabled(tool, checked)
                                  }
                                />
                              </span>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : null}
              </article>
            );
          })}
        </section>
      )}

      {currentTool ? (
        <ToolConfigModal
          tool={currentTool}
          visible={configModalVisible}
          onClose={() => setConfigModalVisible(false)}
          onSave={handleSaveConfig}
        />
      ) : null}
    </CapabilityPage>
  );
}
