import { Form, Switch, Button, Card, Select } from "@/vendor/lantaiDesign";
import { PlusCircleOutlined } from "@ant-design/icons";
import { Input, Radio, Space } from "antd";
import { useTranslation } from "react-i18next";
import type { MergedRule } from "../useToolGuard";
import type {
  RememberedApproval,
  ToolGuardCommandRule,
  ToolGuardConfig,
} from "../../../../api/modules/security";
import type { FormInstance } from "antd";
import {
  CommandRulesEditor,
  RememberedApprovalsTable,
  RuleTable,
  ShellEvasionSection,
} from "./index";
import styles from "../index.module.less";

interface ToolGuardTabProps {
  form: FormInstance;
  config: ToolGuardConfig | null;
  enabled: boolean;
  setEnabled: (val: boolean) => void;
  toolOptions: { label: string; value: string }[];
  commandRules: ToolGuardCommandRule[];
  setCommandRules: (rules: ToolGuardCommandRule[]) => void;
  showFullAutoMode: boolean;
  mergedRules: MergedRule[];
  toggleRule: (ruleId: string, currentlyDisabled: boolean) => void;
  toggleAutoDeny: (ruleId: string, currentlyAutoDeny: boolean) => void;
  onPreviewRule: (rule: MergedRule) => void;
  onEditRule: (rule: MergedRule) => void;
  onDeleteRule: (ruleId: string) => void;
  openAddRule: () => void;
  rememberedApprovals: RememberedApproval[];
  rememberedApprovalsLoading?: boolean;
  onRefreshRememberedApprovals: () => void | Promise<void>;
  onDeleteRememberedApproval: (ruleId: string) => void | Promise<void>;
  onClearRememberedApprovals: () => void | Promise<void>;
  shellEvasionChecks: Record<string, boolean>;
  toggleShellEvasionCheck: (checkName: string, checked: boolean) => void;
  onOpenPermissions?: () => void;
}

const PERMISSION_MODE_OPTIONS = [
  {
    value: "default",
    label: "重要操作先问我",
    description:
      "查看资料可以直接进行；写文件或运行可能产生影响的命令前，会先征求你的同意。",
    technical: "技术边界：文件写入和 Bash 副作用命令需要确认",
  },
  {
    value: "plan",
    label: "只看和规划",
    description:
      "可以阅读资料、分析问题并制定计划，但不会运行命令，也不会修改文件。",
    technical: "技术边界：Plan 只读模式",
  },
  {
    value: "accept_edits",
    label: "允许修改工作区",
    description:
      "可以直接读取和修改所选工作区里的文件；可能影响系统或外部服务的命令仍会先询问。",
    technical: "自动范围：工作区内文件读写",
  },
  {
    value: "quiet",
    label: "安全操作自动完成（推荐）",
    description:
      "常见的读取、编辑、测试和构建会自动进行；检测到危险命令时直接拦截。",
    technical: "技术边界：低风险操作自动执行，高风险命令自动拒绝",
  },
  {
    value: "full_auto",
    label: "尽量自动执行",
    description:
      "除明确禁止的操作外，大多数步骤都会直接执行，不再逐次询问。仅适合你信任的工作环境。",
    technical: "技术边界：不可绕过的硬拒绝规则仍然生效",
  },
] as const;

function FieldLabel({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <span className={styles.fieldLabel}>
      <strong>{title}</strong>
      <span>{description}</span>
    </span>
  );
}

