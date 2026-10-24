import { useState } from "react";
import { Alert, Button, Empty, Modal, Input } from "@/vendor/lantaiDesign";
import { PlusOutlined } from "@ant-design/icons";
import type { MCPClientInfo } from "../../../api/types";
import {
  BuiltInMCPModules,
  MCPClientCard,
  OfficialConnectorCatalog,
} from "./components";
import { useMCP } from "./useMCP";
import { useTranslation } from "react-i18next";
import { PageHeader } from "@/components/PageHeader";
import { useAppMessage } from "../../../hooks/useAppMessage";
import styles from "./index.module.less";
import { useAgentStore } from "../../../stores/agentStore";
import { normalizeImportedClients } from "./mcpImport";

function MCPPage() {
  const { selectedAgent, agents = [] } = useAgentStore();
  const selectedAgentName =
    agents.find((agent) => agent.id === selectedAgent)?.name || selectedAgent;
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const {
    clients,
    loading,
    loadError,
    toggleEnabled,
    deleteClient,
    createClient,
    updateClient,
    pendingClientKeys,
    loadClients,
  } = useMCP();
  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [newClientJson, setNewClientJson] = useState(`{
  "mcpServers": {
    "example-client": {
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "env": {
        "API_KEY": "<YOUR_API_KEY>"
      }
    }
  }
}`);

  const handleToggleEnabled = async (
    client: MCPClientInfo,
    e?: React.MouseEvent,
  ) => {
    e?.stopPropagation();
    await toggleEnabled(client);
  };

  const handleDelete = async (client: MCPClientInfo, e?: React.MouseEvent) => {
    e?.stopPropagation();
    await deleteClient(client);
  };

  const handleCreateClient = async () => {
    try {
      const parsed = JSON.parse(newClientJson);

      // Support two formats:
      // Format 1: { "mcpServers": { "key": { "command": "...", ... } } }
      // Format 2: { "key": { "command": "...", ... } }
      // Format 3: { "key": "...", "name": "...", "command": "...", ... } (direct)

      const clientsToCreate = normalizeImportedClients(parsed);

      if (clientsToCreate.length === 0) {
        message.error(
          "没有识别到可导入的 MCP 服务，请检查配置格式和 command/url 字段。",
        );
        return;
      }

      // Imported clients stay disabled until the user reviews and enables them.
      let allSuccess = true;
      for (const { key, data } of clientsToCreate) {
        const success = await createClient(key, data);
        if (!success) allSuccess = false;
      }

      if (allSuccess) {
        setCreateModalOpen(false);
        setNewClientJson(`{
  "mcpServers": {
    "example-client": {
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "env": {
        "API_KEY": "<YOUR_API_KEY>"
      }
    }
  }
}`);
      }
    } catch (_error) {
      message.error("JSON 格式无效，请检查括号、引号和逗号后重试。");
    }
  };

  return (
    <div className={styles.mcpPage}>
      <PageHeader
        items={[{ title: "设置" }, { title: "服务连接" }]}
        description="连接你实际使用的服务；自定义 MCP 与本地协议配置保留在高级区域。"
        extra={
          <Button
            icon={<PlusOutlined />}
            onClick={() => setCreateModalOpen(true)}
          >
            添加自定义 MCP
          </Button>
        }
      />

      <main className={styles.content} data-testid="mcp-content">
        <aside className={styles.scopeNotice} data-testid="mcp-scope-notice">
          <span>作用范围</span>
          <strong>当前智能体 · {selectedAgentName}</strong>
          <small>连接信息保存在本机；启用后从该智能体的新消息开始生效。</small>
        </aside>
        <OfficialConnectorCatalog onClientsChanged={loadClients} />

        <details
          className={styles.advancedConnections}
          data-testid="mcp-advanced-connections"
        >
          <summary>
            <span>
              <strong>高级与本地能力</strong>
              <small>内置模块、自定义 MCP 和协议级设置</small>
            </span>
            <span aria-hidden="true">展开</span>
          </summary>
          <div className={styles.advancedConnectionsBody}>
            <BuiltInMCPModules />
            <section className={styles.section}>
              <header className={styles.sectionHeader}>
                <h2>{t("mcp.customClients")}</h2>
                <p>{t("mcp.customClientsDescription")}</p>
              </header>

              {loadError ? (
                <Alert
                  type="error"
                  showIcon
                  message={
                    clients.length ? "MCP 列表未更新" : "MCP 列表加载失败"
                  }
                  description={
                    clients.length
                      ? `${loadError}。下面保留的是上一次成功读取的状态。`
                      : `${loadError}。当前无法确认是否已经添加自定义 MCP。`
                  }
                  action={
                    <Button size="small" onClick={() => void loadClients()}>
                      重新读取
                    </Button>
                  }
                />
              ) : null}

              {loading && clients.length === 0 ? (
                <div className={styles.loading}>
                  <p>{t("common.loading")}</p>
                </div>
              ) : clients.length === 0 ? (
                <div className={styles.emptyState}>
                  <Empty description={t("mcp.emptyState")} />
                </div>
              ) : (
                <div className={styles.mcpGrid}>
                  {clients.map((client) => (
                    <MCPClientCard
                      key={client.key}
                      client={client}
                      pending={pendingClientKeys.has(client.key)}
                      onToggle={handleToggleEnabled}
                      onDelete={handleDelete}
                      onUpdate={updateClient}
                    />
                  ))}
                </div>
              )}
            </section>
          </div>
        </details>
      </main>

      <Modal
        title={t("mcp.create")}
        open={createModalOpen}
        onCancel={() => setCreateModalOpen(false)}
        footer={
          <div className={styles.modalFooter}>
            <Button
              onClick={() => setCreateModalOpen(false)}
              style={{ marginRight: 8 }}
            >
              {t("common.cancel")}
            </Button>
            <Button type="primary" onClick={handleCreateClient}>
              {t("common.create")}
            </Button>
          </div>
        }
        width={800}
      >
        <div className={styles.importHint}>
          <p>导入后默认停用；请先核对命令、网络地址和密钥，再手动启用。</p>
          <p className={styles.importHintTitle}>{t("mcp.formatSupport")}:</p>
          <ul className={styles.importHintList}>
            <li>
              {t("mcp.standardFormat")}:{" "}
              <code>{`{ "mcpServers": { "key": {...} } }`}</code>
            </li>
            <li>
              {t("mcp.directFormat")}: <code>{`{ "key": {...} }`}</code>
            </li>
            <li>
              {t("mcp.singleFormat")}:{" "}
              <code>{`{ "key": "...", "name": "...", "command": "..." }`}</code>
            </li>
          </ul>
        </div>
        <Input.TextArea
          value={newClientJson}
          onChange={(e) => setNewClientJson(e.target.value)}
          autoSize={{ minRows: 15, maxRows: 25 }}
          className={styles.jsonTextArea}
        />
      </Modal>
    </div>
  );
}

export default MCPPage;
