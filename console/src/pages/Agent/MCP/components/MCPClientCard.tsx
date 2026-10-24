import {
  Card,
  Button,
  Modal,
  Tooltip,
  Input,
  Empty,
  Tag,
} from "@/vendor/lantaiDesign";
import { Spin } from "antd";
import type { MCPClientInfo, MCPToolInfo } from "../../../../api/types";
import type {
  MCPConnectorLifecycleInfo,
  MCPOAuthConfig,
} from "../../../../api/types";
import { useTranslation } from "react-i18next";
import React, { useState, useCallback, useEffect, useRef } from "react";
import { useTheme } from "../../../../contexts/ThemeContext";
import {
  EyeOutlined,
  EyeInvisibleOutlined,
  ToolOutlined,
  LinkOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import api from "../../../../api";
import { useAgentStore } from "../../../../stores/agentStore";
import { createLatestRequestGate } from "../../../../utils/latestRequest";
import { prepareExternalNavigation } from "../../../../utils/openExternalUrl";
import styles from "../index.module.less";

interface MCPClientUpdate {
  name?: string;
  description?: string;
  command?: string;
  enabled?: boolean;
  transport?: "stdio" | "streamable_http" | "sse";
  url?: string;
  headers?: Record<string, string>;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  oauth?: Partial<MCPOAuthConfig>;
  allowed_tools?: string[];
}

const lifecycleLabels: Record<MCPConnectorLifecycleInfo["status"], string> = {
  disabled: "已停用",
  configuration_required: "待配置",
  authorization_required: "待授权",
  connecting: "连接中",
  ready: "可用",
  error: "异常",
};

interface MCPClientCardProps {
  client: MCPClientInfo;
  pending?: boolean;
  onToggle: (client: MCPClientInfo, e: React.MouseEvent) => void;
  onDelete: (client: MCPClientInfo, e: React.MouseEvent) => void;
  onUpdate: (key: string, updates: MCPClientUpdate) => Promise<boolean>;
}

export const MCPClientCard = React.memo(function MCPClientCard({
  client,
  pending = false,
  onToggle,
  onDelete,
  onUpdate,
}: MCPClientCardProps) {
  const { t } = useTranslation();
  const { isDark } = useTheme();
  const { selectedAgent } = useAgentStore();
  const [isHovered, setIsHovered] = useState(false);
  const [jsonModalOpen, setJsonModalOpen] = useState(false);
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [toolsModalOpen, setToolsModalOpen] = useState(false);
  const [tools, setTools] = useState<MCPToolInfo[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const [lifecycle, setLifecycle] = useState<MCPConnectorLifecycleInfo | null>(
    null,
  );
  const [lifecycleLoading, setLifecycleLoading] = useState(false);
  const [lifecycleError, setLifecycleError] = useState<string | null>(null);
  const [editedJson, setEditedJson] = useState("");
  const [isEditing, setIsEditing] = useState(false);
  const toolsLoadGateRef = useRef(createLatestRequestGate());
  const lifecycleGateRef = useRef(createLatestRequestGate());

  useEffect(() => {
    toolsLoadGateRef.current.invalidate();
    setJsonModalOpen(false);
    setDeleteModalOpen(false);
    setToolsModalOpen(false);
    setTools([]);
    setToolsError(null);
    setToolsLoading(false);
    setLifecycle(null);
    setLifecycleError(null);
    setLifecycleLoading(false);
    setIsEditing(false);
    return () => {
      toolsLoadGateRef.current.invalidate();
      lifecycleGateRef.current.invalidate();
    };
  }, [selectedAgent]);

  const loadLifecycle = useCallback(async () => {
    const ownsState = lifecycleGateRef.current.begin();
    try {
      const data = await api.getMCPLifecycle(client.key);
      if (ownsState()) {
        setLifecycle(data);
        setLifecycleError(data.error_message);
      }
      return data;
    } catch (error: any) {
      if (ownsState()) {
        setLifecycleError(error?.message || "无法读取连接状态");
      }
      return null;
    }
  }, [client.key]);

  useEffect(() => {
    void loadLifecycle();
    return () => lifecycleGateRef.current.invalidate();
  }, [client.enabled, client.oauth?.expires_at, loadLifecycle]);

  // Determine if MCP client is remote or local based on command
  const isRemote =
    client.transport === "streamable_http" || client.transport === "sse";
  const clientType = isRemote ? "Remote" : "Local";

  const handleToggleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onToggle(client, e);
  };

  const handleDeleteClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    setDeleteModalOpen(true);
  };

  const confirmDelete = () => {
    setDeleteModalOpen(false);
    onDelete(client, null as unknown as React.MouseEvent);
  };

  const handleCardClick = () => {
    const jsonStr = JSON.stringify(client, null, 2);
    setEditedJson(jsonStr);
    setIsEditing(false);
    setJsonModalOpen(true);
  };

  const handleSaveJson = async () => {
    try {
      const parsed = JSON.parse(editedJson);
      const { key: _key, revision: _revision, ...updates } = parsed;

      // Send all updates directly to backend, let backend handle env masking check
      const success = await onUpdate(client.key, updates);
      if (success) {
        setJsonModalOpen(false);
        setIsEditing(false);
      }
    } catch {
      alert("Invalid JSON format");
    }
  };

  const handleShowTools = useCallback(
    async (e: React.MouseEvent) => {
      const ownsState = toolsLoadGateRef.current.begin();
      e.stopPropagation();
      setToolsModalOpen(true);
      setToolsLoading(true);
      setToolsError(null);
      setTools([]);
      try {
        const data = await api.listMCPTools(client.key);
        if (ownsState()) setTools(data);
      } catch (err: any) {
        if (!ownsState()) return;
        const msg = err?.message || "";
        if (msg.includes("connecting") || msg.includes("not ready")) {
          setToolsError(t("mcp.toolsConnecting"));
        } else {
          setToolsError(msg || t("mcp.toolsLoadError"));
        }
      } finally {
        if (ownsState()) setToolsLoading(false);
      }
    },
    [client.key, t],
  );

  const handleProbe = useCallback(
    async (e: React.MouseEvent) => {
      e.stopPropagation();
      const ownsState = lifecycleGateRef.current.begin();
      setLifecycleLoading(true);
      setLifecycleError(null);
      try {
        const data = await api.probeMCPConnector(client.key);
        if (!ownsState()) return;
        setLifecycle(data);
        setLifecycleError(data.error_message);
      } catch (error: any) {
        if (ownsState()) {
          setLifecycleError(error?.message || "连接检测失败");
        }
      } finally {
        if (ownsState()) setLifecycleLoading(false);
      }
    },
    [client.key],
  );

  const handleAuthorize = useCallback(
    async (e: React.MouseEvent) => {
      e.stopPropagation();
      const ownsState = lifecycleGateRef.current.begin();
      setLifecycleLoading(true);
      setLifecycleError(null);
      const navigation = prepareExternalNavigation();
      try {
        if (!navigation) {
          throw new Error("浏览器阻止了授权窗口，请允许后重试");
        }
        const started = await api.startMCPOAuth(client.key);
        await navigation.open(started.authorization_url);

        for (let attempt = 0; attempt < 30; attempt += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 2_000));
          if (!ownsState()) return;
          const status = await api.getMCPLifecycle(client.key);
          if (!ownsState()) return;
          setLifecycle(status);
          setLifecycleError(status.error_message);
          if (status.auth_status === "ready") {
            const probed = await api.probeMCPConnector(client.key);
            if (ownsState()) {
              setLifecycle(probed);
              setLifecycleError(probed.error_message);
            }
            return;
          }
        }
        if (ownsState()) {
          setLifecycleError("等待授权超时，请完成授权后点击检测。");
        }
      } catch (error: any) {
        navigation?.close();
        if (ownsState()) {
          setLifecycleError(error?.message || "无法开始授权");
        }
      } finally {
        if (ownsState()) setLifecycleLoading(false);
      }
    },
    [client.key],
  );

  const clientJson = JSON.stringify(client, null, 2);

  return (
    <>
      <Card
        hoverable
        onClick={handleCardClick}
        onMouseEnter={() => setIsHovered(true)}
        onMouseLeave={() => setIsHovered(false)}
        className={`${styles.mcpCard} ${
          client.enabled ? styles.enabledCard : ""
        } ${isHovered ? styles.hover : styles.normal}`}
      >
        <div className={styles.cardHeader}>
          <Tooltip title={client.name}>
            <h3 className={styles.mcpTitle}>
              <span>{client.name}</span>
              <span
                className={`${styles.typeBadge} ${
                  isRemote ? styles.remote : styles.local
                }`}
              >
                {clientType}
              </span>
            </h3>
          </Tooltip>
          <Tag
            color={
              lifecycle?.status === "ready"
                ? "green"
                : lifecycle?.status === "error"
                  ? "red"
                  : lifecycle?.status === "authorization_required"
                    ? "gold"
                    : "default"
            }
          >
            {lifecycle
              ? lifecycleLabels[lifecycle.status]
              : client.enabled
                ? "读取状态"
                : "已停用"}
          </Tag>
        </div>

        <p className={styles.mcpDescription}>{client.description || "-"}</p>
        {lifecycleError ? (
          <p className={styles.toolsError}>{lifecycleError}</p>
        ) : lifecycle?.status === "ready" ? (
          <p className={styles.mcpDescription}>
            已发现 {lifecycle.tool_count} 个可用工具
            {lifecycle.allowed_tools.length
              ? ` · 已限制 ${lifecycle.allowed_tools.length} 个`
              : " · 允许全部"}
          </p>
        ) : null}

        <div className={styles.cardFooter}>
          {client.oauth?.enabled &&
          lifecycle?.auth_status === "authorization_required" ? (
            <Button
              className={styles.toolsButton}
              onClick={handleAuthorize}
              icon={<LinkOutlined />}
              loading={lifecycleLoading}
            >
              授权连接
            </Button>
          ) : (
            <>
              <Button
                className={styles.toolsButton}
                onClick={handleShowTools}
                icon={<ToolOutlined />}
                disabled={!client.enabled || toolsLoading}
                loading={toolsLoading}
              >
                {t("mcp.tools")}
              </Button>
              <Tooltip title="检测连接">
                <Button
                  aria-label="检测连接"
                  onClick={handleProbe}
                  icon={<ReloadOutlined />}
                  disabled={!client.enabled}
                  loading={lifecycleLoading}
                />
              </Tooltip>
            </>
          )}
          <Button
            className={styles.toggleButton}
            onClick={(e) => {
              e.stopPropagation();
              handleToggleClick(e);
            }}
            icon={client.enabled ? <EyeInvisibleOutlined /> : <EyeOutlined />}
            loading={pending}
            disabled={pending}
          >
            {client.enabled ? t("common.disable") : t("common.enable")}
          </Button>
          <Button
            className={styles.deleteButton}
            onClick={(e) => {
              e.stopPropagation();
              handleDeleteClick(e);
            }}
          >
            {t("common.delete")}
          </Button>
        </div>
      </Card>

      <Modal
        title={t("common.confirm")}
        open={deleteModalOpen}
        onOk={confirmDelete}
        onCancel={() => setDeleteModalOpen(false)}
        okText={t("common.confirm")}
        cancelText={t("common.cancel")}
        okButtonProps={{ danger: true }}
      >
        <p>{t("mcp.deleteConfirm")}</p>
      </Modal>

      <Modal
        title={`${client.name} - ${t("mcp.tools")}`}
        open={toolsModalOpen}
        onCancel={() => setToolsModalOpen(false)}
        footer={
          <div style={{ textAlign: "right" }}>
            <Button onClick={() => setToolsModalOpen(false)}>
              {t("common.close")}
            </Button>
          </div>
        }
        width={700}
      >
        {toolsLoading ? (
          <div className={styles.toolsLoading}>
            <Spin />
          </div>
        ) : toolsError ? (
          <div className={styles.toolsError}>{toolsError}</div>
        ) : tools.length === 0 ? (
          <Empty description={t("mcp.noTools")} />
        ) : (
          <div className={styles.toolsList}>
            {tools.map((tool) => (
              <div key={tool.name} className={styles.toolItem}>
                <div className={styles.toolHeader}>
                  <Tag color="blue">{tool.name}</Tag>
                </div>
                {tool.description && (
                  <p className={styles.toolDescription}>{tool.description}</p>
                )}
                {tool.input_schema &&
                  Object.keys(tool.input_schema).length > 0 && (
                    <details className={styles.toolSchema}>
                      <summary>{t("mcp.toolSchema")}</summary>
                      <pre className={styles.toolSchemaContent}>
                        {JSON.stringify(tool.input_schema, null, 2)}
                      </pre>
                    </details>
                  )}
              </div>
            ))}
          </div>
        )}
      </Modal>

      <Modal
        title={`${client.name} - Configuration`}
        open={jsonModalOpen}
        onCancel={() => setJsonModalOpen(false)}
        footer={
          <div style={{ textAlign: "right" }}>
            <Button
              onClick={() => setJsonModalOpen(false)}
              style={{ marginRight: 8 }}
            >
              {t("common.cancel")}
            </Button>
            {isEditing ? (
              <Button
                type="primary"
                onClick={handleSaveJson}
                loading={pending}
                disabled={pending}
              >
                {t("common.save")}
              </Button>
            ) : (
              <Button type="primary" onClick={() => setIsEditing(true)}>
                {t("common.edit")}
              </Button>
            )}
          </div>
        }
        width={700}
      >
        <div className={styles.maskedFieldHint}>{t("mcp.maskedFieldHint")}</div>
        {isEditing ? (
          <Input.TextArea
            value={editedJson}
            onChange={(e) => setEditedJson(e.target.value)}
            autoSize={{ minRows: 15, maxRows: 25 }}
            style={{
              fontFamily: "Monaco, Courier New, monospace",
              fontSize: 13,
            }}
          />
        ) : (
          <pre
            style={{
              backgroundColor: isDark ? "#1f1f1f" : "#f5f5f5",
              color: isDark ? "rgba(255,255,255,0.85)" : "rgba(0,0,0,0.88)",
              padding: 16,
              borderRadius: 8,
              maxHeight: 500,
              overflow: "auto",
            }}
          >
            {clientJson}
          </pre>
        )}
      </Modal>
    </>
  );
});
