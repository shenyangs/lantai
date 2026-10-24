import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Dropdown, Spin, Tooltip } from "antd";
import { useAppMessage } from "../../../hooks/useAppMessage";
import {
  CheckOutlined,
  DownOutlined,
  FolderOpenOutlined,
  LoadingOutlined,
  SearchOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { providerApi } from "../../../api/modules/provider";
import { agentsApi } from "../../../api/modules/agents";
import type {
  ProviderInfo,
  ActiveModelsInfo,
  ContextPolicyInfo,
  ModelInfo,
} from "../../../api/types";
import { useAgentStore } from "../../../stores/agentStore";
import { confirmFreeModelSwitch } from "@/utils/freeModelSwitchWarning";
import {
  filterProductVisibleProviders,
  stripProductHiddenActiveModel,
} from "@/utils/providerVisibility";
import { ProviderIcon } from "../../Settings/Models/components/ProviderIconComponent";
import { isChatCapableModel } from "../../Settings/Models/modelProtocol";
import styles from "./index.module.less";
import { createLatestRequestGate } from "../../../utils/latestRequest";
import { Star } from "lucide-react";

interface EligibleProvider {
  id: string;
  name: string;
  base_url?: string;
  is_local: boolean;
  support_model_discovery: boolean;
  models: ModelInfo[];
}

const HIDDEN_DEFAULT_PROVIDER_IDS = new Set(["opencode"]);
const MODEL_GROUP_AUTO_COLLAPSE_THRESHOLD = 4;
const MODEL_HISTORY_LIMIT = 6;
const MODEL_RECENT_STORAGE_PREFIX = "lantai:model-selector:recent:";
const MODEL_FAVORITES_STORAGE_PREFIX = "lantai:model-selector:favorites:";

interface StoredModelReference {
  providerId: string;
  modelId: string;
}

export interface ModelSelectorProps {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

function storedModelReferences(key: string): StoredModelReference[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(key) || "[]");
    if (!Array.isArray(value)) return [];
    return value
      .filter(
        (item): item is StoredModelReference =>
          Boolean(item) &&
          typeof item.providerId === "string" &&
          typeof item.modelId === "string",
      )
      .slice(0, MODEL_HISTORY_LIMIT);
  } catch {
    return [];
  }
}

function saveModelReferences(key: string, value: StoredModelReference[]) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // The selector still works when local storage is restricted.
  }
}

function formatContextTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "--";
  if (value >= 1_000_000) {
    return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  }
  if (value >= 1_000) {
    return `${(value / 1_000).toFixed(1).replace(/\.0$/, "")}K`;
  }
  return String(Math.round(value));
}

export function formatContextPolicyNotice(
  modelName: string,
  policy: ContextPolicyInfo,
): string {
  const budget = formatContextTokens(policy.target_input_tokens);
  const reserve = formatContextTokens(
    policy.output_reserve + policy.safety_margin,
  );
  if (policy.used_unknown_fallback) {
    return `已切换至 ${modelName}；模型窗口待验证，本轮暂按 ${budget} 可用输入运行，并为输出与安全留出 ${reserve}。`;
  }
  return `已切换至 ${modelName}；本轮可用输入 ${budget}，并为输出与安全留出 ${reserve}。`;
}

