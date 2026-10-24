import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiOutlined,
  CheckCircleOutlined,
  CopyOutlined,
  FileExcelOutlined,
  FilePptOutlined,
  FileTextOutlined,
  FileWordOutlined,
  ReloadOutlined,
  SettingOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import { Input, Skeleton, Switch, Tag } from "antd";
import { Button } from "@/vendor/lantaiDesign";
import { PageHeader } from "@/components/PageHeader";
import { officeDocumentApi } from "../../../api/modules/officeDocument";
import { mcpApi } from "../../../api/modules/mcp";
import type { OfficeCliStatus, OfficeCliMCPInfo } from "../../../api/types";
import { useAppMessage } from "../../../hooks/useAppMessage";
import { SettingsNotice } from "../components/SettingsNotice";
import styles from "./index.module.less";

const capabilityItems = [
  {
    title: "聊天分析附件",
    description: "把 Word、Excel、PPT 拖进聊天后，智能体可读取并继续修改。",
    icon: <FileTextOutlined />,
  },
  {
    title: "知识库 / 记忆入库",
    description: "导入 docx、xlsx、pptx 时提取正文和结构，沉淀为可检索资料。",
    icon: <FileWordOutlined />,
  },
  {
    title: "定时生成文档",
    description: "通过定时任务生成周报、销售表或汇报 PPT。",
    icon: <FileExcelOutlined />,
  },
  {
    title: "预览和校验",
    description: "工作区可用 OfficeCLI 生成文本或 HTML 预览，辅助对比修改。",
    icon: <FilePptOutlined />,
  },
];

function statusTone(status: OfficeCliStatus | null) {
  if (!status) return "default";
  if (!status.enabled) return "default";
  return status.available ? "success" : "warning";
}

function statusLabel(status: OfficeCliStatus | null) {
  if (!status) return "检测中";
  if (!status.enabled) return "已关闭";
  return status.available ? "已就绪" : "未检测到";
}

