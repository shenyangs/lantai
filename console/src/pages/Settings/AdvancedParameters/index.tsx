import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  Button,
  Collapse,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
} from "@/vendor/lantaiDesign";
import {
  ApiOutlined,
  CompressOutlined,
  DatabaseOutlined,
  ReloadOutlined,
  SaveOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import api from "../../../api";
import type { AgentsRunningConfig } from "../../../api/types";
import { PageHeader } from "../../../components/PageHeader";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  confirmDiscardChanges,
  useBeforeUnload,
} from "../../../hooks/useUnsavedChanges";
import { SliderWithValue } from "../../Agent/Config/components/SliderWithValue";
import { SettingsNotice } from "../components/SettingsNotice";
import styles from "./index.module.less";

type UnknownRecord = Record<string, unknown>;

function isPlainObject(value: unknown): value is UnknownRecord {
  return (
    !!value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    Object.prototype.toString.call(value) === "[object Object]"
  );
}

function mergeConfig<T extends UnknownRecord>(
  base: T,
  patch: UnknownRecord,
): T {
  const next: UnknownRecord = { ...base };
  for (const [key, value] of Object.entries(patch)) {
    if (isPlainObject(value) && isPlainObject(next[key])) {
      next[key] = mergeConfig(next[key] as UnknownRecord, value);
    } else {
      next[key] = value;
    }
  }
  return next as T;
}

interface NumberFieldProps {
  label: string;
  name: string | (string | number)[];
  tooltip?: string;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  required?: boolean;
  placeholder?: string;
}

interface SwitchFieldProps {
  label: string;
  name: string | (string | number)[];
  description?: string;
}

interface SettingsSectionProps {
  title: string;
  description: string;
  icon: ReactNode;
  children: ReactNode;
}

function SettingsSection({
  title,
  description,
  icon,
  children,
}: SettingsSectionProps) {
  return (
    <section className={styles.sectionCard}>
      <header className={styles.sectionHeader}>
        <span className={styles.sectionIcon} aria-hidden="true">
          {icon}
        </span>
        <div>
          <h2>{title}</h2>
          <p>{description}</p>
        </div>
      </header>
      <div className={styles.sectionBody}>{children}</div>
    </section>
  );
}

function NumberField({
  label,
  name,
  tooltip,
  min,
  max,
  step = 1,
  unit,
  required = true,
  placeholder,
}: NumberFieldProps) {
  const rules = [
    ...(required ? [{ required: true, message: `${label}不能为空` }] : []),
    ...(typeof min === "number"
      ? [
          {
            type: "number" as const,
            min,
            message: `${label}不能小于 ${min}`,
          },
        ]
      : []),
    ...(typeof max === "number"
      ? [
          {
            type: "number" as const,
            max,
            message: `${label}不能大于 ${max}`,
          },
        ]
      : []),
  ];

  return (
    <Form.Item
      label={label}
      tooltip={tooltip}
      required={required}
      className={styles.fieldItem}
    >
      <Space.Compact block className={styles.numberControl}>
        <Form.Item name={name} noStyle rules={rules}>
          <InputNumber
            min={min}
            max={max}
            step={step}
            placeholder={placeholder}
            className={styles.numberInput}
          />
        </Form.Item>
        {unit ? <span className={styles.unitLabel}>{unit}</span> : null}
      </Space.Compact>
    </Form.Item>
  );
}

function SwitchField({ label, name, description }: SwitchFieldProps) {
  return (
    <div className={styles.switchRow}>
      <div>
        <strong>{label}</strong>
        {description ? <span>{description}</span> : null}
      </div>
      <Form.Item name={name} valuePropName="checked" noStyle>
        <Switch />
      </Form.Item>
    </div>
  );
}

