import { useState, useRef, useCallback, useEffect, useMemo } from "react";
import {
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Tooltip,
} from "antd";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  FolderOpenOutlined,
  ImportOutlined,
  AppstoreOutlined,
  UnorderedListOutlined,
  PlusOutlined,
  SyncOutlined,
  SwapOutlined,
} from "@ant-design/icons";
import { Eye, EyeOff } from "lucide-react";
import { useTranslation } from "react-i18next";
import { agentsApi } from "../../../api/modules/agents";
import { invalidateSkillCache, skillApi } from "../../../api/modules/skill";
import type { AgentSummary } from "../../../api/types/agents";
import { useAgentStore } from "../../../stores/agentStore";
import { useAgents } from "./useAgents";
import {
  AgentModal,
  AgentTemplateModal,
  type AgentTemplateSelection,
} from "./components";
import {
  CapabilityCard,
  CapabilityChip,
  CapabilityPage,
} from "../../../components/CapabilityLayout";
import { getAgentDisplayName } from "../../../utils/agentDisplayName";
import { ProviderIcon } from "../Models/components/ProviderIconComponent";
import { createLatestRequestGate } from "../../../utils/latestRequest";
import { BeverageAgentAvatar } from "../../../components/BeverageAgentAvatar";

import styles from "./index.module.less";

function isSameModel(
  left: AgentSummary["active_model"],
  right: AgentSummary["active_model"],
) {
  if (!left || !right) return false;
  return left.provider_id === right.provider_id && left.model === right.model;
}

function compactDescription(text: string): string {
  if (!text) return "";
  const pipeIdx = text.indexOf(" | ");
  if (pipeIdx !== -1) return text.slice(0, pipeIdx).trim();
  const nlIdx = text.indexOf("\n");
  if (nlIdx !== -1) return text.slice(0, nlIdx).trim();
  const cnPeriodIdx = text.indexOf("。");
  if (cnPeriodIdx !== -1) return text.slice(0, cnPeriodIdx + 1).trim();
  const enPeriodIdx = text.indexOf(". ");
  if (enPeriodIdx !== -1) return text.slice(0, enPeriodIdx + 1).trim();
  return text.length > 96 ? `${text.slice(0, 96)}...` : text;
}

function compactWorkspace(path: string): string {
  if (!path) return "未绑定工作区";
  const normalized = path.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length <= 2) return normalized;
  return `.../${parts.slice(-2).join("/")}`;
}