export default function ModelSelector({
  open: controlledOpen,
  onOpenChange,
}: ModelSelectorProps = {}) {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [activeModels, setActiveModels] = useState<ActiveModelsInfo | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [internalOpen, setInternalOpen] = useState(false);
  const open = controlledOpen ?? internalOpen;
  const setOpen = useCallback(
    (next: boolean) => {
      if (controlledOpen === undefined) setInternalOpen(next);
      onOpenChange?.(next);
    },
    [controlledOpen, onOpenChange],
  );
  const savingRef = useRef(false);
  const localDiscoveryRef = useRef(false);
  const localDiscoveryAttemptedRef = useRef(false);
  const dataLoadGateRef = useRef(createLatestRequestGate());
  const location = useLocation();
  const navigate = useNavigate();
  const { selectedAgent, agents = [], setAgents = () => {} } = useAgentStore();
  const { message } = useAppMessage();
  const [query, setQuery] = useState("");
  const [providerExpansionOverrides, setProviderExpansionOverrides] = useState<
    Record<string, boolean>
  >({});
  const recentStorageKey = `${MODEL_RECENT_STORAGE_PREFIX}${selectedAgent}`;
  const favoritesStorageKey = `${MODEL_FAVORITES_STORAGE_PREFIX}${selectedAgent}`;
  const [recentModels, setRecentModels] = useState<StoredModelReference[]>(() =>
    storedModelReferences(recentStorageKey),
  );
  const [favoriteModels, setFavoriteModels] = useState<StoredModelReference[]>(
    () => storedModelReferences(favoritesStorageKey),
  );
  const fetchData = useCallback(async () => {
    const requestedAgent = selectedAgent;
    const ownsState = dataLoadGateRef.current.begin();
    setLoading(true);
    setLoadError("");
    try {
      const [provData, activeData] = await Promise.all([
        providerApi.listProviders(),
        providerApi.getActiveModels({
          scope: "effective",
          agent_id: selectedAgent,
        }),
      ]);
      if (
        !ownsState() ||
        useAgentStore.getState().selectedAgent !== requestedAgent
      ) {
        return;
      }
      if (Array.isArray(provData)) {
        setProviders(filterProductVisibleProviders(provData));
      }
      if (activeData) {
        setActiveModels(stripProductHiddenActiveModel(activeData));
      }
      setLoadError("");
    } catch (err) {
      if (!ownsState()) return;
      console.error("ModelSelector: failed to load data", err);
      setLoadError("模型列表加载失败，请检查模型服务后重试。");
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, [selectedAgent]);

  useEffect(() => {
    void fetchData();
    return () => dataLoadGateRef.current.invalidate();
  }, [fetchData]);

  useEffect(() => {
    setRecentModels(storedModelReferences(recentStorageKey));
    setFavoriteModels(storedModelReferences(favoritesStorageKey));
  }, [favoritesStorageKey, recentStorageKey]);

  // Re-sync active model whenever the route switches back to /chat
  const prevPathRef = useRef(location.pathname);
  useEffect(() => {
    const prev = prevPathRef.current;
    const curr = location.pathname;
    prevPathRef.current = curr;
    const comingToChat = curr.startsWith("/chat") && !prev.startsWith("/chat");
    if (comingToChat) {
      void fetchData();
    }
  }, [fetchData, location.pathname, selectedAgent]);

  const mergeDiscoveredModels = useCallback(
    (providerId: string, discoveredModels: ModelInfo[]) => {
      setProviders((prev) =>
        prev.map((provider) => {
          if (provider.id !== providerId) return provider;
          const existingIds = new Set(
            [...(provider.models ?? []), ...(provider.extra_models ?? [])].map(
              (model) => model.id,
            ),
          );
          const newModels = discoveredModels.filter(
            (model) => !existingIds.has(model.id),
          );
          if (newModels.length === 0) return provider;
          return {
            ...provider,
            extra_models: [...(provider.extra_models ?? []), ...newModels],
          };
        }),
      );
    },
    [],
  );

  const discoverLocalProviders = useCallback(async () => {
    if (localDiscoveryRef.current || localDiscoveryAttemptedRef.current) return;
    const localProviders = providers.filter(
      (provider) =>
        provider.is_local &&
        provider.support_model_discovery &&
        (provider.base_url || provider.id === "ollama"),
    );
    if (localProviders.length === 0) return;

    localDiscoveryRef.current = true;
    localDiscoveryAttemptedRef.current = true;
    try {
      const results = await Promise.allSettled(
        localProviders.map(async (provider) => {
          const result = await providerApi.discoverModels(
            provider.id,
            undefined,
            true,
          );
          if (result.success && result.models.length > 0) {
            mergeDiscoveredModels(provider.id, result.models);
          }
        }),
      );

      if (results.some((result) => result.status === "fulfilled")) {
        const refreshedProviders = await providerApi.listProviders();
        if (Array.isArray(refreshedProviders)) {
          setProviders(filterProductVisibleProviders(refreshedProviders));
        }
      }
    } finally {
      localDiscoveryRef.current = false;
    }
  }, [mergeDiscoveredModels, providers]);

  const activeProviderId = activeModels?.active_llm?.provider_id;
  const activeModelId = activeModels?.active_llm?.model;

  // Eligible providers must have selectable models. Local discovery providers
  // are probed silently, but stay hidden until they return real model rows.
  const eligibleProviders: EligibleProvider[] = providers
    .filter((p) => {
      if (HIDDEN_DEFAULT_PROVIDER_IDS.has(p.id) && p.id !== activeProviderId) {
        return false;
      }
      const hasModels = [...(p.models ?? []), ...(p.extra_models ?? [])].some(
        isChatCapableModel,
      );
      if (p.is_local && p.support_model_discovery) {
        return hasModels;
      }
      if (!hasModels) return false;
      if (p.require_api_key === false) return !!p.base_url;
      if (p.is_custom) return !!p.base_url;
      if (p.require_api_key ?? true) return !!p.api_key;
      return true;
    })
    .map((p) => ({
      id: p.id,
      name: p.name,
      base_url: p.base_url,
      is_local: p.is_local,
      support_model_discovery: p.support_model_discovery,
      models: [...(p.models ?? []), ...(p.extra_models ?? [])].filter(
        isChatCapableModel,
      ),
    }));

  const activeProvider = eligibleProviders.find(
    (provider) => provider.id === activeProviderId,
  );
  const activeModel = activeProvider?.models.find(
    (model) => model.id === activeModelId,
  );
  const selectedAgentInfo = agents.find((agent) => agent.id === selectedAgent);
  const selectedAgentLabel = selectedAgentInfo?.name || selectedAgent;
  const workspaceAccessRequired = Boolean(
    selectedAgentInfo?.workspace_access_required,
  );

  // Display label for trigger button
  const activeModelName = workspaceAccessRequired
    ? "恢复工作区"
    : activeModel
      ? activeModel.name || activeModel.id
      : t("modelSelector.selectModel");

  const showActiveProviderIcon = Boolean(activeProvider && activeModel);
  const normalizedQuery = query.trim().toLowerCase();
  const visibleProviders = eligibleProviders
    .map((provider) => {
      const providerMatched =
        provider.name.toLowerCase().includes(normalizedQuery) ||
        provider.id.toLowerCase().includes(normalizedQuery);
      const models = normalizedQuery
        ? provider.models.filter(
            (model) =>
              providerMatched ||
              model.id.toLowerCase().includes(normalizedQuery) ||
              (model.name || "").toLowerCase().includes(normalizedQuery),
          )
        : provider.models;
      return { ...provider, models };
    })
    .filter((provider) => provider.models.length > 0);

  const resolveStoredModel = useCallback(
    (reference: StoredModelReference) => {
      const provider = eligibleProviders.find(
        (item) => item.id === reference.providerId,
      );
      const model = provider?.models.find(
        (item) => item.id === reference.modelId,
      );
      return provider && model ? { provider, model } : null;
    },
    [eligibleProviders],
  );
  const favoriteKeys = useMemo(
    () =>
      new Set(
        favoriteModels.map((item) => `${item.providerId}\u0000${item.modelId}`),
      ),
    [favoriteModels],
  );
  const quickModels = useMemo(() => {
    const candidates: Array<{
      reference: StoredModelReference;
      reason: "当前" | "常用" | "最近";
    }> = [];
    if (activeProviderId && activeModelId) {
      candidates.push({
        reference: { providerId: activeProviderId, modelId: activeModelId },
        reason: "当前",
      });
    }
    favoriteModels.forEach((reference) =>
      candidates.push({ reference, reason: "常用" }),
    );
    recentModels.forEach((reference) =>
      candidates.push({ reference, reason: "最近" }),
    );
    const seen = new Set<string>();
    return candidates
      .map((candidate) => {
        const key = `${candidate.reference.providerId}\u0000${candidate.reference.modelId}`;
        if (seen.has(key)) return null;
        seen.add(key);
        const resolved = resolveStoredModel(candidate.reference);
        return resolved ? { ...resolved, reason: candidate.reason } : null;
      })
      .filter((item): item is NonNullable<typeof item> => Boolean(item))
      .slice(0, MODEL_HISTORY_LIMIT);
  }, [
    activeModelId,
    activeProviderId,
    favoriteModels,
    recentModels,
    resolveStoredModel,
  ]);

  const rememberRecentModel = useCallback(
    (providerId: string, modelId: string) => {
      setRecentModels((current) => {
        const next = [
          { providerId, modelId },
          ...current.filter(
            (item) =>
              item.providerId !== providerId || item.modelId !== modelId,
          ),
        ].slice(0, MODEL_HISTORY_LIMIT);
        saveModelReferences(recentStorageKey, next);
        return next;
      });
    },
    [recentStorageKey],
  );

  const toggleFavoriteModel = useCallback(
    (providerId: string, modelId: string) => {
      setFavoriteModels((current) => {
        const exists = current.some(
          (item) => item.providerId === providerId && item.modelId === modelId,
        );
        const next = exists
          ? current.filter(
              (item) =>
                item.providerId !== providerId || item.modelId !== modelId,
            )
          : [{ providerId, modelId }, ...current].slice(0, MODEL_HISTORY_LIMIT);
        saveModelReferences(favoritesStorageKey, next);
        return next;
      });
    },
    [favoritesStorageKey],
  );

  const handleOpenChange = useCallback(
    async (next: boolean) => {
      setOpen(next);
      if (!next) {
        localDiscoveryAttemptedRef.current = false;
        return;
      }
      if (next) {
        // Re-fetch active model every time the dropdown opens
        const requestedAgent = selectedAgent;
        const ownsState = dataLoadGateRef.current.begin();
        try {
          const [activeData, providerData] = await Promise.all([
            providerApi.getActiveModels({
              scope: "effective",
              agent_id: selectedAgent,
            }),
            providerApi.listProviders(),
          ]);
          if (
            !ownsState() ||
            useAgentStore.getState().selectedAgent !== requestedAgent
          ) {
            return;
          }
          if (activeData) {
            setActiveModels(stripProductHiddenActiveModel(activeData));
          }
          if (Array.isArray(providerData)) {
            setProviders(filterProductVisibleProviders(providerData));
          }
          setLoadError("");
        } catch (error) {
          if (!ownsState()) return;
          console.error("ModelSelector: failed to refresh data", error);
          setLoadError("模型列表加载失败，请检查模型服务后重试。");
        }
      }
    },
    [selectedAgent, setOpen],
  );

  useEffect(() => {
    if (!open) return;
    void discoverLocalProviders();
  }, [discoverLocalProviders, open]);

  const handleSelect = async (providerId: string, modelId: string) => {
    if (savingRef.current) return;
    if (providerId === activeProviderId && modelId === activeModelId) {
      setOpen(false);
      return;
    }

    const targetProvider = eligibleProviders.find(
      (provider) => provider.id === providerId,
    );
    const targetModel = targetProvider?.models.find(
      (model) => model.id === modelId,
    );

    setOpen(false);

    if (targetProvider && targetModel) {
      const confirmed = await confirmFreeModelSwitch({
        provider: targetProvider,
        model: targetModel,
        t,
      });
      if (!confirmed) return;
    }
    const mutationAgent = selectedAgent;
    if (useAgentStore.getState().selectedAgent !== mutationAgent) return;

    savingRef.current = true;
    setSaving(true);
    try {
      const result = await providerApi.setActiveLlm({
        provider_id: providerId,
        model: modelId,
        scope: "agent",
        agent_id: mutationAgent,
      });
      if (useAgentStore.getState().selectedAgent !== mutationAgent) return;
      setActiveModels({
        active_llm: { provider_id: providerId, model: modelId },
        context_policy: result.context_policy,
      });
      rememberRecentModel(providerId, modelId);
      if (result.context_policy) {
        const notice = formatContextPolicyNotice(
          targetModel?.name || targetModel?.id || modelId,
          result.context_policy,
        );
        if (result.context_policy.used_unknown_fallback) {
          message.warning(notice);
        } else {
          message.success(notice);
        }
      }
      // Notify ChatPage to refresh multimodal capabilities
      window.dispatchEvent(new CustomEvent("model-switched"));
    } catch (err) {
      const msg =
        err instanceof Error ? err.message : t("modelSelector.switchFailed");
      message.error(msg);
    } finally {
      setSaving(false);
      savingRef.current = false;
    }
  };

  const renderModelRow = (
    provider: EligibleProvider,
    model: ModelInfo,
    reason?: "当前" | "常用" | "最近",
  ) => {
    const isActive =
      provider.id === activeProviderId && model.id === activeModelId;
    const modelKey = `${provider.id}\u0000${model.id}`;
    const favorite = favoriteKeys.has(modelKey);
    const label = model.name || model.id;
    return (
      <div className={styles.modelRowShell} key={`${provider.id}:${model.id}`}>
        <button
          type="button"
          className={[
            styles.modelItem,
            isActive ? styles.modelItemActive : "",
          ].join(" ")}
          aria-label={`选择 ${label}，提供商 ${provider.name}`}
          onClick={(event) => {
            event.stopPropagation();
            void handleSelect(provider.id, model.id);
          }}
        >
          <span className={styles.modelText}>
            <span className={styles.modelName}>{label}</span>
            <span
              className={styles.modelIdentity}
              title={`${provider.id}/${model.id}`}
            >
              {provider.name} · {model.id}
            </span>
          </span>
          {reason ? <span className={styles.quickReason}>{reason}</span> : null}
          {model.is_free && <span className={styles.freeBadge}>免费</span>}
          {isActive && <CheckOutlined className={styles.checkIcon} />}
        </button>
        <button
          type="button"
          className={`${styles.favoriteButton} ${
            favorite ? styles.favoriteButtonActive : ""
          }`}
          aria-label={`${favorite ? "取消常用" : "设为常用"}：${label}`}
          aria-pressed={favorite}
          title={favorite ? "取消常用" : "设为常用"}
          onClick={(event) => {
            event.stopPropagation();
            toggleFavoriteModel(provider.id, model.id);
          }}
        >
          <Star size={14} fill={favorite ? "currentColor" : "none"} />
        </button>
      </div>
    );
  };

  const openModelSettings = () => {
    setOpen(false);
    navigate("/models");
  };

  const setProviderExpanded = useCallback(
    (providerId: string, expanded: boolean) => {
      setProviderExpansionOverrides((current) => ({
        ...current,
        [providerId]: expanded,
      }));
    },
    [],
  );

  const restoreWorkspace = async () => {
    if (restoring || !selectedAgentInfo) return;
    const desktopRestore = window.electronAPI?.restoreWorkspace;
    if (!desktopRestore) {
      message.error("当前环境不支持桌面工作区恢复。");
      return;
    }
    const mutationAgent = selectedAgent;
    setRestoring(true);
    try {
      const staged = await desktopRestore({
        agentId: mutationAgent,
        expectedPath: selectedAgentInfo.workspace_dir,
      });
      if (!staged) return;
      if (!staged.ok || !staged.stagedPath || !staged.sourcePath) {
        throw new Error(staged.error || "工作区复制失败。");
      }
      await agentsApi.restoreWorkspace(mutationAgent, {
        expected_source_dir: staged.sourcePath,
        staged_workspace_dir: staged.stagedPath,
      });
      const refreshed = await agentsApi.listAgents();
      if (useAgentStore.getState().selectedAgent !== mutationAgent) return;
      setAgents(refreshed.agents || []);
      await fetchData();
      setOpen(false);
      window.dispatchEvent(new CustomEvent("workspace-restored"));
      message.success("工作区已恢复，原目录未删除。");
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "工作区恢复失败。",
      );
    } finally {
      setRestoring(false);
    }
  };

  const dropdownContent = (
    <div className={styles.panel}>
      {!workspaceAccessRequired && (
        <div className={styles.scopeNotice}>
          <span>作用范围</span>
          <strong>当前智能体 · {selectedAgentLabel}</strong>
          <small>立即影响新消息；历史回复保留原模型</small>
        </div>
      )}
      {!workspaceAccessRequired && (
        <div className={styles.searchBox}>
          <SearchOutlined />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索模型或供应商..."
            onClick={(event) => event.stopPropagation()}
          />
        </div>
      )}

      {workspaceAccessRequired ? (
        <div className={styles.recoveryPanel}>
          <FolderOpenOutlined className={styles.recoveryIcon} />
          <div className={styles.recoveryText}>
            <strong>恢复“{selectedAgentInfo?.name || selectedAgent}”</strong>
            <span title={selectedAgentInfo?.workspace_dir}>
              请选择原工作区文件夹。澜台会先完整复制，再原子切换配置。
            </span>
          </div>
          <button
            type="button"
            className={styles.recoveryButton}
            disabled={restoring}
            onClick={(event) => {
              event.stopPropagation();
              void restoreWorkspace();
            }}
          >
            {restoring ? <LoadingOutlined /> : <FolderOpenOutlined />}
            {restoring ? "正在复制..." : "选择并恢复"}
          </button>
        </div>
      ) : (
        <div className={styles.modelList}>
          {loading ? (
            <div className={styles.spinWrapper}>
              <Spin size="small" />
            </div>
          ) : loadError ? (
            <div className={styles.loadFailure} role="alert">
              <span>{loadError}</span>
              <button
                type="button"
                onClick={(event) => {
                  event.stopPropagation();
                  void fetchData();
                }}
              >
                重新加载
              </button>
            </div>
          ) : eligibleProviders.length === 0 ? (
            <div className={styles.emptyTip}>
              {t("modelSelector.noConfiguredModels")}
            </div>
          ) : visibleProviders.length === 0 ? (
            <div className={styles.emptyTip}>没有匹配的模型</div>
          ) : (
            <>
              {!normalizedQuery && quickModels.length > 0 ? (
                <section
                  className={styles.quickSection}
                  aria-label="当前、常用与最近模型"
                >
                  <div className={styles.sectionLabel}>
                    <strong>快速选择</strong>
                    <span>当前 · 常用 · 最近</span>
                  </div>
                  <div className={styles.quickRows}>
                    {quickModels.map(({ provider, model, reason }) =>
                      renderModelRow(provider, model, reason),
                    )}
                  </div>
                </section>
              ) : null}
              <div className={styles.sectionLabel}>
                <strong>{normalizedQuery ? "搜索结果" : "按提供商浏览"}</strong>
                <span>{visibleProviders.length} 个提供商</span>
              </div>
              {visibleProviders.map((provider) => {
                const isProviderActive = provider.id === activeProviderId;
                const defaultExpanded =
                  isProviderActive &&
                  provider.models.length <= MODEL_GROUP_AUTO_COLLAPSE_THRESHOLD;
                const userExpanded = providerExpansionOverrides[provider.id];
                const isExpanded =
                  normalizedQuery.length > 0
                    ? true
                    : (userExpanded ?? defaultExpanded);
                const providerModelsId = `model-provider-${provider.id.replace(
                  /[^a-zA-Z0-9_-]/g,
                  "-",
                )}`;
                return (
                  <section key={provider.id} className={styles.providerSection}>
                    <button
                      type="button"
                      className={styles.providerHeader}
                      aria-expanded={isExpanded}
                      aria-controls={providerModelsId}
                      aria-label={`${provider.name}，${provider.models.length} 个模型`}
                      disabled={normalizedQuery.length > 0}
                      title={
                        normalizedQuery.length > 0
                          ? `${provider.name} 的匹配结果已展开`
                          : isExpanded
                            ? `收起 ${provider.name}`
                            : `展开 ${provider.name}`
                      }
                      onClick={(event) => {
                        event.stopPropagation();
                        setProviderExpanded(provider.id, !isExpanded);
                      }}
                    >
                      <span className={styles.providerTitle}>
                        <ProviderIcon providerId={provider.id} size={18} />
                        <span>{provider.name}</span>
                      </span>
                      <span
                        className={styles.providerHeaderMeta}
                        aria-hidden="true"
                      >
                        <span className={styles.providerModelCount}>
                          {provider.models.length}
                        </span>
                        {provider.is_local && (
                          <span className={styles.providerBadge}>本地</span>
                        )}
                        <DownOutlined
                          className={[
                            styles.providerChevron,
                            isExpanded ? styles.providerChevronExpanded : "",
                          ].join(" ")}
                        />
                      </span>
                    </button>

                    {isExpanded && (
                      <div id={providerModelsId} className={styles.modelRows}>
                        {provider.models.map((model) =>
                          renderModelRow(provider, model),
                        )}
                      </div>
                    )}
                  </section>
                );
              })}
            </>
          )}
        </div>
      )}

      {!workspaceAccessRequired && (
        <div className={styles.panelFooter}>
          <button
            type="button"
            className={styles.settingsButton}
            onClick={(event) => {
              event.stopPropagation();
              openModelSettings();
            }}
          >
            <SettingOutlined />
            模型设置
          </button>
          <span className={styles.footerHint}>本轮未发送的消息使用新模型</span>
        </div>
      )}
    </div>
  );

  return (
    <Dropdown
      open={open}
      onOpenChange={handleOpenChange}
      popupRender={() => dropdownContent}
      trigger={["click"]}
      placement="bottomLeft"
    >
      <Tooltip
        title={
          workspaceAccessRequired
            ? "恢复智能体工作区"
            : t("chat.modelSelectTooltip")
        }
        mouseEnterDelay={0.5}
      >
        <button
          type="button"
          className={[styles.trigger, open ? styles.triggerActive : ""].join(
            " ",
          )}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label={`${workspaceAccessRequired ? "恢复工作区" : "选择模型"}，当前 ${activeModelName}`}
        >
          {(saving || restoring) && (
            <LoadingOutlined className={styles.triggerSpinner} />
          )}
          {showActiveProviderIcon && activeProviderId && (
            <span className={styles.triggerProviderIcon}>
              <ProviderIcon providerId={activeProviderId} size={18} />
            </span>
          )}
          <span className={styles.triggerName}>{activeModelName}</span>
          <DownOutlined
            className={[
              styles.triggerArrow,
              open ? styles.triggerArrowOpen : "",
            ].join(" ")}
          />
        </button>
      </Tooltip>
    </Dropdown>
  );
}