export default function OfficeDocumentsPage() {
  const { message } = useAppMessage();
  const [status, setStatus] = useState<OfficeCliStatus | null>(null);
  const [mcpInfo, setMcpInfo] = useState<OfficeCliMCPInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [mcpSaving, setMcpSaving] = useState(false);
  const [pathInput, setPathInput] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextStatus, nextMcp] = await Promise.all([
        officeDocumentApi.getOfficeCliStatus(),
        mcpApi.getOfficeCliMCP().catch(() => null),
      ]);
      setStatus(nextStatus);
      setMcpInfo(nextMcp);
      setPathInput(nextStatus.path || "");
    } catch (error) {
      console.error("Failed to load Office document capability", error);
      message.error("Office 文档能力状态加载失败");
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void load();
  }, [load]);

  const saveConfig = useCallback(
    async (patch: { enabled?: boolean; officecli_path?: string }) => {
      setSaving(true);
      try {
        const next = await officeDocumentApi.updateOfficeCliConfig(patch);
        setStatus(next);
        setPathInput(next.path || patch.officecli_path || "");
        message.success("Office 文档能力设置已更新");
        const nextMcp = await mcpApi.getOfficeCliMCP().catch(() => null);
        setMcpInfo(nextMcp);
      } catch (error) {
        console.error("Failed to update Office document config", error);
        message.error("保存失败");
      } finally {
        setSaving(false);
      }
    },
    [message],
  );

  const toggleMcp = useCallback(
    async (enabled: boolean) => {
      setMcpSaving(true);
      try {
        const next = await mcpApi.setOfficeCliMCP(enabled);
        setMcpInfo(next);
        message.success(
          enabled ? "已启用 OfficeCLI MCP" : "已关闭 OfficeCLI MCP",
        );
      } catch (error) {
        console.error("Failed to toggle OfficeCLI MCP", error);
        message.error(
          error instanceof Error ? error.message : "OfficeCLI MCP 设置失败",
        );
      } finally {
        setMcpSaving(false);
      }
    },
    [message],
  );

  const copyInstallCommand = useCallback(async () => {
    if (!status?.install_command) return;
    await navigator.clipboard?.writeText(status.install_command);
    message.success("已复制安装命令");
  }, [message, status?.install_command]);

  const statusTagColor = useMemo(() => {
    const tone = statusTone(status);
    if (tone === "success") return "green";
    if (tone === "warning") return "gold";
    return "default";
  }, [status]);

  return (
    <main className={styles.page}>
      <PageHeader
        parent="设置"
        current="Office 文档能力"
        description="让智能体读取、生成、预览和修改 Word、Excel、PowerPoint 文件。"
        extra={
          <Button
            size="small"
            icon={<ReloadOutlined spin={loading} />}
            onClick={() => void load()}
          >
            刷新
          </Button>
        }
      />

      <section className={styles.content}>
        {loading && !status ? (
          <div className={styles.loadingPanel}>
            <Skeleton active paragraph={{ rows: 6 }} />
          </div>
        ) : (
          <>
            <section className={styles.statusPanel}>
              <div className={styles.panelIntro}>
                <span className={styles.panelIcon}>
                  <ToolOutlined />
                </span>
                <div>
                  <div className={styles.titleRow}>
                    <h2>OfficeCLI 状态</h2>
                    <Tag color={statusTagColor}>{statusLabel(status)}</Tag>
                  </div>
                  <p>{status?.message || "正在检测本机 OfficeCLI。"}</p>
                </div>
              </div>

              <div className={styles.statusGrid}>
                <div>
                  <span>可执行文件</span>
                  <strong>{status?.path || "未找到"}</strong>
                </div>
                <div>
                  <span>来源</span>
                  <strong>{status?.source || "-"}</strong>
                </div>
                <div>
                  <span>版本</span>
                  <strong>{status?.version || "-"}</strong>
                </div>
                <div>
                  <span>MCP</span>
                  <strong>{status?.mcp_supported ? "支持" : "待安装"}</strong>
                </div>
              </div>

              {status?.available ? (
                <SettingsNotice tone="success" title="已检测到 OfficeCLI">
                  聊天附件、知识库导入、工作区预览和智能体 MCP 都可以使用同一套
                  Office 文档底座。
                </SettingsNotice>
              ) : (
                <SettingsNotice
                  tone="warning"
                  title="本机还没有可用的 officecli"
                >
                  Mac App Store/TestFlight 版不会在 App
                  内动态下载可执行文件。可以手动安装到 PATH，或把 officecli 随
                  App 打包后再刷新。
                </SettingsNotice>
              )}
            </section>

            <section className={styles.configPanel}>
              <div className={styles.sectionHeader}>
                <div>
                  <h2>连接方式</h2>
                  <p>
                    默认从 PATH 检测，也可以指定一个已安装的 officecli
                    可执行文件。
                  </p>
                </div>
                <Switch
                  checked={status?.enabled ?? true}
                  loading={saving}
                  onChange={(enabled) => void saveConfig({ enabled })}
                />
              </div>
              <div className={styles.pathRow}>
                <Input
                  value={pathInput}
                  onChange={(event) => setPathInput(event.target.value)}
                  placeholder="/opt/homebrew/bin/officecli"
                  allowClear
                />
                <Button
                  type="primary"
                  loading={saving}
                  icon={<SettingOutlined />}
                  onClick={() =>
                    void saveConfig({ officecli_path: pathInput.trim() })
                  }
                >
                  保存路径
                </Button>
              </div>
            </section>

            <section className={styles.mcpPanel}>
              <div className={styles.sectionHeader}>
                <div>
                  <h2>Agent 工具接入</h2>
                  <p>
                    启用后，当前智能体会通过 OfficeCLI MCP 直接调用文档操作。
                  </p>
                </div>
                <Switch
                  checked={Boolean(mcpInfo?.enabled)}
                  loading={mcpSaving}
                  disabled={!status?.available}
                  onChange={(enabled) => void toggleMcp(enabled)}
                />
              </div>
              <div className={styles.toolList}>
                {(mcpInfo?.tools || []).map((tool) => (
                  <div key={tool.name}>
                    <ApiOutlined />
                    <span>
                      <strong>{tool.name}</strong>
                      <em>{tool.description}</em>
                    </span>
                  </div>
                ))}
              </div>
            </section>

            <section className={styles.capabilityPanel}>
              <div className={styles.sectionHeader}>
                <div>
                  <h2>产品入口</h2>
                  <p>
                    这不是单独的技术页，而是会落到聊天、知识库、任务和工作区里。
                  </p>
                </div>
              </div>
              <div className={styles.capabilityGrid}>
                {capabilityItems.map((item) => (
                  <div key={item.title} className={styles.capabilityItem}>
                    <span>{item.icon}</span>
                    <div>
                      <div className={styles.capabilityTitle}>
                        <strong>{item.title}</strong>
                        <CheckCircleOutlined className={styles.checkIcon} />
                      </div>
                      <p>{item.description}</p>
                    </div>
                  </div>
                ))}
              </div>
            </section>

            {!status?.available ? (
              <section className={styles.installPanel}>
                <div>
                  <h2>安装引导</h2>
                  <p>
                    官网版可以直接用命令行安装；TestFlight
                    版建议用户手动安装，或后续把二进制作为随 App 资源打包。
                  </p>
                </div>
                <code>{status?.install_command}</code>
                <div className={styles.actions}>
                  <Button icon={<CopyOutlined />} onClick={copyInstallCommand}>
                    复制命令
                  </Button>
                  <Button
                    onClick={() =>
                      window.open(status?.docs_url, "_blank", "noopener")
                    }
                  >
                    打开 README
                  </Button>
                </div>
              </section>
            ) : null}
          </>
        )}
      </section>
    </main>
  );
}
