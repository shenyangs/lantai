import { useState, useEffect, useMemo, useCallback } from "react";
import {
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Tag,
  Tooltip,
  Switch,
} from "@/vendor/lantaiDesign";
import { AutoComplete } from "antd";
import {
  DeleteOutlined,
  PlusOutlined,
  ApiOutlined,
  EyeOutlined,
  SettingOutlined,
  DownOutlined,
  SearchOutlined,
  ExperimentOutlined,
  AppstoreOutlined,
  VideoCameraOutlined,
  FileTextOutlined,
  QuestionCircleOutlined,
  DatabaseOutlined,
  UserOutlined,
  GiftOutlined,
  ToolOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import type {
  ProviderInfo,
  SeriesResponse,
  ModelInfo,
  ExtendedModelInfo,
} from "../../../../../api/types";

import api from "../../../../../api";
import { useTranslation } from "react-i18next";
import { useTheme } from "../../../../../contexts/ThemeContext";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import { JsonConfigEditor } from "./JsonConfigEditor.tsx";
import {
  getLocalizedTestConnectionMessage,
  getTestConnectionFailureDetail,
} from "./testConnectionMessage";
import { OpenRouterFilterSection } from "./OpenRouterFilterSection";
import { ModelCompatibilityPanel } from "./ModelCompatibilityPanel";
import styles from "../../index.module.less";
import { supportsChatProtocol } from "../../modelProtocol";

function ModelConfigEditor({
  providerId,
  model,
  onSaved,
  onClose,
  isDark,
}: {
  providerId: string;
  model: ModelInfo;
  onSaved: () => void | Promise<void>;
  onClose: () => void;
  isDark: boolean;
}) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [saving, setSaving] = useState(false);

  const initialText = useMemo(
    () =>
      model.generate_kwargs && Object.keys(model.generate_kwargs).length > 0
        ? JSON.stringify(model.generate_kwargs, null, 2)
        : "",
    [model.generate_kwargs],
  );

  const [text, setText] = useState(initialText);
  const [dirty, setDirty] = useState(false);
  const [contextWindow, setContextWindow] = useState<number | null>(
    model.context_window ?? null,
  );
  const [maxOutputTokens, setMaxOutputTokens] = useState<number | null>(
    model.max_output_tokens ?? null,
  );
  const [recommendedReserve, setRecommendedReserve] = useState<number | null>(
    model.recommended_output_reserve ?? null,
  );
  const capabilityDirty =
    contextWindow !== (model.context_window ?? null) ||
    maxOutputTokens !== (model.max_output_tokens ?? null) ||
    recommendedReserve !== (model.recommended_output_reserve ?? null);

  useEffect(() => {
    setText(initialText);
    setDirty(false);
    setContextWindow(model.context_window ?? null);
    setMaxOutputTokens(model.max_output_tokens ?? null);
    setRecommendedReserve(model.recommended_output_reserve ?? null);
  }, [
    initialText,
    model.context_window,
    model.max_output_tokens,
    model.recommended_output_reserve,
  ]);

  const handleChange = useCallback(
    (val: string) => {
      setText(val);
      setDirty(val !== initialText);
    },
    [initialText],
  );

  const handleSave = async () => {
    const trimmed = text.trim();
    let parsed: Record<string, unknown> = {};
    if (trimmed) {
      try {
        const obj = JSON.parse(trimmed);
        if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
          message.error(t("models.generateConfigMustBeObject"));
          return;
        }
        parsed = obj;
      } catch {
        message.error(t("models.generateConfigInvalidJson"));
        return;
      }
    }

    setSaving(true);
    try {
      await api.configureModel(providerId, model.id, {
        generate_kwargs: parsed,
        context_window: contextWindow,
        max_output_tokens: maxOutputTokens,
        recommended_output_reserve: recommendedReserve,
      });
      message.success(t("models.modelConfigSaved", { name: model.name }));
      setDirty(false);
      await onSaved();
      onClose();
    } catch (error) {
      const errMsg =
        error instanceof Error
          ? error.message
          : t("models.modelConfigSaveFailed");
      message.error(errMsg);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div style={{ padding: "8px 0 4px" }}>
      <div
        style={{
          fontSize: 12,
          color: isDark ? "rgba(255,255,255,0.45)" : "#888",
          marginBottom: 4,
        }}
      >
        {t("models.modelGenerateConfigHint")}
      </div>
      <JsonConfigEditor
        value={text}
        onChange={handleChange}
        placeholder={`Example:\n{\n  "extra_body": {\n    "enable_thinking": false\n  },\n  "max_tokens": 2048\n}`}
      />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
          gap: 8,
          marginTop: 10,
        }}
      >
        <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
          <span>
            {t("models.contextWindow", { defaultValue: "上下文硬上限" })}
          </span>
          <InputNumber
            min={1000}
            step={1024}
            value={contextWindow}
            placeholder="例如 262144"
            onChange={(value) => setContextWindow(value)}
          />
        </label>
        <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
          <span>
            {t("models.maxOutputTokens", { defaultValue: "最大输出" })}
          </span>
          <InputNumber
            min={1}
            step={1024}
            value={maxOutputTokens}
            placeholder="例如 32768"
            onChange={(value) => setMaxOutputTokens(value)}
          />
        </label>
        <label style={{ display: "grid", gap: 4, fontSize: 12 }}>
          <span>
            {t("models.recommendedReserve", {
              defaultValue: "建议预留",
            })}
          </span>
          <InputNumber
            min={1}
            step={1024}
            value={recommendedReserve}
            placeholder="例如 50000"
            onChange={(value) => setRecommendedReserve(value)}
          />
        </label>
      </div>
      <div
        style={{
          marginTop: 6,
          color: isDark ? "rgba(255,255,255,0.45)" : "#888",
          fontSize: 11,
          lineHeight: 1.45,
        }}
      >
        {t("models.contextCapabilityHint", {
          defaultValue:
            "仅在官方资料或网关明确给出时填写；未知模型留空将使用保守预算。",
        })}
      </div>
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          marginTop: 8,
          gap: 8,
        }}
      >
        <Button
          type="primary"
          size="small"
          loading={saving}
          disabled={!dirty && !capabilityDirty}
          onClick={handleSave}
        >
          {t("models.save")}
        </Button>
      </div>
    </div>
  );
}