export default function AdvancedParametersPage() {
  const [form] = Form.useForm();
  const { message, modal } = useAppMessage();
  const [config, setConfig] = useState<AgentsRunningConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  useBeforeUnload(dirty);

  const llmRetryEnabled = Form.useWatch("llm_retry_enabled", form) ?? true;

  const fetchConfig = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const nextConfig = await api.getAgentRunningConfig();
      setConfig(nextConfig);
      form.setFieldsValue(nextConfig);
      setDirty(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "高级参数加载失败");
    } finally {
      setLoading(false);
    }
  }, [form]);

  useEffect(() => {
    void fetchConfig();
  }, [fetchConfig]);

  const handleSave = useCallback(async () => {
    if (!config) return;
    try {
      const values = await form.validateFields();
      setSaving(true);
      const payload = mergeConfig(config as unknown as UnknownRecord, values);
      const saved = await api.updateAgentRunningConfig(
        payload as unknown as AgentsRunningConfig,
      );
      setConfig(saved);
      form.setFieldsValue(saved);
      setDirty(false);
      message.success("高级参数已保存");
    } catch (err) {
      if (err instanceof Error && "errorFields" in err) return;
      message.error(err instanceof Error ? err.message : "高级参数保存失败");
    } finally {
      setSaving(false);
    }
  }, [config, form, message]);

  if (loading) {
    return (
      <main className={styles.advancedPage}>
        <div className={styles.centerState}>正在读取高级参数...</div>
      </main>
    );
  }

  if (error) {
    return (
      <main className={styles.advancedPage}>
        <div className={styles.centerState}>
          <span className={styles.errorText}>{error}</span>
          <Button onClick={fetchConfig}>重试</Button>
        </div>
      </main>
    );
  }

  return (
    <main className={styles.advancedPage}>
      <PageHeader
        parent="设置"
        current="高级参数"
        description="调整 Agent 执行、上下文压缩、记忆注入和模型请求等系统级行为。"
      />

      <Form
        form={form}
        layout="vertical"
        className={styles.form}
        onValuesChange={() => setDirty(true)}
      >
        <SettingsSection
          title="Agent 循环"
          description="约束单轮执行的步数、工具重复调用和本地命令等待时间。"
          icon={<SyncOutlined />}
        >
          <div className={styles.fieldGrid}>
            <NumberField
              label="最大迭代次数"
              name="max_iters"
              min={1}
              unit="轮"
              tooltip="单次对话中 Agent 推理、调用工具、再观察结果的最大轮数。"
            />
            <NumberField
              label="单工具调用上限"
              name="tool_call_limit_per_tool"
              min={0}
              unit="次"
              tooltip="同一次回复中，同一个工具最多可被调用多少次。0 表示不限制。"
            />
            <NumberField
              label="死循环检测阈值"
              name="repeated_tool_call_limit"
              min={0}
              unit="次"
              tooltip="同一次回复里，使用完全相同参数反复调用同一工具超过该次数时中断。0 表示关闭。"
            />
            <NumberField
              label="Shell 命令超时"
              name="shell_command_timeout"
              min={1}
              step={10}
              unit="秒"
              tooltip="execute_shell_command 工具的默认超时时间，单次工具调用仍可覆盖。"
            />
          </div>

        </SettingsSection>

        <SettingsSection
          title="上下文管理"
          description="控制历史对话和工具结果进入模型前的压缩与裁剪策略。"
          icon={<CompressOutlined />}
        >
          <div className={styles.fieldGrid}>
            <NumberField
              label="最大上下文长度"
              name="max_input_length"
              min={1000}
              step={1024}
              unit="tokens"
              tooltip="请求的上下文窗口。实际窗口由 Provider、路由和本地 Runtime 的最小硬上限决定。"
            />
          </div>
          <SettingsNotice
            className={styles.sectionNotice}
            title="按模型有效窗口动态预算"
          >
            运行时会先为系统规则、输出和安全余量留出空间，再按当前模型能力压缩历史。旧比例字段仅用于兼容读取，不再控制生产阈值。
          </SettingsNotice>

          <Collapse
            className={styles.innerCollapse}
            items={[
              {
                key: "context",
                label: "上下文压缩",
                children: (
                  <div className={styles.disclosureBody}>
                    <SwitchField
                      label="启用上下文压缩"
                      name={[
                        "light_context_config",
                        "context_compact_config",
                        "enabled",
                      ]}
                      description="达到有效窗口预算时，自动整理较早的对话内容。"
                    />
                    <SwitchField
                      label="压缩时包含思考块"
                      name={[
                        "light_context_config",
                        "context_compact_config",
                        "compact_with_thinking_block",
                      ]}
                      description="把可见的推理过程一并纳入压缩摘要。"
                    />
                  </div>
                ),
              },
              {
                key: "tools",
                label: "工具结果裁剪",
                children: (
                  <div className={styles.disclosureBody}>
                    <SwitchField
                      label="启用工具结果裁剪"
                      name={[
                        "light_context_config",
                        "tool_result_pruning_config",
                        "enabled",
                      ]}
                      description="缩短过长的历史工具输出，避免占满上下文窗口。"
                    />
                    <div className={styles.fieldGrid}>
                      <NumberField
                        label="Snip 触发范围"
                        name={[
                          "light_context_config",
                          "tool_result_pruning_config",
                          "pruning_recent_n",
                        ]}
                        min={1}
                        max={10}
                        unit="条"
                        tooltip="最近 N 条消息使用较宽松的工具结果上限，更早消息使用更严格上限。"
                      />
                      <NumberField
                        label="旧工具结果上限"
                        name={[
                          "light_context_config",
                          "tool_result_pruning_config",
                          "pruning_old_msg_max_bytes",
                        ]}
                        min={100}
                        step={500}
                        unit="bytes"
                      />
                      <NumberField
                        label="最近工具结果上限"
                        name={[
                          "light_context_config",
                          "tool_result_pruning_config",
                          "pruning_recent_msg_max_bytes",
                        ]}
                        min={1000}
                        step={1000}
                        unit="bytes"
                      />
                      <NumberField
                        label="裁剪文件保留"
                        name={[
                          "light_context_config",
                          "tool_result_pruning_config",
                          "offload_retention_days",
                        ]}
                        min={1}
                        max={10}
                        unit="天"
                      />
                    </div>
                    <div className={styles.fieldGrid}>
                      <Form.Item
                        label="豁免文件后缀"
                        name={[
                          "light_context_config",
                          "tool_result_pruning_config",
                          "exempt_file_extensions",
                        ]}
                      >
                        <Select mode="tags" tokenSeparators={[",", " "]} />
                      </Form.Item>
                      <Form.Item
                        label="豁免工具名称"
                        name={[
                          "light_context_config",
                          "tool_result_pruning_config",
                          "exempt_tool_names",
                        ]}
                      >
                        <Select mode="tags" tokenSeparators={[",", " "]} />
                      </Form.Item>
                    </div>
                  </div>
                ),
              },
            ]}
          />
        </SettingsSection>

        <SettingsSection
          title="记忆注入"
          description="设置每轮写入、对话召回和分层记忆进入上下文的预算。"
          icon={<DatabaseOutlined />}
        >
          <SettingsNotice
            className={styles.sectionNotice}
            title="记忆维护已改为事件驱动"
          >
            旧的自动记忆间隔会继续读取以兼容历史配置，但不再按固定轮次触发摘要；L0
            捕获、Atom 提取与 System2 会根据新事件和背压运行。
          </SettingsNotice>
          <div className={styles.fieldGrid}>
            <NumberField
              label="单轮记忆写入上限"
              name={[
                "reme_light_memory_config",
                "layered_memory_config",
                "max_atoms_per_turn",
              ]}
              min={1}
              max={30}
              unit="条"
            />
            <NumberField
              label="记忆召回上限"
              name={[
                "reme_light_memory_config",
                "layered_memory_config",
                "max_recall_results",
              ]}
              min={1}
              max={20}
              unit="条"
            />
            <NumberField
              label="动态记忆字数回退上限"
              name={[
                "reme_light_memory_config",
                "layered_memory_config",
                "max_context_chars",
              ]}
              min={500}
              max={20000}
              step={500}
              unit="字符"
              tooltip="旧配置兼容字段；未设置动态记忆 Token 预算时才使用。"
            />
            <NumberField
              label="动态记忆 Token 预算"
              name={[
                "reme_light_memory_config",
                "layered_memory_config",
                "max_context_tokens",
              ]}
              min={128}
              max={131072}
              step={128}
              unit="tokens"
              required={false}
              placeholder="留空则使用字数回退上限"
              tooltip="优先使用的动态记忆召回预算；最终仍会由模型有效上下文和安全余量裁剪。"
            />
          </div>

          <Collapse
            className={styles.innerCollapse}
            items={[
              {
                key: "memory",
                label: "记忆维护与召回",
                children: (
                  <div className={styles.disclosureBody}>
                    <div className={styles.switchRows}>
                      <SwitchField
                        label="上下文压缩时总结记忆"
                        name={[
                          "reme_light_memory_config",
                          "summarize_when_compact",
                        ]}
                        description="压缩对话时同步提炼可复用的稳定信息。"
                      />
                      <SwitchField
                        label="启用分层记忆"
                        name={[
                          "reme_light_memory_config",
                          "layered_memory_config",
                          "enabled",
                        ]}
                        description="把即时、动态和稳定记忆按不同层级维护。"
                      />
                      <SwitchField
                        label="混合召回"
                        name={[
                          "reme_light_memory_config",
                          "layered_memory_config",
                          "hybrid_recall_enabled",
                        ]}
                        description="同时使用关键词与语义相关性召回记忆。"
                      />
                    </div>
                    <div className={styles.fieldGrid}>
                      <NumberField
                        label="稳定记忆字数回退上限"
                        name={[
                          "reme_light_memory_config",
                          "layered_memory_config",
                          "stable_context_chars",
                        ]}
                        min={500}
                        max={20000}
                        step={500}
                        unit="字符"
                        tooltip="旧配置兼容字段；未设置稳定记忆 Token 预算时才使用。"
                      />
                      <NumberField
                        label="稳定记忆 Token 预算"
                        name={[
                          "reme_light_memory_config",
                          "layered_memory_config",
                          "stable_context_tokens",
                        ]}
                        min={128}
                        max={131072}
                        step={128}
                        unit="tokens"
                        required={false}
                        placeholder="留空则使用字数回退上限"
                        tooltip="优先使用的已确认稳定记忆预算；最终仍受模型有效窗口约束。"
                      />
                      <NumberField
                        label="记忆抽取超时"
                        name={[
                          "reme_light_memory_config",
                          "layered_memory_config",
                          "extraction_timeout_seconds",
                        ]}
                        min={1}
                        max={120}
                        unit="秒"
                      />
                    </div>
                    <div className={styles.fieldGrid}>
                      <Form.Item
                        label="梦境整理计划"
                        name={["reme_light_memory_config", "dream_cron"]}
                        tooltip="Cron 表达式，留空表示关闭定期记忆整理。"
                      >
                        <Input placeholder="0 23 * * *" />
                      </Form.Item>
                      <NumberField
                        label="自动搜索最大结果"
                        name={[
                          "reme_light_memory_config",
                          "auto_memory_search_config",
                          "max_results",
                        ]}
                        min={1}
                        unit="条"
                      />
                    </div>
                    <Form.Item
                      label="自动搜索最低相关性"
                      name={[
                        "reme_light_memory_config",
                        "auto_memory_search_config",
                        "min_score",
                      ]}
                    >
                      <SliderWithValue
                        min={0}
                        max={1}
                        step={0.05}
                        marks={{ 0: "0", 0.5: "0.5", 1: "1" }}
                      />
                    </Form.Item>
                    <SwitchField
                      label="启用自动记忆搜索"
                      name={[
                        "reme_light_memory_config",
                        "auto_memory_search_config",
                        "enabled",
                      ]}
                      description="对话需要补充背景时自动检索相关记忆。"
                    />
                  </div>
                ),
              },
            ]}
          />
        </SettingsSection>

        <SettingsSection
          title="LLM 请求"
          description="调整瞬时故障重试、并发上限和请求节流。"
          icon={<ApiOutlined />}
        >
          <div className={styles.switchRows}>
            <SwitchField
              label="启用自动重试"
              name="llm_retry_enabled"
              description="遇到限流、超时或连接中断时自动再次请求。"
            />
          </div>
          <div className={styles.fieldGrid}>
            <NumberField
              label="最大重试次数"
              name="llm_max_retries"
              min={1}
              unit="次"
              required={llmRetryEnabled}
            />
            <NumberField
              label="退避基础延迟"
              name="llm_backoff_base"
              min={0.1}
              step={0.1}
              unit="秒"
              required={llmRetryEnabled}
            />
            <NumberField
              label="退避最大延迟"
              name="llm_backoff_cap"
              min={0.5}
              step={0.5}
              unit="秒"
              required={llmRetryEnabled}
            />
            <NumberField
              label="槽位获取超时"
              name="llm_acquire_timeout"
              min={10}
              step={10}
              unit="秒"
            />
          </div>
          <Collapse
            className={styles.innerCollapse}
            items={[
              {
                key: "rate",
                label: "并发与限流",
                children: (
                  <div className={styles.fieldGrid}>
                    <NumberField
                      label="最大并发请求"
                      name="llm_max_concurrent"
                      min={1}
                      unit="路"
                    />
                    <NumberField
                      label="每分钟请求上限"
                      name="llm_max_qpm"
                      min={0}
                      step={10}
                      unit="QPM"
                      tooltip="0 表示不限流。"
                    />
                    <NumberField
                      label="限流暂停"
                      name="llm_rate_limit_pause"
                      min={1}
                      step={0.5}
                      unit="秒"
                    />
                    <NumberField
                      label="暂停抖动"
                      name="llm_rate_limit_jitter"
                      min={0}
                      step={0.5}
                      unit="秒"
                    />
                  </div>
                ),
              },
            ]}
          />
        </SettingsSection>

        <div className={styles.stickyActions}>
          <span>高级设置会影响后续对话和任务运行。</span>
          <div className={styles.headerActions}>
            <Button
              icon={<ReloadOutlined />}
              onClick={() =>
                confirmDiscardChanges(modal, dirty, () => void fetchConfig(), {
                  title: "重新载入并舍弃高级参数修改？",
                  content:
                    "页面会恢复为当前已保存的系统参数，尚未保存的更改将丢失。",
                })
              }
              disabled={saving}
            >
              重置
            </Button>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={saving}
              onClick={handleSave}
            >
              保存更改
            </Button>
          </div>
        </div>
      </Form>
    </main>
  );
}