export function ToolGuardTab({
  form,
  config,
  enabled,
  setEnabled,
  toolOptions,
  commandRules,
  setCommandRules,
  showFullAutoMode,
  mergedRules,
  toggleRule,
  toggleAutoDeny,
  onPreviewRule,
  onEditRule,
  onDeleteRule,
  openAddRule,
  rememberedApprovals,
  rememberedApprovalsLoading,
  onRefreshRememberedApprovals,
  onDeleteRememberedApproval,
  onClearRememberedApprovals,
  shellEvasionChecks,
  toggleShellEvasionCheck,
  onOpenPermissions,
}: ToolGuardTabProps) {
  const { t } = useTranslation();
  const visibleModes = PERMISSION_MODE_OPTIONS.filter(
    (mode) => showFullAutoMode || mode.value !== "full_auto",
  );

  const chooseDefaultWorkspace = async () => {
    const folder = await window.electronAPI?.selectDirectory?.();
    if (folder) {
      form.setFieldsValue({ default_workspace_dir: folder });
    }
  };

  return (
    <div className={styles.tabContent}>
      {onOpenPermissions ? (
        <section className={styles.permissionSourcePanel}>
          <div>
            <strong>权限模式由统一权限页管理</strong>
            <p>
              当前模式：
              {visibleModes.find(
                (mode) => mode.value === config?.permission_mode,
              )?.label || "安全操作自动完成"}
              。这里继续管理检测规则、文件边界和扫描器。
            </p>
          </div>
          <Button onClick={onOpenPermissions}>打开权限设置</Button>
        </section>
      ) : (
        <>
          <div className={styles.sectionConfigureContainer}>
            <p className={styles.tabDescription}>
              附加风险扫描负责发现命令绕过、敏感路径和自定义风险；执行模式、命令权限、工作区边界与系统硬性限制是独立规则。聊天权限由每个对话单独保存。
            </p>

            <Card className={styles.formCard}>
              <Form
                form={form}
                layout="vertical"
                className={styles.form}
                initialValues={{
                  enabled: config?.enabled ?? true,
                  permission_mode: config?.permission_mode ?? "quiet",
                  default_workspace_dir: config?.default_workspace_dir ?? "",
                  guarded_tools: config?.guarded_tools ?? [],
                  denied_tools: config?.denied_tools ?? [],
                }}
              >
                <Form.Item
                  label={
                    <FieldLabel
                      title="附加风险扫描"
                      description="同时检查所有聊天和后台任务中的命令绕过、敏感路径与自定义风险。关闭后，执行模式、命令规则和系统硬性限制仍生效。"
                    />
                  }
                  name="enabled"
                  valuePropName="checked"
                  tooltip={t("security.enabledTooltip")}
                >
                  <Switch
                    aria-label="附加风险扫描"
                    onChange={(val) => setEnabled(val)}
                  />
                </Form.Item>
                <Form.Item
                  label={
                    <FieldLabel
                      title="备用工作文件夹"
                      description="调用没有明确工作区时才使用；不会改变已经绑定工作区的对话。"
                    />
                  }
                  name="default_workspace_dir"
                >
                  <Space.Compact style={{ width: "100%" }}>
                    <Input
                      aria-label="备用工作文件夹"
                      placeholder="未设置时使用当前智能体的工作区"
                    />
                    <Button onClick={chooseDefaultWorkspace}>选择</Button>
                  </Space.Compact>
                </Form.Item>
                <Form.Item
                  label={
                    <FieldLabel
                      title="后台与未指定场景怎样执行"
                      description="用于定时任务、外部渠道和没有单独选择权限的调用；聊天输入框里的选择由各对话单独保存。"
                    />
                  }
                  name="permission_mode"
                >
                  <Radio.Group className={styles.permissionModeGroup}>
                    {visibleModes.map((mode) => (
                      <Radio
                        key={mode.value}
                        value={mode.value}
                        className={styles.permissionModeOption}
                      >
                        <span className={styles.permissionModeText}>
                          <span className={styles.permissionModeLabel}>
                            {mode.label}
                          </span>
                          <span className={styles.permissionModeDescription}>
                            {mode.description}
                          </span>
                          <span className={styles.permissionModeTechnical}>
                            {mode.technical}
                          </span>
                        </span>
                      </Radio>
                    ))}
                  </Radio.Group>
                </Form.Item>
                <div className={styles.toolGuardRow}>
                  <Form.Item
                    label={
                      <FieldLabel
                        title="附加扫描重点检查的工具"
                        description="扫描开启时，这些工具发现风险会停下来征求你的同意。"
                      />
                    }
                    name="guarded_tools"
                    tooltip={t("security.guardedToolsTooltip")}
                    style={{ marginBottom: 0 }}
                  >
                    <Select
                      aria-label={t("security.guardedTools")}
                      mode="tags"
                      options={toolOptions}
                      placeholder={t("security.guardedToolsPlaceholder")}
                      disabled={!enabled}
                      allowClear
                      style={{ width: "100%" }}
                    />
                  </Form.Item>

                  <Form.Item
                    label={
                      <FieldLabel
                        title="附加扫描直接拒绝的工具"
                        description="扫描开启时，这些工具会直接拒绝，也不会弹窗请求放行。系统硬性限制不依赖此列表。"
                      />
                    }
                    name="denied_tools"
                    tooltip={t("security.deniedToolsTooltip")}
                    style={{ marginBottom: 0 }}
                  >
                    <Select
                      aria-label={t("security.deniedTools")}
                      mode="tags"
                      options={toolOptions}
                      placeholder={t("security.deniedToolsPlaceholder")}
                      disabled={!enabled}
                      allowClear
                      style={{ width: "100%" }}
                    />
                  </Form.Item>
                </div>
              </Form>
            </Card>
          </div>

          <div className={styles.sectionContainer}>
            <Card className={styles.tableCard}>
              <CommandRulesEditor
                enabled
                toolOptions={toolOptions}
                commandRules={commandRules}
                setCommandRules={setCommandRules}
              />
            </Card>
          </div>
        </>
      )}

      <div className={styles.sectionContainer}>
        <div className={styles.sectionHeader}>
          <h2 className={styles.sectionTitle}>{t("security.rules.title")}</h2>
          <Button
            type="primary"
            icon={<PlusCircleOutlined />}
            onClick={openAddRule}
            disabled={!enabled}
            size="middle"
          >
            {t("security.rules.add")}
          </Button>
        </div>

        <Card className={styles.tableCard}>
          <RuleTable
            rules={mergedRules}
            enabled={enabled}
            onToggleRule={toggleRule}
            onToggleAutoDeny={toggleAutoDeny}
            onPreviewRule={onPreviewRule}
            onEditRule={onEditRule}
            onDeleteRule={onDeleteRule}
          />
        </Card>
      </div>

      <div className={styles.sectionContainer}>
        <div className={styles.sectionHeader}>
          <h2 className={styles.sectionTitle}>
            {t("security.rememberedApprovals.title")}
          </h2>
        </div>

        <Card className={styles.tableCard}>
          <RememberedApprovalsTable
            approvals={rememberedApprovals}
            enabled
            loading={rememberedApprovalsLoading}
            onRefresh={onRefreshRememberedApprovals}
            onDelete={onDeleteRememberedApproval}
            onClearAll={onClearRememberedApprovals}
          />
        </Card>
      </div>

      <div className={styles.sectionContainer}>
        <div className={styles.sectionHeader}>
          <h2 className={styles.sectionTitle}>
            {t("security.shellEvasion.title")}
          </h2>
        </div>
        <div className={styles.sectionConfigureContainer}>
          <p className={styles.tabDescription}>
            {t("security.shellEvasion.description")}
          </p>
          <ShellEvasionSection
            checks={shellEvasionChecks}
            onToggle={toggleShellEvasionCheck}
            disabled={!enabled}
          />
        </div>
      </div>
    </div>
  );
}