const tagColors = (isDark: boolean) => ({
  multimodal: {
    backgroundColor: isDark ? "rgba(24,144,255,0.15)" : "#e6f7ff",
    color: "#1890ff",
    borderColor: isDark ? "rgba(24,144,255,0.3)" : "#91d5ff",
  },
  vision: {
    backgroundColor: isDark ? "rgba(19,194,194,0.15)" : "#e6fffb",
    color: "#13c2c2",
    borderColor: isDark ? "rgba(19,194,194,0.3)" : "#87e8de",
  },
  video: {
    backgroundColor: isDark ? "rgba(114,46,211,0.15)" : "#f9f0ff",
    color: "#722ed1",
    borderColor: isDark ? "rgba(114,46,211,0.3)" : "#d3adf7",
  },
  text: {
    backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "#f5f5f5",
    color: isDark ? "rgba(255,255,255,0.65)" : "#595959",
    borderColor: isDark ? "rgba(255,255,255,0.15)" : "#d9d9d9",
  },
  notProbed: {
    backgroundColor: isDark ? "rgba(255,255,255,0.1)" : "#f5f5f5",
    color: isDark ? "rgba(255,255,255,0.65)" : "#8c8c8c",
    borderColor: isDark ? "rgba(255,255,255,0.15)" : "#d9d9d9",
  },
  builtin: {
    backgroundColor: isDark ? "rgba(82,196,26,0.15)" : "#f6ffed",
    color: "#52c41a",
    borderColor: isDark ? "rgba(82,196,26,0.3)" : "#b7eb8f",
  },
  free: {
    backgroundColor: isDark ? "rgba(82,196,26,0.15)" : "#f6ffed",
    color: "#52c41a",
    borderColor: isDark ? "rgba(82,196,26,0.3)" : "#b7eb8f",
  },
  userAdded: {
    backgroundColor: isDark ? "rgba(24,144,255,0.15)" : "#e6f7ff",
    color: "#1890ff",
    borderColor: isDark ? "rgba(24,144,255,0.3)" : "#91d5ff",
  },
  tools: {
    backgroundColor: isDark ? "rgba(82,196,26,0.15)" : "#f6ffed",
    color: "#389e0d",
    borderColor: isDark ? "rgba(82,196,26,0.3)" : "#b7eb8f",
  },
  chatOnly: {
    backgroundColor: isDark ? "rgba(250,173,20,0.14)" : "#fffbe6",
    color: isDark ? "#ffc53d" : "#ad6800",
    borderColor: isDark ? "rgba(250,173,20,0.3)" : "#ffe58f",
  },
});