export default function AgentsPage() {
  const { t, i18n } = useTranslation();
  const { agents, loading, deleteAgent, toggleAgent, loadAgents } = useAgents();
  const { selectedAgent, setSelectedAgent } = useAgentStore();
  const [modalVisible, setModalVisible] = useState(false);
  const [templateModalVisible, setTemplateModalVisible] = useState(false);
  const [editingAgent, setEditingAgent] = useState<AgentSummary | null>(null);
  const [syncingAllModels, setSyncingAllModels] = useState(false);
  const [agentSearch, setAgentSearch] = useState("");
  const [agentStatus, setAgentStatus] = useState<
    "all" | "enabled" | "disabled"
  >("all");
  const [compactView, setCompactView] = useState(true);

  const [form] = Form.useForm();
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const installedSkillsRef = useRef<string[]>([]);
  const editLoadGateRef = useRef(createLatestRequestGate());
  const { message } = useAppMessage();

  useEffect(
    () => () => {
      editLoadGateRef.current.invalidate();
    },
    [],
  );

  const handleCreate = () => {
    editLoadGateRef.current.invalidate();
    setEditingAgent(null);
    form.resetFields();
    form.setFieldsValue({
      workspace_dir: "",
      active_model_provider: undefined,
      active_model_model: undefined,
      role_template_id: undefined,
      role_template_markdown: undefined,
      role_template_source_name: undefined,
    });
    setSelectedSkills([]);
    installedSkillsRef.current = [];
    setModalVisible(true);
  };

  const handleUseTemplate = (selection: AgentTemplateSelection) => {
    editLoadGateRef.current.invalidate();
    setEditingAgent(null);
    form.resetFields();
    form.setFieldsValue({
      id: selection.suggested_agent_id,
      name: selection.name,
      description: selection.description,
      workspace_dir: "",
      active_model_provider: undefined,
      active_model_model: undefined,
      role_template_id: selection.role_template_id,
      role_template_markdown: selection.role_template_markdown,
      role_template_source_name: selection.role_template_source_name,
    });
    setSelectedSkills(selection.recommended_skills ?? []);
    installedSkillsRef.current = [];
    setTemplateModalVisible(false);
    setModalVisible(true);
  };

  const handleEdit = async (agent: AgentSummary) => {
    const ownsState = editLoadGateRef.current.begin();
    try {
      setSelectedSkills([]);
      installedSkillsRef.current = [];
      invalidateSkillCache({ agentId: agent.id });
      const config = await agentsApi.getAgent(agent.id);
      if (!ownsState()) return;
      setEditingAgent(agent);
      form.setFieldsValue({
        ...config,
        active_model_provider: config.active_model?.provider_id || undefined,
        active_model_model: config.active_model?.model || undefined,
      });
      setModalVisible(true);
    } catch (error) {
      if (!ownsState()) return;
      console.error("Failed to load agent config:", error);
      message.error(t("agent.loadConfigFailed"));
    }
  };

  const handleDelete = async (agentId: string) => {
    try {
      await deleteAgent(agentId);

      if (selectedAgent === agentId) {
        setSelectedAgent("default");
        message.info(t("agent.switchedToDefault"));
      }
    } catch {
      message.error(t("agent.deleteFailed"));
    }
  };

  const handleToggle = async (agentId: string, currentEnabled: boolean) => {
    const newEnabled = !currentEnabled;
    try {
      await toggleAgent(agentId, newEnabled);

      if (!newEnabled && selectedAgent === agentId) {
        setSelectedAgent("default");
        message.info(t("agent.switchedToDefault"));
      }
    } catch {
      // Error already handled in hook
    }
  };

  const handleInstalledSkillsLoaded = useCallback((skills: string[]) => {
    installedSkillsRef.current = skills;
  }, []);

  const handleSubmit = async () => {
    const targetAgent = editingAgent;
    try {
      const values = await form.validateFields();
      const workspaceRaw = values.workspace_dir;
      const workspace_dir =
        typeof workspaceRaw === "string"
          ? workspaceRaw.trim() || undefined
          : workspaceRaw;

      const providerId = values.active_model_provider;
      const modelId = values.active_model_model;
      const active_model =
        providerId && modelId
          ? { provider_id: providerId, model: modelId }
          : null;

      const {
        active_model_provider: _activeModelProvider,
        active_model_model: _activeModelModel,
        role_template_id,
        role_template_markdown,
        role_template_source_name,
        ...rest
      } = values;
      const payload = { ...rest, workspace_dir, active_model };

      if (targetAgent) {
        const previousInstalledSkills = installedSkillsRef.current;
        const newSkills = selectedSkills.filter(
          (skill) => !previousInstalledSkills.includes(skill),
        );

        for (const skill of newSkills) {
          await skillApi.downloadSkillPoolSkill({
            skill_name: skill,
            targets: [{ workspace_id: targetAgent.id }],
          });
        }
        await agentsApi.updateAgent(targetAgent.id, payload);
        installedSkillsRef.current = [
          ...previousInstalledSkills,
          ...newSkills.filter(
            (skill) => !previousInstalledSkills.includes(skill),
          ),
        ];
        invalidateSkillCache({ agentId: targetAgent.id });
        message.success(t("agent.updateSuccess"));
      } else {
        const result = await agentsApi.createAgent({
          ...payload,
          language: i18n.language,
          skill_names: selectedSkills,
          role_template_id,
          role_template_markdown,
          role_template_source_name,
        });
        message.success(`${t("agent.createSuccess")} (ID: ${result.id})`);
      }

      setModalVisible(false);
      await loadAgents();
    } catch (error: any) {
      console.error("Failed to save agent:", error);
      if (targetAgent) {
        invalidateSkillCache({ agentId: targetAgent.id });
      }
      message.error(error.message || t("agent.saveFailed"));
    }
  };

  const defaultAgentModel = useMemo(() => {
    const defaultAgent = agents.find((a) => a.id === "default");
    return defaultAgent?.active_model || null;
  }, [agents]);

  const childAgents = useMemo(
    () => agents.filter((agent) => agent.id !== "default"),
    [agents],
  );

  const unsyncedChildAgents = useMemo(
    () =>
      childAgents.filter(
        (agent) => !isSameModel(agent.active_model, defaultAgentModel),
      ),
    [childAgents, defaultAgentModel],
  );

  const enabledAgentCount = useMemo(
    () => agents.filter((agent) => agent.enabled).length,
    [agents],
  );
  const disabledAgentCount = agents.length - enabledAgentCount;

  const defaultModelLabel = useMemo(() => {
    if (!defaultAgentModel?.model) return "未配置模型";
    return defaultAgentModel.model;
  }, [defaultAgentModel]);

  const isModelSynced = useCallback(
    (agent: AgentSummary) => isSameModel(agent.active_model, defaultAgentModel),
    [defaultAgentModel],
  );

  const filteredAgents = useMemo(() => {
    const query = agentSearch.trim().toLowerCase();
    return agents.filter((agent) => {
      if (agentStatus === "enabled" && !agent.enabled) return false;
      if (agentStatus === "disabled" && agent.enabled) return false;
      if (!query) return true;
      const haystack = [
        agent.id,
        agent.name,
        agent.description,
        agent.workspace_dir,
        agent.active_model?.model,
        agent.active_model?.provider_id,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(query);
    });
  }, [agentSearch, agentStatus, agents]);

  const handleSyncModel = async (agentId: string) => {
    if (!defaultAgentModel) {
      message.warning(t("agent.noDefaultModel"));
      return;
    }
    try {
      const config = await agentsApi.getAgent(agentId);
      await agentsApi.updateAgent(agentId, {
        ...config,
        active_model: defaultAgentModel,
      });
      message.success(t("agent.syncModelSuccess"));
      await loadAgents();
    } catch (error: any) {
      console.error("Failed to sync model:", error);
      message.error(error.message || t("agent.syncModelFailed"));
    }
  };

  const handleSyncAllModels = () => {
    if (!defaultAgentModel) {
      message.warning(t("agent.noDefaultModel"));
      return;
    }

    if (childAgents.length === 0) {
      message.info(t("agent.noChildAgents"));
      return;
    }

    if (unsyncedChildAgents.length === 0) {
      message.success(t("agent.syncAllModelNoop"));
      return;
    }

    Modal.confirm({
      title: t("agent.syncAllModelConfirmTitle"),
      content: t("agent.syncAllModelConfirmDesc", {
        count: unsyncedChildAgents.length,
        model: defaultAgentModel.model,
      }),
      okText: t("agent.syncAllModel"),
      cancelText: t("common.cancel"),
      onOk: async () => {
        setSyncingAllModels(true);
        const failedAgents: string[] = [];

        try {
          for (const agent of unsyncedChildAgents) {
            try {
              const config = await agentsApi.getAgent(agent.id);
              await agentsApi.updateAgent(agent.id, {
                ...config,
                active_model: defaultAgentModel,
              });
            } catch (error) {
              console.error(
                `Failed to sync model for agent ${agent.id}:`,
                error,
              );
              failedAgents.push(agent.name || agent.id);
            }
          }

          await loadAgents();
        } finally {
          setSyncingAllModels(false);
        }

        const syncedCount = unsyncedChildAgents.length - failedAgents.length;
        if (failedAgents.length === 0) {
          message.success(
            t("agent.syncAllModelSuccess", { count: syncedCount }),
          );
        } else if (syncedCount > 0) {
          message.warning(
            t("agent.syncAllModelPartial", {
              success: syncedCount,
              failed: failedAgents.length,
            }),
          );
        } else {
          message.error(t("agent.syncAllModelFailed"));
        }
      },
    });
  };

  return (
    <CapabilityPage
      section="能力"
      title="智能体"
      description="统一管理主智能体与协作智能体的角色、模型、工作区和技能配置，让不同任务由合适的智能体稳定承接。"
      actions={
        <>
          <Button
            icon={<ImportOutlined />}
            onClick={() => setTemplateModalVisible(true)}
          >
            从模板创建
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={handleCreate}
            className={styles.createButton}
          >
            {t("agent.create")}
          </Button>
        </>
      }
      stats={[
        { label: "全部", value: agents.length },
        { label: "启用", value: enabledAgentCount, tone: "success" },
        { label: "停用", value: disabledAgentCount, tone: "muted" },
        { label: "默认模型", value: defaultModelLabel, tone: "info" },
      ]}
      toolbar={
        <div className={styles.capabilityToolbar}>
          <div className={styles.toolbarFilters}>
            <Input
              className={styles.searchInput}
              placeholder="搜索名称、描述、模型或工作区"
              value={agentSearch}
              onChange={(event) => setAgentSearch(event.target.value)}
              allowClear
            />
            <Select
              className={styles.statusSelect}
              value={agentStatus}
              onChange={setAgentStatus}
              options={[
                { value: "all", label: "全部状态" },
                { value: "enabled", label: "仅启用" },
                { value: "disabled", label: "仅停用" },
              ]}
            />
          </div>
          <Button
            icon={
              compactView ? <AppstoreOutlined /> : <UnorderedListOutlined />
            }
            onClick={() => setCompactView((current) => !current)}
            aria-pressed={compactView}
          >
            {compactView ? "卡片视图" : "紧凑列表"}
          </Button>
          <Button
            icon={<SyncOutlined spin={syncingAllModels} />}
            onClick={handleSyncAllModels}
            loading={syncingAllModels}
            disabled={!defaultAgentModel || childAgents.length === 0}
          >
            {t("agent.syncAllModel")}
          </Button>
        </div>
      }
    >
      {loading && agents.length === 0 ? (
        <div
          className={`${styles.capabilityGrid} ${
            compactView ? styles.compactAgentGrid : ""
          }`}
        >
          {Array.from({ length: 6 }).map((_, index) => (
            <div key={index} className={styles.skeletonCard} />
          ))}
        </div>
      ) : filteredAgents.length === 0 ? (
        <div className={styles.emptyState}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="没有匹配的智能体"
          />
        </div>
      ) : (
        <div className={styles.capabilityGrid}>
          {filteredAgents.map((agent) => {
            const isDefault = agent.id === "default";
            const modelSynced = isModelSynced(agent);
            const displayName = getAgentDisplayName(agent, t);
            const roleDescription = isDefault
              ? "沈行知负责理解、判断和协作，澜台负责系统与长期沉淀。"
              : compactDescription(
                  agent.description || "用于专项任务的协作智能体。",
                );
            const desc = (
              <>
                <span className={styles.agentUseHint}>
                  {isDefault
                    ? "使用方式：直接在聊天中协作。"
                    : "使用方式：在聊天中说明所需角色，由沈行知按任务委派。"}
                </span>
                {roleDescription}
              </>
            );

            return (
              <CapabilityCard
                key={agent.id}
                icon={<BeverageAgentAvatar agentId={agent.id} size={40} />}
                title={displayName}
                subtitle={isDefault ? "主工作伙伴 · 直接对话" : "可委派协作者"}
                status={
                  <>
                    <CheckCircleOutlined />
                    {agent.enabled ? "启用" : t("agent.disabled")}
                  </>
                }
                statusTone={agent.enabled ? "success" : "muted"}
                description={desc}
                className={compactView ? styles.agentCompactCard : undefined}
                muted={!agent.enabled}
                chips={
                  <>
                    <Tooltip title={agent.workspace_dir}>
                      <CapabilityChip>
                        <FolderOpenOutlined />
                        <span className={styles.chipText}>
                          {compactWorkspace(agent.workspace_dir)}
                        </span>
                      </CapabilityChip>
                    </Tooltip>
                    {agent.active_model ? (
                      <CapabilityChip tone="info">
                        <ProviderIcon
                          providerId={agent.active_model.provider_id}
                          size={15}
                        />
                        <Tooltip title={agent.active_model.model}>
                          <span className={styles.chipText}>
                            {agent.active_model.model}
                          </span>
                        </Tooltip>
                      </CapabilityChip>
                    ) : (
                      <CapabilityChip tone="muted">
                        {t("agent.modelPlaceholder")}
                      </CapabilityChip>
                    )}
                    {!isDefault && modelSynced ? (
                      <CapabilityChip tone="success">
                        {t("agent.modelSynced")}
                      </CapabilityChip>
                    ) : null}
                  </>
                }
                footer={
                  <>
                    <div>
                      {isDefault ? (
                        <Tooltip title={t("agent.syncAllModelTooltip")}>
                          <Button
                            type="text"
                            icon={<SyncOutlined spin={syncingAllModels} />}
                            onClick={handleSyncAllModels}
                            loading={syncingAllModels}
                            disabled={
                              !defaultAgentModel || childAgents.length === 0
                            }
                            className={styles.softAction}
                          >
                            {t("agent.syncAllModel")}
                          </Button>
                        </Tooltip>
                      ) : defaultAgentModel && !modelSynced ? (
                        <Tooltip title={t("agent.syncModelTooltip")}>
                          <Button
                            type="text"
                            icon={<SwapOutlined />}
                            onClick={() => handleSyncModel(agent.id)}
                            className={styles.softAction}
                          >
                            {t("agent.syncModel")}
                          </Button>
                        </Tooltip>
                      ) : null}
                    </div>
                    <div className={styles.cardActionGroup}>
                      <Button
                        type="text"
                        size="small"
                        icon={<EditOutlined />}
                        onClick={() => handleEdit(agent)}
                        disabled={isDefault}
                        className={styles.softAction}
                        title={
                          isDefault ? t("agent.defaultNotEditable") : undefined
                        }
                      >
                        {t("common.edit")}
                      </Button>
                      <Popconfirm
                        title={
                          agent.enabled
                            ? t("agent.disableConfirm")
                            : t("agent.enableConfirm")
                        }
                        description={
                          agent.enabled
                            ? t("agent.disableConfirmDesc")
                            : t("agent.enableConfirmDesc")
                        }
                        onConfirm={() => handleToggle(agent.id, agent.enabled)}
                        disabled={isDefault}
                        okText={t("common.confirm")}
                        cancelText={t("common.cancel")}
                      >
                        <Button
                          type="text"
                          size="small"
                          icon={
                            agent.enabled ? (
                              <EyeOff size={14} />
                            ) : (
                              <Eye size={14} />
                            )
                          }
                          disabled={isDefault}
                          className={styles.softAction}
                          title={
                            isDefault
                              ? t("agent.defaultNotDisablable")
                              : undefined
                          }
                        >
                          {agent.enabled ? "停用" : "启用"}
                        </Button>
                      </Popconfirm>
                      <Popconfirm
                        title={t("agent.deleteConfirm")}
                        description={t("agent.deleteConfirmDesc")}
                        onConfirm={() => handleDelete(agent.id)}
                        disabled={isDefault}
                        okText={t("common.confirm")}
                        cancelText={t("common.cancel")}
                      >
                        <Button
                          type="text"
                          size="small"
                          danger
                          icon={<DeleteOutlined />}
                          disabled={isDefault}
                          className={`${styles.softAction} ${styles.dangerAction}`}
                          title={
                            isDefault
                              ? t("agent.defaultNotDeletable")
                              : undefined
                          }
                        >
                          {t("common.delete")}
                        </Button>
                      </Popconfirm>
                    </div>
                  </>
                }
              />
            );
          })}
        </div>
      )}

      <AgentModal
        open={modalVisible}
        editingAgent={editingAgent}
        form={form}
        selectedSkills={selectedSkills}
        onSelectedSkillsChange={setSelectedSkills}
        onInstalledSkillsLoaded={handleInstalledSkillsLoaded}
        onSave={handleSubmit}
        onCancel={() => setModalVisible(false)}
      />
      <AgentTemplateModal
        open={templateModalVisible}
        onCancel={() => setTemplateModalVisible(false)}
        onUseTemplate={handleUseTemplate}
      />
    </CapabilityPage>
  );
}
