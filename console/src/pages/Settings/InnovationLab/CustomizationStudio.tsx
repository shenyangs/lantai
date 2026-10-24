import {
  Alert,
  Button,
  Checkbox,
  Empty,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Segmented,
  Select,
  Skeleton,
  Slider,
  Switch,
  Tag,
  Tooltip,
  message,
} from "antd";
import {
  Check,
  ChevronDown,
  Clock3,
  Copy,
  Download,
  FileUp,
  History,
  Library,
  Pencil,
  RotateCcw,
  Search,
  ShieldCheck,
  ShieldAlert,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  Undo2,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
} from "react";
import {
  customizationApi,
  notifyCustomizationChanged,
  type CustomizationPlan,
  type CustomizationEffectiveState,
  type CustomizationPreview,
  type CustomizationProfile,
  type CustomizationRegistryItem,
  type CustomizationRevision,
  type CustomizationSlot,
  type CustomizationState,
} from "../../../api/modules/customization";
import {
  booleanStateLabel,
  formatCustomizationValue,
  getFieldPresentation,
  getNumberMeaning,
  getOptionDescription,
  getOptionLabel,
  valuesEqual,
} from "./customizationPresentation";
import styles from "./CustomizationStudio.module.less";

interface StudioSnapshot {
  profiles: CustomizationProfile[];
  slots: CustomizationSlot[];
  state: CustomizationState;
  registry: CustomizationRegistryItem[];
  effective: CustomizationEffectiveState;
}

interface DraftPlan {
  summary: string;
  plan: CustomizationPlan;
  preview: CustomizationPreview;
  modelLabel: string;
}

const emptyState: CustomizationState = {
  mode: "factory",
  active_profile_id: null,
  active_scoped_profiles: [],
  last_known_good: {},
  updated_at: "",
};

const emptyEffective: CustomizationEffectiveState = {
  values: {},
  sources: {},
  ignored: {},
  warnings: [],
};

const categoryLabels: Record<string, string> = {
  appearance: "外观与材质",
  navigation: "导航与布局",
  conversation: "对话与过程",
  persona: "人格与关系",
  models: "模型路由",
  agents: "Agent 团队",
  work_methods: "工作法",
  memory: "记忆策略",
  permissions: "权限边界",
  automation: "主动性与自动化",
  delivery: "产物与交付",
};

function riskLabel(risk: CustomizationPreview["risk"]) {
  if (risk === "high") return "高风险";
  if (risk === "medium") return "需要确认";
  if (risk === "forbidden") return "不可修改";
  return "低风险";
}