interface RemoteModelManageModalProps {
  provider: ProviderInfo;
  open: boolean;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}

function CapabilityTags({
  model,
  isDark,
}: {
  model: ModelInfo;
  isDark: boolean;
}) {
  const { t } = useTranslation();
  const c = tagColors(isDark);
  if (model.supports_image && model.supports_video) {
    return (
      <Tag style={{ fontSize: 11, marginRight: 4, ...c.multimodal }}>
        <AppstoreOutlined style={{ fontSize: 10, marginRight: 3 }} />
        {t("models.tagMultimodal", "多模态")}
      </Tag>
    );
  }
  if (model.supports_image) {
    return (
      <Tag style={{ fontSize: 11, marginRight: 4, ...c.vision }}>
        <EyeOutlined style={{ fontSize: 10, marginRight: 3 }} />
        {t("models.tagVision", "视觉")}
      </Tag>
    );
  }
  if (model.supports_video) {
    return (
      <Tag style={{ fontSize: 11, marginRight: 4, ...c.video }}>
        <VideoCameraOutlined style={{ fontSize: 10, marginRight: 3 }} />
        {t("models.tagVideo", "视频")}
      </Tag>
    );
  }
  if (model.supports_multimodal === false) {
    return (
      <Tag style={{ fontSize: 11, marginRight: 4, ...c.text }}>
        <FileTextOutlined style={{ fontSize: 10, marginRight: 3 }} />
        {t("models.tagText", "文本")}
      </Tag>
    );
  }
  return (
    <Tag style={{ fontSize: 11, marginRight: 4, ...c.notProbed }}>
      <QuestionCircleOutlined style={{ fontSize: 10, marginRight: 3 }} />
      {t("models.tagNotProbed", "未检测")}
    </Tag>
  );
}

function ToolCapabilityTag({
  model,
  isDark,
}: {
  model: ModelInfo;
  isDark: boolean;
}) {
  const { t } = useTranslation();
  const colors = tagColors(isDark);

  if (model.supports_tool_use == null) {
    return null;
  }

  return (
    <Tag
      style={{
        fontSize: 11,
        marginRight: 4,
        ...(model.supports_tool_use ? colors.tools : colors.chatOnly),
      }}
    >
      {model.supports_tool_use ? (
        <ToolOutlined style={{ fontSize: 10, marginRight: 3 }} />
      ) : (
        <FileTextOutlined style={{ fontSize: 10, marginRight: 3 }} />
      )}
      {t(
        model.supports_tool_use
          ? "models.tagAgentTools"
          : "models.tagNoAgentTools",
      )}
    </Tag>
  );
}

function formatTokenCount(value: number): string {
  if (value >= 1_000_000) {
    return `${Math.round(value / 100_000) / 10}M`;
  }
  if (value >= 1_000) {
    return `${Math.round(value / 100) / 10}K`;
  }
  return String(value);
}

function formatModelSize(value: number): string {
  if (value >= 1024 ** 3) {
    return `${(value / 1024 ** 3).toFixed(1)} GB`;
  }
  return `${Math.max(1, Math.round(value / 1024 ** 2))} MB`;
}

