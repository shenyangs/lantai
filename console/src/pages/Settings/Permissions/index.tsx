import { Button, Form, Switch } from "@/vendor/lantaiDesign";
import { Collapse, Input, Radio, Space, Tag } from "antd";
import {
  ClipboardCheck,
  FolderOpen,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Zap,
} from "lucide-react";
import { useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { PageHeader } from "@/components/PageHeader";
import {
  CommandRulesEditor,
  RememberedApprovalsTable,
} from "../Security/components";
import { useSecurityPage } from "../Security/useSecurityPage";
import type { ToolPermissionMode } from "../../../api/modules/security";
import styles from "./index.module.less";

interface PermissionModeCard {
  value: ToolPermissionMode;
  title: string;
  description: string;
  tags: string[];
  recommended?: boolean;
  icon: ReactNode;
}

const MODE_CARDS: PermissionModeCard[] = [
  {
    value: "quiet",
    title: "安全操作自动完成",
    description:
      "常见的读取、编辑、测试和构建会自动进行；检测到危险命令时直接拦截。",
    tags: ["推荐", "少确认", "可审计"],
    recommended: true,
    icon: <ShieldCheck size={18} />,
  },
  {
    value: "default",
    title: "重要操作先问我",
    description:
      "查看资料可以直接进行；写文件或运行可能产生影响的命令前，会先征求你的同意。",
    tags: ["稳妥", "可控", "适合敏感项目"],
    icon: <ShieldAlert size={18} />,
  },
  {
    value: "accept_edits",
    title: "允许修改工作区",
    description:
      "工作区内文件读写自动放行，命令行副作用仍需要确认，适合代码改动密集任务。",
    tags: ["写文件少打扰", "命令仍确认"],
    icon: <ClipboardCheck size={18} />,
  },
  {
    value: "plan",
    title: "只看和规划",
    description:
      "只允许分析、读取和规划，不执行会修改文件、联网、改环境或运行命令的工具。",
    tags: ["只读", "方案审查"],
    icon: <SlidersHorizontal size={18} />,
  },
  {
    value: "full_auto",
    title: "尽量自动执行",
    description:
      "大多数操作自动放行，仍保留不可绕过的硬拒绝规则；仅适合受信任环境。",
    tags: ["官网版", "高效率", "硬拒绝仍生效"],
    icon: <Zap size={18} />,
  },
];

function modeTitle(mode?: string) {
  return (
    MODE_CARDS.find((item) => item.value === mode)?.title ?? "安全操作自动完成"
  );
}

export default function PermissionsPage() {
  const navigate = useNavigate();
  const {
    form,
    config,
    enabled,
    setEnabled,
    toolOptions,
    saving,
    handleSave,
    handleReset,
    commandRules,
    setCommandRules: setCommandRulesInternal,
    showFullAutoMode,
    rememberedApprovals,
    rememberedApprovalsLoading,
    handleRefreshRememberedApprovals,
    handleDeleteRememberedApproval,
    handleClearRememberedApprovals,
    loading,
    error,
    fetchAll,
  } = useSecurityPage();
  const [dirty, setDirty] = useState(false);
  const setCommandRules = (
    next: Parameters<typeof setCommandRulesInternal>[0],
  ) => {
    setDirty(true);
    setCommandRulesInternal(next);
  };

  const savePermissions = async () => {
    if (await handleSave()) setDirty(false);
  };

  const resetPermissions = () => {
    handleReset();
    setDirty(false);
  };

  const visibleModes = useMemo(
    () =>
      MODE_CARDS.filter(
        (mode) => showFullAutoMode || mode.value !== "full_auto",
      ),
    [showFullAutoMode],
  );

  const chooseDefaultWorkspace = async () => {
    const folder = await window.electronAPI?.selectDirectory?.();
    if (folder) {
      form.setFieldsValue({ default_workspace_dir: folder });
      setDirty(true);
    }
  };

  if (loading) {
    return (
      <div className={styles.permissionsPage}>
        <div className={styles.centerState}>正在读取权限设置...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className={styles.permissionsPage}>
        <div className={styles.centerState}>
          <Space direction="vertical" align="center">
            <span className={styles.stateTextError}>{error}</span>
            <Button size="small" onClick={fetchAll}>
              重试
            </Button>
          </Space>
        </div>
      </div>
    );
  }

  const currentMode = config?.permission_mode ?? "quiet";

  return (
    <div className={styles.permissionsPage}>
      <PageHeader
        parent="设置"
        current="权限"
        description="设置后台任务和未单独选择权限的场景；附加风险扫描与细则同时保护所有聊天和后台任务，系统硬性限制始终有效。"
      />

      <div className={styles.content}>
        <div className={styles.inner}>
          <div className={styles.introBand}>
            <section className={styles.introCopy}>
              <h2>后台与未指定场景的默认权限</h2>
              <p>
                定时任务、渠道消息或其他没有单独选择权限的调用会使用这里的设置。聊天输入框里的权限由每个对话单独保存，不会改动这里。
              </p>
            </section>

            <aside className={styles.statusPanel}>
              <span className={styles.statusLabel}>作用范围</span>
              <span className={styles.statusValue}>本机 · 所有智能体</span>
              <span className={styles.statusLabel}>后台与未指定场景</span>
              <span className={styles.statusValue}>
                {modeTitle(currentMode)}
              </span>
              <span className={styles.statusMeta}>
                聊天权限由每个对话单独保存
              </span>
              <span className={styles.statusMeta}>
                {enabled ? "附加风险扫描已开启" : "附加风险扫描已关闭"}
              </span>
              <span className={styles.statusMeta}>
                保存的改动影响所有智能体的新操作
              </span>
            </aside>
          </div>

          <section className={styles.scopeFlow} aria-label="权限范围关系">
            <div>
              <strong>系统硬性限制</strong>
              <span>同时约束下面两类场景，任何模式都不能绕过</span>
            </div>
            <div>
              <strong>后台与未指定场景</strong>
              <span>{modeTitle(currentMode)}</span>
            </div>
            <div>
              <strong>每个聊天对话</strong>
              <span>在聊天输入框单独选择，不覆盖左侧默认</span>
            </div>
          </section>

          <section className={styles.sectionPanel}>
            <div className={styles.sectionHeader}>
              <h2>智能体能自动做什么</h2>
              <p>先选一种自动执行范围，再保存。明确禁止的操作始终不会执行。</p>
            </div>

            <Form
              form={form}
              layout="vertical"
              initialValues={{
                enabled: config?.enabled ?? true,
                permission_mode: currentMode,
                default_workspace_dir: config?.default_workspace_dir ?? "",
                guarded_tools: config?.guarded_tools ?? [],
                denied_tools: config?.denied_tools ?? [],
              }}
              onValuesChange={() => setDirty(true)}
            >
              <Form.Item
                name="enabled"
                valuePropName="checked"
                label="附加风险扫描"
                extra="这项扫描同时用于所有聊天和后台任务，补充检查命令绕过、敏感路径和自定义拒绝规则。关闭后仍保留系统硬性限制和当前执行模式。"
              >
                <Switch onChange={(val) => setEnabled(val)} />
              </Form.Item>

              <Form.Item name="permission_mode">
                <Radio.Group className={styles.modeGrid}>
                  {visibleModes.map((mode) => (
                    <Radio
                      key={mode.value}
                      value={mode.value}
                      className={`${styles.modeOption} ${
                        mode.recommended ? styles.modeOptionRecommended : ""
                      }`}
                    >
                      <span className={styles.modeText}>
                        <span className={styles.modeTitle}>
                          <span className={styles.modeIcon}>{mode.icon}</span>
                          {mode.title}
                        </span>
                        <span className={styles.modeDescription}>
                          {mode.description}
                        </span>
                        <span className={styles.modeMeta}>
                          {mode.tags.map((tag) => (
                            <Tag key={tag}>{tag}</Tag>
                          ))}
                        </span>
                      </span>
                    </Radio>
                  ))}
                </Radio.Group>
              </Form.Item>

              <Form.Item
                label="备用工作文件夹"
                name="default_workspace_dir"
                extra="只在任务没有明确工作区时使用，不会改变已经绑定工作区的对话。"
              >
                <div className={styles.workspaceRow}>
                  <Input
                    aria-label="备用工作文件夹"
                    placeholder="未设置时使用当前智能体的工作区"
                  />
                  <Button
                    icon={<FolderOpen size={15} />}
                    iconSize={15}
                    onClick={chooseDefaultWorkspace}
                  >
                    选择
                  </Button>
                </div>
              </Form.Item>
            </Form>

            <ul className={styles.hintList}>
              <li>安全操作自动完成：适合日常开发、测试、构建和安全读写。</li>
              <li>重要操作先问我：适合生产仓库、客户项目和高敏感代码。</li>
              <li>只看和规划：适合让智能体先出方案，再由你决定是否执行。</li>
            </ul>
          </section>

          <section className={`${styles.sectionPanel} ${styles.advancedPanel}`}>
            <Collapse
              items={[
                {
                  key: "advanced",
                  label: "高级控制",
                  children: (
                    <>
                      <div className={styles.advancedBlock}>
                        <CommandRulesEditor
                          enabled
                          toolOptions={toolOptions}
                          commandRules={commandRules}
                          setCommandRules={setCommandRules}
                        />
                      </div>

                      <div className={styles.advancedBlock}>
                        <div className={styles.advancedBlockHeader}>
                          <h3>自动授权记录</h3>
                        </div>
                        <RememberedApprovalsTable
                          approvals={rememberedApprovals}
                          enabled
                          loading={rememberedApprovalsLoading}
                          onRefresh={handleRefreshRememberedApprovals}
                          onDelete={handleDeleteRememberedApproval}
                          onClearAll={handleClearRememberedApprovals}
                        />
                      </div>

                      <div className={styles.advancedBlock}>
                        <Button onClick={() => navigate("/security")}>
                          打开完整安全策略
                        </Button>
                      </div>
                    </>
                  ),
                },
              ]}
            />
          </section>
        </div>
      </div>

      <div className={styles.footerButtons}>
        <span
          className={styles.editState}
          data-dirty={dirty ? "true" : "false"}
        >
          {dirty ? "有尚未保存的权限修改" : "当前设置已保存"}
        </span>
        <Button onClick={resetPermissions} disabled={saving || !dirty}>
          取消修改
        </Button>
        <Button
          type="primary"
          onClick={() => void savePermissions()}
          loading={saving}
          disabled={!dirty}
        >
          保存权限设置
        </Button>
      </div>
    </div>
  );
}