function readableValue(value: unknown) {
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (value === null || value === undefined || value === "") return "默认";
  if (Array.isArray(value)) return value.join("、") || "无";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default function CustomizationStudio() {
  const [messageApi, contextHolder] = message.useMessage();
  const [snapshot, setSnapshot] = useState<StudioSnapshot>({
    profiles: [],
    slots: [],
    state: emptyState,
    registry: [],
    effective: emptyEffective,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [intent, setIntent] = useState("");
  const [planning, setPlanning] = useState(false);
  const [applying, setApplying] = useState(false);
  const [draft, setDraft] = useState<DraftPlan | null>(null);
  const [revisions, setRevisions] = useState<CustomizationRevision[]>([]);
  const [editingName, setEditingName] = useState(false);
  const [profileName, setProfileName] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [importText, setImportText] = useState("");
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedValues, setAdvancedValues] = useState<Record<string, unknown>>(
    {},
  );
  const [advancedResets, setAdvancedResets] = useState<string[]>([]);
  const [advancedMode, setAdvancedMode] = useState<"common" | "expert">(
    "common",
  );
  const [advancedSearch, setAdvancedSearch] = useState("");
  const [customizedOnly, setCustomizedOnly] = useState(false);
  const [expandedCategories, setExpandedCategories] = useState<string[]>([
    "appearance",
  ]);

  const refresh = useCallback(async () => {
    try {
      const [profiles, slots, state, registry, effective] = await Promise.all([
        customizationApi.listProfiles(),
        customizationApi.listSlots(),
        customizationApi.getState(),
        customizationApi.getRegistry(),
        customizationApi.getEffectiveState(),
      ]);
      setSnapshot({
        profiles,
        slots,
        state,
        registry: registry.items,
        effective,
      });
      const historyProfileId = state.active_profile_id;
      setRevisions(
        historyProfileId
          ? await customizationApi.listRevisions(historyProfileId)
          : [],
      );
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法读取个性化配置");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const activeProfile = useMemo(
    () =>
      snapshot.profiles.find(
        (profile) => profile.id === snapshot.state.active_profile_id,
      ) ?? null,
    [snapshot],
  );

  const modifiableRegistry = useMemo(
    () => snapshot.registry.filter((item) => item.risk !== "forbidden"),
    [snapshot.registry],
  );

  const registryGroups = useMemo(() => {
    const groups = new Map<string, CustomizationRegistryItem[]>();
    const query = advancedSearch.trim().toLocaleLowerCase();
    for (const item of modifiableRegistry) {
      const presentation = getFieldPresentation(item);
      if (advancedMode === "common" && presentation.level === "expert")
        continue;
      const isCustomized = Object.prototype.hasOwnProperty.call(
        activeProfile?.overrides || {},
        item.id,
      );
      const isPending =
        Object.prototype.hasOwnProperty.call(advancedValues, item.id) ||
        advancedResets.includes(item.id);
      if (customizedOnly && !isCustomized && !isPending) continue;
      if (
        query &&
        !`${item.title} ${presentation.description} ${advancedMode === "expert" ? item.id : ""}`
          .toLocaleLowerCase()
          .includes(query)
      ) {
        continue;
      }
      const list = groups.get(item.category) || [];
      list.push(item);
      groups.set(item.category, list);
    }
    return Array.from(groups.entries());
  }, [
    activeProfile?.overrides,
    advancedMode,
    advancedResets,
    advancedSearch,
    advancedValues,
    customizedOnly,
    modifiableRegistry,
  ]);

  const advancedChangeCount = useMemo(
    () => new Set([...Object.keys(advancedValues), ...advancedResets]).size,
    [advancedResets, advancedValues],
  );

  const advancedInvalidFields = useMemo(
    () =>
      Object.entries(advancedValues).filter(([itemId, value]) => {
        const item = snapshot.registry.find(
          (candidate) => candidate.id === itemId,
        );
        if (item?.value_type !== "object" || typeof value !== "string")
          return false;
        try {
          JSON.parse(value);
          return false;
        } catch {
          return true;
        }
      }).length,
    [advancedValues, snapshot.registry],
  );

  useEffect(() => {
    setProfileName(activeProfile?.name || "");
    setEditingName(false);
    setAdvancedValues({});
    setAdvancedResets([]);
  }, [activeProfile?.id, activeProfile?.name]);

  const createProfileForSlot = async (slot: CustomizationSlot) => {
    try {
      const profile = await customizationApi.createProfile({
        name: slot.label || `我的澜台 ${slot.index}`,
        icon: "sparkles",
        color: "#4b6581",
      });
      await customizationApi.setSlot(slot.index, {
        profile_id: profile.id,
        label: profile.name,
        icon: profile.icon,
        color: profile.color,
      });
      await customizationApi.activateProfile(profile.id);
      notifyCustomizationChanged();
      await refresh();
      messageApi.success(`已创建“${profile.name}”`);
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "创建失败");
    }
  };

  const ensureTargetProfile = async () => {
    if (activeProfile) return activeProfile;
    const firstEmpty = snapshot.slots.find((slot) => !slot.profile_id);
    const profile = await customizationApi.createProfile({
      name: "我的澜台",
      icon: "sparkles",
      color: "#4b6581",
    });
    if (firstEmpty) {
      await customizationApi.setSlot(firstEmpty.index, {
        profile_id: profile.id,
        label: profile.name,
        icon: profile.icon,
        color: profile.color,
      });
    }
    await customizationApi.activateProfile(profile.id);
    return profile;
  };

  const switchProfile = async (profileId: string) => {
    try {
      await customizationApi.activateProfile(profileId);
      notifyCustomizationChanged();
      await refresh();
      setDraft(null);
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "切换失败");
    }
  };

  const returnToFactory = () => {
    Modal.confirm({
      title: "回到原生澜台？",
      content:
        "所有个性化覆盖会立即停用。聊天、记忆、模型配置、工作区和配置档都不会删除。",
      okText: "回到原生澜台",
      cancelText: "取消",
      onOk: async () => {
        await customizationApi.enableFactoryMode();
        notifyCustomizationChanged();
        await refresh();
        setDraft(null);
        messageApi.success("已回到原生澜台，原配置档仍然保留");
      },
    });
  };

  const resumeActiveProfile = async () => {
    if (!snapshot.state.active_profile_id) return;
    await switchProfile(snapshot.state.active_profile_id);
  };

  const generatePlan = async () => {
    const cleanIntent = intent.trim();
    if (!cleanIntent) return;
    let target = activeProfile;
    setPlanning(true);
    try {
      if (!target) target = await ensureTargetProfile();
      const result = await customizationApi.planFromIntent({
        intent: cleanIntent,
        target_profile_id: target.id,
      });
      setDraft({
        summary: result.summary,
        plan: result.plan,
        preview: result.preview,
        modelLabel: result.model_label,
      });
      await refresh();
    } catch (cause) {
      messageApi.error(
        cause instanceof Error ? cause.message : "暂时无法生成改造方案",
      );
    } finally {
      setPlanning(false);
    }
  };

  const applyDraft = async () => {
    if (!draft) return;
    setApplying(true);
    try {
      if (!draft.plan.id) throw new Error("改造方案缺少版本标识");
      const receipt = draft.preview.requires_confirmation
        ? await customizationApi.confirmPlan(draft.plan.id)
        : null;
      const result = await customizationApi.applyPlan(
        draft.plan.id,
        receipt?.token,
      );
      if (result.status !== "committed") {
        throw new Error("健康检查未通过，澜台已经自动回滚");
      }
      notifyCustomizationChanged();
      setDraft(null);
      setIntent("");
      await refresh();
      messageApi.success("改造已应用，可在版本历史中恢复");
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "应用失败");
    } finally {
      setApplying(false);
    }
  };

  const saveProfileName = async () => {
    if (!activeProfile || !profileName.trim()) return;
    try {
      const updated = await customizationApi.updateProfile(activeProfile.id, {
        name: profileName.trim(),
      });
      const slot = snapshot.slots.find(
        (candidate) => candidate.profile_id === activeProfile.id,
      );
      if (slot) {
        await customizationApi.setSlot(slot.index, {
          profile_id: activeProfile.id,
          label: updated.name,
          icon: updated.icon,
          color: updated.color,
        });
      }
      await refresh();
      messageApi.success("配置档名称已更新");
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "重命名失败");
    }
  };

  const undoLast = async () => {
    if (!activeProfile) return;
    try {
      const result = await customizationApi.undoLast(activeProfile.id);
      if (result.status !== "committed") throw new Error("撤销健康检查失败");
      notifyCustomizationChanged();
      await refresh();
      messageApi.success("已撤销上一版改造");
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "撤销失败");
    }
  };

  const restoreRevision = async (revision: number) => {
    if (!activeProfile) return;
    try {
      const result = await customizationApi.restoreRevision(
        activeProfile.id,
        revision,
      );
      if (result.status !== "committed") throw new Error("恢复健康检查失败");
      notifyCustomizationChanged();
      await refresh();
      messageApi.success(`已从版本 ${revision} 创建新的恢复版本`);
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "恢复失败");
    }
  };

  const exportActiveProfile = async () => {
    if (!activeProfile) return;
    try {
      const template = await customizationApi.exportProfile(activeProfile.id);
      const blob = new Blob([JSON.stringify(template, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${activeProfile.name.replace(/[\\/:*?"<>|]/g, "-")}.lantai-profile.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      messageApi.success("已导出无密钥模板");
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "导出失败");
    }
  };

  const importProfile = async () => {
    try {
      const payload = JSON.parse(importText) as Record<string, unknown>;
      const result = await customizationApi.importProfile(payload);
      const emptySlot = snapshot.slots.find((slot) => !slot.profile_id);
      if (emptySlot) {
        await customizationApi.setSlot(emptySlot.index, {
          profile_id: result.profile.id,
          label: result.profile.name,
          icon: result.profile.icon,
          color: result.profile.color,
        });
      }
      setImportOpen(false);
      setImportText("");
      await refresh();
      messageApi.success(
        `模板已导入但未启用；接受 ${result.report.accepted.length} 项配置`,
      );
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "模板格式无效");
    }
  };

  const copyProfile = async (profile: CustomizationProfile) => {
    try {
      const copied = await customizationApi.copyProfile(profile.id);
      const emptySlot = snapshot.slots.find((slot) => !slot.profile_id);
      if (emptySlot) {
        await customizationApi.setSlot(emptySlot.index, {
          profile_id: copied.id,
          label: copied.name,
          icon: copied.icon,
          color: copied.color,
        });
      }
      await refresh();
      messageApi.success(`已复制“${profile.name}”`);
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "复制失败");
    }
  };

  const toggleSlotBinding = async (profile: CustomizationProfile) => {
    const bound = snapshot.slots.find((slot) => slot.profile_id === profile.id);
    const target = bound || snapshot.slots.find((slot) => !slot.profile_id);
    if (!target) {
      messageApi.warning("五个快捷槽都已占用，请先取消一个绑定");
      return;
    }
    await customizationApi.setSlot(target.index, {
      profile_id: bound ? null : profile.id,
      label: bound ? null : profile.name,
      icon: bound ? null : profile.icon,
      color: bound ? null : profile.color,
    });
    await refresh();
  };

  const deleteProfile = async (profile: CustomizationProfile) => {
    try {
      await customizationApi.deleteProfile(profile.id);
      await refresh();
      messageApi.success(`已删除“${profile.name}”的个性化配置`);
    } catch (cause) {
      messageApi.error(cause instanceof Error ? cause.message : "删除失败");
    }
  };

  const buildAdvancedPreview = async () => {
    const entries = Object.entries(advancedValues);
    if (entries.length === 0 && advancedResets.length === 0) return;
    try {
      const target = await ensureTargetProfile();
      const setOperations = entries.map(([path, rawValue]) => {
        const item = snapshot.registry.find(
          (candidate) => candidate.id === path,
        );
        if (!item) throw new Error(`未知改造项：${path}`);
        let value = rawValue;
        if (item.value_type === "object") {
          value =
            typeof rawValue === "string" ? JSON.parse(rawValue) : rawValue;
        } else if (item.value_type === "string_array") {
          value = Array.isArray(rawValue)
            ? rawValue
            : String(rawValue)
                .split(/[，,\n]/)
                .map((part) => part.trim())
                .filter(Boolean);
        }
        return { op: "set" as const, path, value };
      });
      const resetOperations = advancedResets.map((path) => ({
        op: "unset" as const,
        path,
      }));
      const operations = [...setOperations, ...resetOperations];
      const preview = await customizationApi.previewPlan({
        target_profile_id: target.id,
        base_revision: target.revision,
        intent: `精细设置：调整 ${operations.length} 项`,
        operations,
      });
      setDraft({
        summary: `调整 ${operations.length} 项澜台设置`,
        plan: preview.plan,
        preview,
        modelLabel: "精细设置",
      });
      setAdvancedValues({});
      setAdvancedResets([]);
      await refresh();
    } catch (cause) {
      messageApi.error(
        cause instanceof Error ? cause.message : "高级配置格式无效",
      );
    }
  };

  const advancedControl = (item: CustomizationRegistryItem) => {
    const presentation = getFieldPresentation(item);
    const isReset = advancedResets.includes(item.id);
    const effectiveValue =
      snapshot.effective.values[item.id] ?? item.factory_value;
    const current =
      (isReset ? item.factory_value : advancedValues[item.id]) ??
      effectiveValue;
    const commit = (value: unknown) => {
      setAdvancedResets((existing) => existing.filter((id) => id !== item.id));
      setAdvancedValues((existing) => {
        const next = { ...existing };
        if (valuesEqual(value, effectiveValue)) delete next[item.id];
        else next[item.id] = value;
        return next;
      });
    };
    if (item.value_type === "boolean") {
      return (
        <div className={styles.switchControl}>
          <Switch
            checked={Boolean(current)}
            onChange={commit}
            aria-label={item.title}
          />
          <span>{booleanStateLabel(item, Boolean(current))}</span>
        </div>
      );
    }
    if (item.enum?.length) {
      return (
        <div className={styles.controlStack}>
          <Select
            value={current}
            aria-label={item.title}
            options={item.enum.map((value) => ({
              value,
              label: getOptionLabel(item, value),
            }))}
            onChange={commit}
          />
          {getOptionDescription(item, current) ? (
            <span>{getOptionDescription(item, current)}</span>
          ) : null}
        </div>
      );
    }
    if (item.value_type === "number") {
      const numericValue = Number(current);
      const minimum = item.minimum ?? 0;
      const maximum = item.maximum ?? 100;
      return (
        <div className={styles.numberControl}>
          <div>
            <strong>{getNumberMeaning(item, numericValue)}</strong>
            <span>{formatCustomizationValue(item, numericValue)}</span>
          </div>
          <div>
            <Slider
              value={numericValue}
              min={minimum}
              max={maximum}
              step={maximum === 1 ? 0.05 : 1}
              tooltip={{
                formatter: (value) =>
                  value === undefined
                    ? null
                    : formatCustomizationValue(item, value),
              }}
              onChange={commit}
              aria-label={item.title}
            />
            <InputNumber
              value={numericValue}
              min={minimum}
              max={maximum}
              step={maximum === 1 ? 0.05 : 1}
              controls={false}
              aria-label={`${item.title}精确数值`}
              onChange={(value) => value !== null && commit(value)}
            />
          </div>
        </div>
      );
    }
    if (item.value_type === "object") {
      const text =
        typeof current === "string"
          ? current
          : JSON.stringify(current, null, 2);
      let jsonError = "";
      try {
        JSON.parse(text);
      } catch {
        jsonError = "JSON 格式不完整，请检查括号、引号和逗号。";
      }
      return (
        <div className={styles.controlStack}>
          <Input.TextArea
            value={text}
            status={jsonError ? "error" : undefined}
            aria-label={item.title}
            aria-invalid={Boolean(jsonError)}
            placeholder={presentation.placeholder}
            autoSize={{ minRows: 3, maxRows: 8 }}
            onChange={(event) => commit(event.target.value)}
          />
          <span className={jsonError ? styles.fieldError : undefined}>
            {jsonError || "仅接受 JSON 格式；预览通过后才能应用。"}
          </span>
        </div>
      );
    }
    if (item.value_type === "string_array") {
      return (
        <div className={styles.controlStack}>
          <Input.TextArea
            value={
              Array.isArray(current)
                ? current.join("\n")
                : String(current ?? "")
            }
            aria-label={item.title}
            placeholder={presentation.placeholder}
            autoSize={{ minRows: 2, maxRows: 6 }}
            onChange={(event) => commit(event.target.value)}
          />
          <span>每行一项；使用前会检查它们是否真实存在。</span>
        </div>
      );
    }
    return (
      <Input
        value={
          Array.isArray(current) ? current.join(", ") : String(current ?? "")
        }
        aria-label={item.title}
        onChange={(event) => commit(event.target.value)}
        placeholder={presentation.placeholder}
      />
    );
  };

  const queueFactoryValue = (item: CustomizationRegistryItem) => {
    setAdvancedValues((existing) => {
      const next = { ...existing };
      delete next[item.id];
      return next;
    });
    if (
      Object.prototype.hasOwnProperty.call(
        activeProfile?.overrides || {},
        item.id,
      )
    ) {
      setAdvancedResets((existing) =>
        existing.includes(item.id) ? existing : [...existing, item.id],
      );
    }
  };

  const cancelFieldChange = (itemId: string) => {
    setAdvancedValues((existing) => {
      const next = { ...existing };
      delete next[itemId];
      return next;
    });
    setAdvancedResets((existing) => existing.filter((id) => id !== itemId));
  };

  const clearAdvancedChanges = () => {
    setAdvancedValues({});
    setAdvancedResets([]);
  };

  if (loading) {
    return (
      <section className={styles.studio} aria-label="澜台改造台加载中">
        <Skeleton active paragraph={{ rows: 4 }} />
      </section>
    );
  }

  return (
    <section
      className={styles.studio}
      aria-labelledby="customization-studio-title"
    >
      {contextHolder}
      <div className={styles.statusRail}>
        <div>
          <span className={styles.eyebrow}>当前配置</span>
          <h2 id="customization-studio-title">
            {snapshot.state.mode === "factory"
              ? "原生澜台"
              : activeProfile?.name || "原生澜台"}
          </h2>
          <p>
            {snapshot.state.mode === "safe"
              ? "安全模式 · 个性化和第三方扩展已隔离"
              : snapshot.state.mode === "factory"
                ? "官方只读基线 · 你的配置档仍保留在本机"
                : `版本 ${activeProfile?.revision ?? 0} · 所有修改均可追溯`}
          </p>
        </div>
        <div className={styles.statusActions}>
          <span className={styles.healthState}>
            <ShieldCheck size={15} aria-hidden="true" />
            恢复就绪
          </span>
          {snapshot.state.mode === "factory" &&
          snapshot.state.active_profile_id ? (
            <Button onClick={resumeActiveProfile}>恢复刚才的配置档</Button>
          ) : null}
          <Button icon={<RotateCcw size={15} />} onClick={returnToFactory}>
            回到原生澜台
          </Button>
          <Button
            icon={<FileUp size={15} />}
            onClick={() => setImportOpen(true)}
          >
            导入模板
          </Button>
          <Button
            icon={<Library size={15} />}
            onClick={() => setLibraryOpen(true)}
          >
            配置档库
          </Button>
        </div>
      </div>

      {activeProfile ? (
        <div className={styles.profileManager}>
          <div className={styles.profileIdentity}>
            {editingName ? (
              <Input
                value={profileName}
                maxLength={80}
                autoFocus
                onChange={(event) => setProfileName(event.target.value)}
                onPressEnter={() => void saveProfileName()}
                onBlur={() => void saveProfileName()}
                aria-label="配置档名称"
              />
            ) : (
              <button type="button" onClick={() => setEditingName(true)}>
                <Pencil size={14} />
                {activeProfile.name}
              </button>
            )}
            <span>版本 {activeProfile.revision}</span>
          </div>
          <div className={styles.profileTools}>
            <Button
              size="small"
              icon={<Undo2 size={14} />}
              disabled={activeProfile.revision === 0}
              onClick={() => void undoLast()}
            >
              撤销上一版
            </Button>
            <Button
              size="small"
              icon={<Download size={14} />}
              onClick={() => void exportActiveProfile()}
            >
              导出模板
            </Button>
          </div>
        </div>
      ) : null}

      {activeProfile && revisions.length > 0 ? (
        <div className={styles.revisionRail} aria-label="配置档版本历史">
          <span>
            <History size={14} />
            版本历史
          </span>
          {revisions.slice(0, 6).map((revision) => (
            <button
              type="button"
              key={revision.revision}
              title={`${revision.intent || revision.source} · ${revision.created_at}`}
              onClick={() => void restoreRevision(revision.revision)}
            >
              版本 {revision.revision} · {revision.intent || revision.source}
            </button>
          ))}
          <button type="button" onClick={() => void restoreRevision(0)}>
            r0 · 初始状态
          </button>
        </div>
      ) : null}

      {error ? (
        <Alert
          type="warning"
          showIcon
          message="个性化控制面暂时不可用"
          description={`${error}。澜台会继续使用原生配置。`}
          action={<Button onClick={() => void refresh()}>重试</Button>}
        />
      ) : null}

      <div className={styles.intentStudio}>
        <div className={styles.intentHeading}>
          <span className={styles.intentIcon} aria-hidden="true">
            <Sparkles size={18} />
          </span>
          <div>
            <strong>用一句话改造澜台</strong>
            <p>模型只生成结构化变更方案；应用前会展示差异和风险。</p>
          </div>
        </div>
        <div className={styles.intentComposer}>
          <Input.TextArea
            aria-label="描述你想怎样改造澜台"
            value={intent}
            onChange={(event) => setIntent(event.target.value)}
            placeholder="例如：换成温暖的颜色，平时安静一点，左侧隐藏记忆和技能。"
            autoSize={{ minRows: 2, maxRows: 5 }}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void generatePlan();
              }
            }}
          />
          <Button
            type="primary"
            loading={planning}
            disabled={!intent.trim()}
            onClick={() => void generatePlan()}
          >
            生成方案
          </Button>
        </div>
      </div>

      <div className={styles.advancedStudio}>
        <button
          type="button"
          aria-expanded={advancedOpen}
          aria-controls="customization-precision-settings"
          onClick={() => setAdvancedOpen((value) => !value)}
        >
          <span>
            <span className={styles.advancedIcon} aria-hidden="true">
              <SlidersHorizontal size={17} />
            </span>
            <span>
              <strong>精细设置</strong>
              <small>按项目调整外观、对话、记忆和交付方式</small>
            </span>
          </span>
          <span>{advancedOpen ? "收起" : "按项调整"}</span>
        </button>
        {advancedOpen ? (
          <div
            id="customization-precision-settings"
            className={styles.registryEditor}
          >
            <div className={styles.registryWelcome}>
              <div>
                <strong>选择你想要的结果</strong>
                <p>
                  这里的改动不会立即生效。先预览影响，确认后再应用；每一版都可撤销。
                </p>
              </div>
              <Segmented
                value={advancedMode}
                aria-label="设置复杂度"
                options={[
                  { value: "common", label: "常用设置" },
                  { value: "expert", label: "专家设置" },
                ]}
                onChange={(value) =>
                  setAdvancedMode(value as "common" | "expert")
                }
              />
            </div>

            {advancedMode === "expert" ? (
              <div className={styles.expertNotice} role="note">
                <ShieldAlert size={17} aria-hidden="true" />
                <span>
                  <strong>专家设置会影响模型路由、工具权限和底层外观。</strong>
                  格式、权限和启动健康检查不通过时，澜台会阻止应用或自动回滚。
                </span>
              </div>
            ) : null}

            <div className={styles.registryToolbar}>
              <Input
                allowClear
                value={advancedSearch}
                prefix={<Search size={15} aria-hidden="true" />}
                placeholder={
                  advancedMode === "expert"
                    ? "搜索设置名称或技术 ID"
                    : "搜索设置"
                }
                aria-label="搜索精细设置"
                onChange={(event) => setAdvancedSearch(event.target.value)}
              />
              <Checkbox
                checked={customizedOnly}
                onChange={(event) => setCustomizedOnly(event.target.checked)}
              >
                只看已自定义
              </Checkbox>
            </div>

            {registryGroups.length > 0 ? (
              registryGroups.map(([category, items]) => (
                <details
                  key={`${advancedMode}-${category}-${advancedSearch}-${customizedOnly}`}
                  open={
                    Boolean(advancedSearch) ||
                    customizedOnly ||
                    expandedCategories.includes(category)
                  }
                  onToggle={(event) => {
                    if (advancedSearch || customizedOnly) return;
                    const isOpen = event.currentTarget.open;
                    setExpandedCategories((existing) =>
                      isOpen
                        ? Array.from(new Set([...existing, category]))
                        : existing.filter((value) => value !== category),
                    );
                  }}
                >
                  <summary>
                    <span>{categoryLabels[category] || category}</span>
                    <span>
                      {items.length} 项
                      <ChevronDown size={15} aria-hidden="true" />
                    </span>
                  </summary>
                  <div className={styles.registryRows}>
                    {items.map((item) => {
                      const presentation = getFieldPresentation(item);
                      const isCustomized = Object.prototype.hasOwnProperty.call(
                        activeProfile?.overrides || {},
                        item.id,
                      );
                      const isPending =
                        Object.prototype.hasOwnProperty.call(
                          advancedValues,
                          item.id,
                        ) || advancedResets.includes(item.id);
                      const currentValue = advancedResets.includes(item.id)
                        ? item.factory_value
                        : (advancedValues[item.id] ??
                          snapshot.effective.values[item.id] ??
                          item.factory_value);
                      return (
                        <div
                          key={item.id}
                          className={`${styles.registryRow} ${isPending ? styles.registryRowPending : ""}`}
                        >
                          <div className={styles.registryMeta}>
                            <div className={styles.registryTitleLine}>
                              <strong>{item.title}</strong>
                              {isPending ? (
                                <Tag color="processing">待预览</Tag>
                              ) : null}
                              {!isPending && isCustomized ? (
                                <Tag>已自定义</Tag>
                              ) : null}
                              {item.risk === "medium" ? (
                                <Tooltip title="这项修改应用前需要你再确认一次">
                                  <Tag color="gold">需确认</Tag>
                                </Tooltip>
                              ) : null}
                              {item.risk === "high" ? (
                                <Tooltip title="这项可能改变工具或文件访问范围，应用前必须确认">
                                  <Tag color="red">高风险</Tag>
                                </Tooltip>
                              ) : null}
                            </div>
                            <p>{presentation.description}</p>
                            <span className={styles.currentValue}>
                              当前：
                              {formatCustomizationValue(item, currentValue)}
                              {isCustomized
                                ? " · 来自当前配置档"
                                : " · 沿用原生值"}
                            </span>
                            {advancedMode === "expert" ? (
                              <code>{item.id}</code>
                            ) : null}
                          </div>
                          <div className={styles.registryControl}>
                            {advancedControl(item)}
                            <div className={styles.fieldActions}>
                              {isPending ? (
                                <Button
                                  type="link"
                                  size="small"
                                  onClick={() => cancelFieldChange(item.id)}
                                >
                                  放弃这项修改
                                </Button>
                              ) : null}
                              {isCustomized &&
                              !advancedResets.includes(item.id) ? (
                                <Button
                                  type="link"
                                  size="small"
                                  icon={<RotateCcw size={13} />}
                                  onClick={() => queueFactoryValue(item)}
                                >
                                  恢复原生值
                                </Button>
                              ) : null}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </details>
              ))
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  customizedOnly
                    ? "当前范围还没有自定义项"
                    : "没有找到匹配的设置"
                }
              >
                <Button
                  onClick={() => {
                    setAdvancedSearch("");
                    setCustomizedOnly(false);
                  }}
                >
                  显示全部设置
                </Button>
              </Empty>
            )}
            <div className={styles.registryFooter}>
              <span aria-live="polite">
                {advancedInvalidFields > 0
                  ? `${advancedInvalidFields} 项内容格式需要修正，修正后才能预览`
                  : advancedChangeCount > 0
                    ? `${advancedChangeCount} 项修改等待预览；此时还没有改变澜台`
                    : "调整控件后，先预览影响，再决定是否应用"}
              </span>
              <Button
                disabled={advancedChangeCount === 0}
                onClick={clearAdvancedChanges}
              >
                放弃待预览修改
              </Button>
              <Button
                type="primary"
                disabled={
                  advancedChangeCount === 0 || advancedInvalidFields > 0
                }
                onClick={() => void buildAdvancedPreview()}
              >
                预览{advancedChangeCount > 0 ? ` ${advancedChangeCount} ` : ""}
                项修改
              </Button>
            </div>
          </div>
        ) : null}
      </div>

      <div className={styles.profileStrip} aria-label="快捷配置档">
        <button
          type="button"
          className={`${styles.profileCard} ${
            snapshot.state.mode === "factory" ? styles.profileCardActive : ""
          }`}
          onClick={returnToFactory}
        >
          <span className={styles.factoryMark}>澜</span>
          <strong>原生澜台</strong>
          <small>只读 · 永远可用</small>
          {snapshot.state.mode === "factory" ? <Check size={15} /> : null}
        </button>

        {snapshot.slots.map((slot) => {
          const profile = snapshot.profiles.find(
            (candidate) => candidate.id === slot.profile_id,
          );
          const active =
            snapshot.state.mode === "normal" &&
            snapshot.state.active_profile_id === profile?.id;
          if (!profile) {
            return (
              <button
                type="button"
                key={slot.index}
                className={`${styles.profileCard} ${styles.profileCardEmpty}`}
                onClick={() => void createProfileForSlot(slot)}
              >
                <span className={styles.slotIndex}>0{slot.index}</span>
                <strong>新配置档</strong>
                <small>点击创建</small>
              </button>
            );
          }
          return (
            <Tooltip key={slot.index} title={`版本 ${profile.revision}`}>
              <button
                type="button"
                className={`${styles.profileCard} ${
                  active ? styles.profileCardActive : ""
                }`}
                onClick={() => void switchProfile(profile.id)}
                style={{ "--profile-color": profile.color } as CSSProperties}
              >
                <span className={styles.profileMark} aria-hidden="true">
                  {profile.name.slice(0, 1)}
                </span>
                <strong>{slot.label || profile.name}</strong>
                <small>版本 {profile.revision}</small>
                {active ? <Check size={15} /> : null}
              </button>
            </Tooltip>
          );
        })}
      </div>

      {draft ? (
        <article className={styles.planPanel} aria-label="改造方案预览">
          <header>
            <div>
              <span className={styles.eyebrow}>应用前预览</span>
              <h3>{draft.summary}</h3>
            </div>
            <Tag
              color={draft.preview.risk === "low" ? "green" : "gold"}
              bordered={false}
            >
              {riskLabel(draft.preview.risk)}
            </Tag>
          </header>
          <div className={styles.changeList}>
            {Object.entries(draft.preview.changed).map(([path, values]) => (
              <div className={styles.changeRow} key={path}>
                <code>{path}</code>
                <span>{readableValue(values.before)}</span>
                <i aria-hidden="true">→</i>
                <strong>{readableValue(values.after)}</strong>
              </div>
            ))}
          </div>
          <footer>
            <span>
              <Clock3 size={14} aria-hidden="true" />由 {draft.modelLabel}{" "}
              生成；应用前会自动建立快照
            </span>
            <div>
              <Button onClick={() => setDraft(null)}>修改需求</Button>
              <Button
                type="primary"
                loading={applying}
                onClick={() => void applyDraft()}
              >
                应用这项改造
              </Button>
            </div>
          </footer>
        </article>
      ) : snapshot.profiles.length === 0 ? (
        <div className={styles.emptyHistory}>
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有自定义配置档。使用一句话改造，或点击任一空槽开始。"
          />
        </div>
      ) : (
        <div className={styles.historyHint}>
          <History size={15} aria-hidden="true" />
          每次成功改造都会生成只读版本记录；恢复历史会创建新版本，不覆盖原记录。
        </div>
      )}

      <Modal
        title="导入澜台配置模板"
        open={importOpen}
        okText="验证并导入"
        cancelText="取消"
        onOk={() => void importProfile()}
        onCancel={() => setImportOpen(false)}
        okButtonProps={{ disabled: !importText.trim() }}
      >
        <Alert
          type="info"
          showIcon
          message="导入后不会自动启用"
          description="未知字段、密钥、本机路径和权限引用会被忽略；你可以先检查再切换。"
        />
        <Input.TextArea
          className={styles.importEditor}
          value={importText}
          onChange={(event) => setImportText(event.target.value)}
          autoSize={{ minRows: 8, maxRows: 16 }}
          placeholder="粘贴 .lantai-profile.json 的内容"
          aria-label="澜台配置模板 JSON"
        />
      </Modal>
      <Modal
        title="配置档库"
        open={libraryOpen}
        footer={null}
        onCancel={() => setLibraryOpen(false)}
      >
        <div className={styles.libraryList}>
          {snapshot.profiles.map((profile) => {
            const slot = snapshot.slots.find(
              (candidate) => candidate.profile_id === profile.id,
            );
            const selected = snapshot.state.active_profile_id === profile.id;
            const active = snapshot.state.mode === "normal" && selected;
            return (
              <div className={styles.libraryRow} key={profile.id}>
                <span
                  className={styles.libraryMark}
                  style={{ "--profile-color": profile.color } as CSSProperties}
                >
                  {profile.name.slice(0, 1)}
                </span>
                <div>
                  <strong>{profile.name}</strong>
                  <small>
                    版本 {profile.revision}
                    {slot ? ` · 快捷槽 ${slot.index}` : " · 未固定"}
                    {active ? " · 当前使用" : selected ? " · 最近使用" : ""}
                  </small>
                </div>
                <div className={styles.libraryActions}>
                  <Button
                    size="small"
                    onClick={() => void switchProfile(profile.id)}
                  >
                    使用
                  </Button>
                  <Button
                    size="small"
                    onClick={() => void toggleSlotBinding(profile)}
                  >
                    {slot ? "取消固定" : "固定到槽"}
                  </Button>
                  <Button
                    size="small"
                    icon={<Copy size={13} />}
                    onClick={() => void copyProfile(profile)}
                  >
                    复制
                  </Button>
                  <Popconfirm
                    title="删除这个配置档？"
                    description="只删除个性化配置，不删除聊天、记忆或文件。"
                    disabled={selected}
                    onConfirm={() => void deleteProfile(profile)}
                  >
                    <Button
                      size="small"
                      danger
                      disabled={selected}
                      icon={<Trash2 size={13} />}
                    />
                  </Popconfirm>
                </div>
              </div>
            );
          })}
          {snapshot.profiles.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="暂无配置档"
            />
          ) : null}
        </div>
      </Modal>
    </section>
  );
}