export function RemoteModelManageModal({
  provider,
  open,
  onClose,
  onSaved,
}: RemoteModelManageModalProps) {
  const { t } = useTranslation();
  const { isDark } = useTheme();
  const darkBtnStyle = isDark ? { color: "rgba(255,255,255,0.65)" } : undefined;
  const { message } = useAppMessage();
  const supportsAutoDiscover = provider.support_model_discovery;
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [discoveringModels, setDiscoveringModels] = useState(false);
  const [testingModelId, setTestingModelId] = useState<string | null>(null);
  const [probingModelId, setProbingModelId] = useState<string | null>(null);
  const [visibilityModelId, setVisibilityModelId] = useState<string | null>(
    null,
  );
  const [configOpenModelId, setConfigOpenModelId] = useState<string | null>(
    null,
  );
  const [compatibilityOpenModelId, setCompatibilityOpenModelId] = useState<
    string | null
  >(null);
  const [modelSearchQuery, setModelSearchQuery] = useState("");
  const [form] = Form.useForm();
  // OpenRouter filter state
  const isOpenRouter = provider.id === "openrouter";
  const [showFilters, setShowFilters] = useState(false);
  const [availableSeries, setAvailableSeries] = useState<string[]>([]);
  const [discoveredModels, setDiscoveredModels] = useState<ExtendedModelInfo[]>(
    [],
  );
  const [selectedSeries, setSelectedSeries] = useState<string[]>([]);
  const [selectedInputModalities, setSelectedInputModalities] = useState<
    string[]
  >([]);
  const [showFreeOnly, setShowFreeOnly] = useState(false);
  const [loadingFilters, setLoadingFilters] = useState(false);

  const [loadingDiscoveredModels, setLoadingDiscoveredModels] = useState(false);

  // For custom providers ALL models are deletable.
  // For built-in providers only extra_models are deletable.
  const extraModelIds = new Set((provider.extra_models || []).map((m) => m.id));

  const doAddModel = async (id: string, name: string) => {
    await api.addModel(provider.id, { id, name });
    message.success(t("models.modelAdded", { name }));
    form.resetFields();
    setAdding(false);
    onSaved();
  };

  const handleAddModel = async () => {
    try {
      const values = await form.validateFields();
      const id = values.id.trim();
      const name = values.name?.trim() || id;
      const modelAlreadyExists = [
        ...(provider.models ?? []),
        ...(provider.extra_models ?? []),
      ].some((model) => model.id.trim() === id);

      if (modelAlreadyExists) {
        message.warning(t("models.modelAlreadyExists", { id }));
        return;
      }

      // Step 1: Test the model connection first
      setSaving(true);
      const testResult = await api.testModelConnection(provider.id, {
        model_id: id,
      });

      if (!testResult.success) {
        // Test failed – ask user whether to proceed anyway
        setSaving(false);
        const failureDetail =
          getTestConnectionFailureDetail(testResult.message) ||
          t("models.modelTestFailed");
        Modal.confirm({
          title: t("models.testConnectionFailed"),
          content: t("models.modelTestFailedConfirm", {
            message: failureDetail,
          }),
          okText: t("models.addModel"),
          cancelText: t("models.cancel"),
          onOk: async () => {
            setSaving(true);
            try {
              await doAddModel(id, name);
            } catch (error) {
              const errMsg =
                error instanceof Error
                  ? error.message
                  : t("models.modelAddFailed");
              message.error(errMsg);
            } finally {
              setSaving(false);
            }
          },
        });
        return;
      }

      // Step 2: If test passed, add the model
      await doAddModel(id, name);
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) return;
      const errMsg =
        error instanceof Error ? error.message : t("models.modelAddFailed");
      message.error(errMsg);
    } finally {
      setSaving(false);
    }
  };

  const handleTestModel = async (modelId: string) => {
    setTestingModelId(modelId);
    try {
      const result = await api.testModelConnection(provider.id, {
        model_id: modelId,
      });
      if (result.success) {
        message.success(getLocalizedTestConnectionMessage(result, t));
      } else {
        message.warning(getLocalizedTestConnectionMessage(result, t));
      }
    } catch (error) {
      const errMsg =
        error instanceof Error
          ? error.message
          : t("models.testConnectionError");
      message.error(errMsg);
    } finally {
      setTestingModelId(null);
    }
  };

  const handleProbeMultimodal = async (modelId: string) => {
    setProbingModelId(modelId);
    try {
      const result = await api.probeMultimodal(provider.id, modelId);
      const parts: string[] = [];
      if (result.supports_image) parts.push(t("models.probeImage"));

      if (result.supports_video) parts.push(t("models.probeVideo"));

      if (parts.length > 0) {
        message.success(
          t("models.probeSupported", {
            types: parts.join(", "),
          }),
        );
      } else {
        message.info(t("models.probeNotSupported"));
      }
      await onSaved();
    } catch (error) {
      const errMsg =
        error instanceof Error ? error.message : t("models.probeFailed");

      message.error(errMsg);
    } finally {
      setProbingModelId(null);
    }
  };

  const handleChatVisibility = async (model: ModelInfo, checked: boolean) => {
    setVisibilityModelId(model.id);
    try {
      await api.configureModel(provider.id, model.id, {
        show_in_chat: checked,
      });
      await onSaved();
      message.success(checked ? "已加入聊天下拉栏" : "已从聊天下拉栏隐藏");
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : "无法更新聊天显示设置",
      );
    } finally {
      setVisibilityModelId(null);
    }
  };

  const handleRemoveModel = (modelId: string, modelName: string) => {
    Modal.confirm({
      title: t("models.removeModel"),
      content: t("models.removeModelConfirm", {
        name: modelName,
        provider: provider.name,
      }),
      okText: t("common.delete"),
      okButtonProps: { danger: true },
      cancelText: t("models.cancel"),
      onOk: async () => {
        try {
          await api.removeModel(provider.id, modelId);
          message.success(t("models.modelRemoved", { name: modelName }));
          await onSaved();
        } catch (error) {
          const errMsg =
            error instanceof Error
              ? error.message
              : t("models.modelRemoveFailed");
          message.error(errMsg);
        }
      },
    });
  };

  const handleClose = () => {
    setAdding(false);
    setConfigOpenModelId(null);
    setCompatibilityOpenModelId(null);
    setModelSearchQuery("");
    form.resetFields();
    onClose();
  };

  // Load available series for OpenRouter
  useEffect(() => {
    if (isOpenRouter) {
      api
        .getOpenRouterSeries()
        .then((res: SeriesResponse) => {
          const series = res.series || [];
          setAvailableSeries(series);
          setSelectedSeries((prev) =>
            prev.length === 0
              ? series
              : prev.filter((item) => series.includes(item)),
          );
        })
        .catch(() => {
          setAvailableSeries([]);
          setSelectedSeries([]);
        });
    }
  }, [isOpenRouter]);

  // Fetch models with current filters
  const handleFetchModels = async () => {
    if (!isOpenRouter) return;

    setLoadingFilters(true);
    try {
      const filterBody: Record<string, unknown> = {};
      const hasPartialProviderSelection =
        selectedSeries.length > 0 &&
        selectedSeries.length < availableSeries.length;
      if (hasPartialProviderSelection) {
        filterBody.providers = selectedSeries;
      }
      if (selectedInputModalities.length > 0) {
        filterBody.input_modalities = selectedInputModalities;
      }
      if (showFreeOnly) {
        filterBody.is_free = true;
      }

      const result = await api.filterOpenRouterModels(filterBody);
      if (result.success) {
        setDiscoveredModels(result.models || []);
        message.success(
          t("models.filteredModelsLoaded", { count: result.total_count }),
        );
      } else {
        message.error(t("models.filterFailed"));
      }
    } catch {
      message.error(t("models.filterFailed"));
    } finally {
      setLoadingFilters(false);
    }
  };

  const handleAddFilteredModel = async (model: ExtendedModelInfo) => {
    setSaving(true);
    try {
      await api.addModel(provider.id, {
        id: model.id,
        name: model.name,
        is_free: model.is_free,
        supports_multimodal: model.supports_multimodal,
        supports_image: model.supports_image,
        supports_video: model.supports_video,
        probe_source: model.probe_source,
        context_window: model.context_window,
        max_output_tokens: model.max_output_tokens,
        recommended_output_reserve: model.recommended_output_reserve,
        tokenizer: model.tokenizer,
      });
      message.success(t("models.modelAdded", { name: model.name }));
      await onSaved();
      setDiscoveredModels((prev) => prev.filter((m) => m.id !== model.id));
    } catch {
      message.error(t("models.modelAddFailed"));
    } finally {
      setSaving(false);
    }
  };

  const handleAutoDiscoverModels = async () => {
    setDiscoveringModels(true);
    try {
      const result = await api.discoverModels(provider.id, undefined, true);

      if (!result.success) {
        message.error(result.message || t("models.autoDiscoverModelsFailed"));
        return;
      }

      await onSaved();

      if (result.added_count > 0) {
        message.success(
          t("models.autoDiscoverModelsSuccess", {
            count: result.added_count,
          }),
        );
        return;
      }

      message.info(
        result.message ||
          t("models.autoDiscoverModelsNoNew", {
            count: result.models.length,
          }),
      );
    } catch (error) {
      const errMsg =
        error instanceof Error
          ? error.message
          : t("models.autoDiscoverModelsFailed");
      message.error(errMsg);
    } finally {
      setDiscoveringModels(false);
    }
  };

  useEffect(() => {
    if (!adding) {
      setDiscoveredModels([]);
      return;
    }
    setLoadingDiscoveredModels(true);
    api
      .discoverModels(provider.id, undefined, false)
      .then((result) => {
        const sorted = result.models
          .slice()
          .sort((a, b) => a.id.localeCompare(b.id));
        setDiscoveredModels(sorted as unknown as ExtendedModelInfo[]);
      })
      .catch(() => setDiscoveredModels([]))
      .finally(() => setLoadingDiscoveredModels(false));
  }, [adding, provider.id]);

  useEffect(() => {
    if (!isOpenRouter || !adding) return;
    setAdding(false);
    form.resetFields();
  }, [adding, form, isOpenRouter]);

  const filteredModels = useMemo(() => {
    const all_models = [
      ...(provider.extra_models ?? []),
      ...(provider.models ?? []),
    ];
    const q = modelSearchQuery.trim().toLowerCase();
    if (!q) return all_models;
    return all_models.filter(
      (m) => m.name.toLowerCase().includes(q) || m.id.toLowerCase().includes(q),
    );
  }, [provider.models, provider.extra_models, modelSearchQuery]);

  const colors = tagColors(isDark);

  return (
    <Modal
      title={t("models.manageModelsTitle", { provider: provider.name })}
      open={open}
      onCancel={handleClose}
      footer={null}
      width={800}
      destroyOnHidden
    >
      <Input
        placeholder={t("models.searchModelPlaceholder", "搜索模型...")}
        value={modelSearchQuery}
        onChange={(e) => setModelSearchQuery(e.target.value)}
        prefix={<SearchOutlined />}
        allowClear
      />

      {/* Model list */}
      <div className={styles.modelList}>
        {filteredModels.length === 0 ? (
          <div className={styles.modelListEmpty}>{t("models.noModels")}</div>
        ) : (
          filteredModels.map((m) => {
            const isDeletable = provider.is_custom || extraModelIds.has(m.id);
            const isConfigOpen = configOpenModelId === m.id;
            const isCompatibilityOpen = compatibilityOpenModelId === m.id;
            const runtimeMetadata = [
              m.parameter_size,
              m.quantization,
              m.size_bytes ? formatModelSize(m.size_bytes) : null,
              m.runtime_state === "loaded"
                ? t("models.localModelLoaded")
                : m.runtime_state
                  ? t("models.localModelNotLoaded")
                  : null,
              m.context_window
                ? t("models.localContextWindow", {
                    value: formatTokenCount(m.context_window),
                  })
                : m.max_context_window
                  ? t("models.localMaxContextWindow", {
                      value: formatTokenCount(m.max_context_window),
                    })
                  : null,
            ].filter((item): item is string => Boolean(item));
            return (
              <div key={m.id}>
                <div className={styles.modelListItem}>
                  <div className={styles.modelListItemInfo}>
                    <span className={styles.modelListItemName}>{m.name}</span>
                    <span className={styles.modelListItemId}>{m.id}</span>
                    {runtimeMetadata.length > 0 && (
                      <span className={styles.modelListItemMeta}>
                        {runtimeMetadata.join(" · ")}
                      </span>
                    )}
                  </div>
                  <div className={styles.modelListItemActions}>
                    {provider.id === "tokendance" &&
                      supportsChatProtocol(m) && (
                        <Tooltip title="控制这个模型是否出现在聊天页下拉栏">
                          <label className={styles.chatVisibilityControl}>
                            <span>聊天下拉</span>
                            <Switch
                              size="small"
                              checked={m.show_in_chat === true}
                              loading={visibilityModelId === m.id}
                              onChange={(checked) =>
                                void handleChatVisibility(m, checked)
                              }
                            />
                          </label>
                        </Tooltip>
                      )}
                    <CapabilityTags model={m} isDark={isDark} />
                    <ToolCapabilityTag model={m} isDark={isDark} />
                    {m.is_free && (
                      <Tag
                        style={{
                          fontSize: 11,
                          marginRight: 4,
                          ...colors.free,
                        }}
                      >
                        <GiftOutlined
                          style={{ fontSize: 10, marginRight: 3 }}
                        />
                        {t("models.free")}
                      </Tag>
                    )}
                    <Tag
                      style={{
                        fontSize: 11,
                        marginRight: 4,
                        ...(isDeletable ? colors.userAdded : colors.builtin),
                      }}
                    >
                      {isDeletable ? (
                        <UserOutlined
                          style={{ fontSize: 10, marginRight: 3 }}
                        />
                      ) : (
                        <DatabaseOutlined
                          style={{ fontSize: 10, marginRight: 3 }}
                        />
                      )}
                      {t(isDeletable ? "models.userAdded" : "models.builtin")}
                    </Tag>
                    <span
                      style={{
                        display: "inline-block",
                        width: 1,
                        height: 16,
                        background: isDark
                          ? "rgba(255,255,255,0.15)"
                          : "#e5e7eb",
                        margin: "0 8px",
                        flexShrink: 0,
                      }}
                    />
                    {m.probe_source !== "documentation" && (
                      <Tooltip
                        title={t("models.probeMultimodal", "测试多模态")}
                      >
                        <Button
                          type="text"
                          size="small"
                          icon={<ExperimentOutlined />}
                          onClick={() => handleProbeMultimodal(m.id)}
                          loading={probingModelId === m.id}
                          style={darkBtnStyle}
                        />
                      </Tooltip>
                    )}
                    <Tooltip title={t("models.testConnection")}>
                      <Button
                        type="text"
                        size="small"
                        icon={<ApiOutlined />}
                        onClick={() => handleTestModel(m.id)}
                        loading={testingModelId === m.id}
                        style={darkBtnStyle}
                      />
                    </Tooltip>
                    <Tooltip title={t("models.compatibilityProbeAction")}>
                      <Button
                        type="text"
                        size="small"
                        aria-label={t("models.compatibilityProbeAction")}
                        icon={<SafetyCertificateOutlined />}
                        onClick={() => {
                          setCompatibilityOpenModelId(
                            isCompatibilityOpen ? null : m.id,
                          );
                          setConfigOpenModelId(null);
                        }}
                        style={darkBtnStyle}
                      />
                    </Tooltip>
                    <Tooltip title={t("models.modelConfigLabel", "模型配置")}>
                      <Button
                        type="text"
                        size="small"
                        icon={
                          isConfigOpen ? <DownOutlined /> : <SettingOutlined />
                        }
                        onClick={() => {
                          setConfigOpenModelId(isConfigOpen ? null : m.id);
                          setCompatibilityOpenModelId(null);
                        }}
                        style={darkBtnStyle}
                      />
                    </Tooltip>
                    {isDeletable && (
                      <Button
                        type="text"
                        size="small"
                        danger
                        icon={<DeleteOutlined />}
                        onClick={() => handleRemoveModel(m.id, m.name)}
                      />
                    )}
                  </div>
                </div>
                {isConfigOpen && (
                  <div
                    style={{
                      padding: "0 16px 12px",
                      borderBottom: isDark
                        ? "1px solid rgba(255,255,255,0.06)"
                        : "1px solid #f5f5f5",
                    }}
                  >
                    <ModelConfigEditor
                      providerId={provider.id}
                      model={m}
                      onSaved={onSaved}
                      onClose={() => setConfigOpenModelId(null)}
                      isDark={isDark}
                    />
                  </div>
                )}
                {isCompatibilityOpen && (
                  <ModelCompatibilityPanel
                    providerId={provider.id}
                    modelId={m.id}
                  />
                )}
              </div>
            );
          })
        )}
      </div>

      {isOpenRouter && (
        <OpenRouterFilterSection
          showFilters={showFilters}
          availableSeries={availableSeries}
          selectedSeries={selectedSeries}
          selectedInputModalities={selectedInputModalities}
          showFreeOnly={showFreeOnly}
          loadingFilters={loadingFilters}
          discoveredModels={discoveredModels}
          saving={saving}
          isDark={isDark}
          freeTagStyle={colors.free}
          onToggleFilters={() => setShowFilters(!showFilters)}
          onSelectedSeriesChange={setSelectedSeries}
          onSelectedInputModalitiesChange={setSelectedInputModalities}
          onShowFreeOnlyChange={setShowFreeOnly}
          onFetchModels={handleFetchModels}
          onAddModel={handleAddFilteredModel}
        />
      )}

      {/* Add model section */}
      {!isOpenRouter &&
        (adding ? (
          <div className={styles.modelAddForm}>
            <Form form={form} layout="vertical" style={{ marginBottom: 0 }}>
              <Form.Item
                name="id"
                label={t("models.modelIdLabel")}
                rules={[{ required: true, message: t("models.modelIdLabel") }]}
                style={{ marginBottom: 12 }}
              >
                <AutoComplete
                  placeholder={t("models.modelIdPlaceholder")}
                  options={discoveredModels.map((model) => ({
                    value: model.id,
                    label: model.id,
                  }))}
                  filterOption={(
                    inputValue: string,
                    option?: { value?: string },
                  ) =>
                    option?.value
                      ?.toLowerCase()
                      .includes(inputValue.toLowerCase()) ?? false
                  }
                  notFoundContent={
                    loadingDiscoveredModels
                      ? t("common.loading")
                      : t("models.modelDiscoveryUnavailableHint")
                  }
                >
                  <Input />
                </AutoComplete>
              </Form.Item>
              <Form.Item
                name="name"
                label={t("models.modelNameLabel")}
                style={{ marginBottom: 12 }}
              >
                <Input placeholder={t("models.modelNamePlaceholder")} />
              </Form.Item>
              <div
                style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}
              >
                <Button
                  size="small"
                  onClick={() => {
                    setAdding(false);
                    form.resetFields();
                  }}
                >
                  {t("models.cancel")}
                </Button>
                <Button
                  type="primary"
                  size="small"
                  loading={saving}
                  onClick={handleAddModel}
                >
                  {t("models.addModel")}
                </Button>
              </div>
            </Form>
          </div>
        ) : (
          <div className={styles.modalActionRow}>
            {supportsAutoDiscover && (
              <Button
                icon={<SearchOutlined />}
                loading={discoveringModels}
                onClick={handleAutoDiscoverModels}
                style={{ flex: 1 }}
              >
                {t("models.autoDiscoverModels")}
              </Button>
            )}
            <Button
              type="dashed"
              icon={<PlusOutlined />}
              onClick={() => setAdding(true)}
              style={{ flex: 1 }}
            >
              {t("models.addModel")}
            </Button>
          </div>
        ))}
    </Modal>
  );
}
