import { useEffect, useMemo, useRef, useState } from "react";
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import { GripVertical, Plus, Save, Trash2 } from "lucide-react";
import {
  Button,
  Card,
  InputNumber,
  Select,
  Switch,
  Tooltip,
} from "@/vendor/lantaiDesign";
import { videoGenerationApi } from "@/api";
import type {
  ModelSlotConfig,
  ProviderInfo,
  VideoGenerationConfig,
} from "@/api/types";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "@/hooks/useAppMessage";
import { isProductVisibleProviderId } from "@/utils/providerVisibility";
import styles from "../../index.module.less";
import { isTextToVideoModel } from "../../modelProtocol";

interface VideoGenerationSectionProps {
  providers: ProviderInfo[];
}

interface SortableModelRowProps {
  id: string;
  index: number;
  label: string;
  disabled: boolean;
  onRemove: () => void;
}

const DEFAULT_CONFIG: VideoGenerationConfig = {
  enabled: false,
  models: [],
  timeout_seconds: 600,
};

function slotId(slot: ModelSlotConfig): string {
  return slot.provider_id + "\u0000" + slot.model;
}

function SortableModelRow({
  id,
  index,
  label,
  disabled,
  onRemove,
}: SortableModelRowProps) {
  const { t } = useTranslation();
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id, disabled });

  return (
    <div
      ref={setNodeRef}
      className={[
        styles.imageModelRow,
        isDragging ? styles.imageModelRowDragging : "",
      ]
        .filter(Boolean)
        .join(" ")}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
      }}
    >
      <Tooltip title={t("models.videoGenerationReorder")}>
        <button
          type="button"
          className={styles.imageModelDragHandle}
          aria-label={t("models.videoGenerationReorder")}
          disabled={disabled}
          {...attributes}
          {...listeners}
        >
          <GripVertical size={16} />
        </button>
      </Tooltip>
      <span className={styles.imageModelRank}>{index + 1}</span>
      <span className={styles.imageModelLabel}>{label}</span>
      <Tooltip title={t("models.videoGenerationRemove")}>
        <Button
          type="text"
          className={styles.imageModelRemove}
          aria-label={t("models.videoGenerationRemove")}
          icon={<Trash2 size={16} />}
          disabled={disabled}
          onClick={onRemove}
        />
      </Tooltip>
    </div>
  );
}

export function VideoGenerationSection({
  providers,
}: VideoGenerationSectionProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [config, setConfig] = useState<VideoGenerationConfig>(DEFAULT_CONFIG);
  const [providerId, setProviderId] = useState("");
  const [modelId, setModelId] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  const eligibleProviders = useMemo(
    () =>
      providers.filter((provider) => {
        if (!isProductVisibleProviderId(provider.id)) return false;
        if (provider.supports_video_generation !== true) return false;
        const hasModels = [
          ...(provider.models ?? []),
          ...(provider.extra_models ?? []),
        ].some(isTextToVideoModel);
        if (!hasModels || !provider.base_url) return false;
        return provider.require_api_key === false || Boolean(provider.api_key);
      }),
    [providers],
  );

  useEffect(() => {
    let active = true;
    void videoGenerationApi
      .getVideoGenerationConfig()
      .then((value) => {
        if (!active) return;
        setConfig(value);
        setLoaded(true);
      })
      .catch(() => {
        if (active) setLoaded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  const selectedProvider = eligibleProviders.find(
    (provider) => provider.id === providerId,
  );
  const selectedModels = [
    ...(selectedProvider?.models ?? []),
    ...(selectedProvider?.extra_models ?? []),
  ].filter(isTextToVideoModel);
  const providerById = new Map(
    eligibleProviders.map((provider) => [provider.id, provider]),
  );
  const rowLabel = (slot: ModelSlotConfig) => {
    const provider = providerById.get(slot.provider_id);
    const model = [
      ...(provider?.models ?? []),
      ...(provider?.extra_models ?? []),
    ].find((item) => item.id === slot.model);
    return (
      (provider?.name ?? slot.provider_id) + " / " + (model?.name ?? slot.model)
    );
  };
  const canAdd = !saving && Boolean(providerId && modelId);
  const canSave =
    loaded && !saving && dirty && (!config.enabled || config.models.length > 0);

  const update = (next: VideoGenerationConfig) => {
    if (savingRef.current) return;
    setConfig(next);
    setDirty(true);
  };

  const addModel = () => {
    if (savingRef.current || !canAdd) return;
    const slot = { provider_id: providerId, model: modelId };
    if (config.models.some((item) => slotId(item) === slotId(slot))) {
      message.info(t("models.videoGenerationDuplicate"));
      return;
    }
    update({ ...config, models: [...config.models, slot] });
    setModelId("");
  };

  const removeModel = (id: string) => {
    if (savingRef.current) return;
    update({
      ...config,
      models: config.models.filter((slot) => slotId(slot) !== id),
    });
  };

  const reorderModels = ({ active, over }: DragEndEvent) => {
    if (savingRef.current) return;
    if (!over || active.id === over.id) return;
    const from = config.models.findIndex((slot) => slotId(slot) === active.id);
    const to = config.models.findIndex((slot) => slotId(slot) === over.id);
    if (from < 0 || to < 0) return;
    update({ ...config, models: arrayMove(config.models, from, to) });
  };

  const save = async () => {
    if (savingRef.current || !canSave) return;
    const snapshot: VideoGenerationConfig = {
      ...config,
      models: config.models.map((slot) => ({ ...slot })),
    };
    savingRef.current = true;
    setSaving(true);
    try {
      const saved =
        await videoGenerationApi.updateVideoGenerationConfig(snapshot);
      setConfig(saved);
      setDirty(false);
      message.success(t("models.videoGenerationUpdated"));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("models.failedToSave"),
      );
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  return (
    <Card
      className={styles.slotSection}
      data-testid="video-generation-settings"
      title={t("models.videoGenerationTitle")}
      extra={
        <Switch
          aria-label={t("models.videoGenerationEnabled")}
          checked={config.enabled}
          disabled={!loaded || saving}
          onChange={(enabled) => update({ ...config, enabled })}
        />
      }
    >
      <p className={styles.imageGenerationIntro}>
        {t("models.videoGenerationDescription")}
      </p>

      <div className={styles.slotForm}>
        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.provider")}</label>
          <Select
            style={{ width: "100%" }}
            value={providerId || undefined}
            disabled={saving}
            placeholder={t("models.selectProvider")}
            options={eligibleProviders.map((provider) => ({
              value: provider.id,
              label: provider.name,
            }))}
            onChange={(value) => {
              setProviderId(value);
              setModelId("");
            }}
          />
        </div>
        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.model")}</label>
          <Select
            style={{ width: "100%" }}
            showSearch
            optionFilterProp="label"
            disabled={saving || !selectedProvider}
            value={modelId || undefined}
            placeholder={
              selectedProvider
                ? t("models.selectModel")
                : t("models.selectProvider")
            }
            options={selectedModels.map((model) => ({
              value: model.id,
              label: model.name + " (" + model.id + ")",
            }))}
            onChange={setModelId}
          />
        </div>
        <div className={styles.slotField + " " + styles.slotActionField}>
          <label
            className={styles.slotLabel + " " + styles.visuallyHiddenLabel}
          >
            {t("models.videoGenerationAdd")}
          </label>
          <Button
            icon={<Plus size={16} />}
            disabled={!canAdd}
            onClick={addModel}
          >
            {t("models.videoGenerationAdd")}
          </Button>
        </div>
      </div>

      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={reorderModels}
      >
        <SortableContext
          items={config.models.map(slotId)}
          strategy={verticalListSortingStrategy}
        >
          <div className={styles.imageModelList}>
            {config.models.length ? (
              config.models.map((slot, index) => (
                <SortableModelRow
                  key={slotId(slot)}
                  id={slotId(slot)}
                  index={index}
                  label={rowLabel(slot)}
                  disabled={saving}
                  onRemove={() => removeModel(slotId(slot))}
                />
              ))
            ) : (
              <div className={styles.imageModelEmpty}>
                {t("models.videoGenerationEmpty")}
              </div>
            )}
          </div>
        </SortableContext>
      </DndContext>

      <div className={styles.imageGenerationFooter}>
        <label className={styles.imageGenerationTimeout}>
          <span>{t("models.videoGenerationTimeout")}</span>
          <InputNumber
            min={30}
            max={1800}
            value={config.timeout_seconds}
            disabled={!loaded || saving}
            onChange={(value) =>
              update({
                ...config,
                timeout_seconds:
                  typeof value === "number"
                    ? value
                    : DEFAULT_CONFIG.timeout_seconds,
              })
            }
          />
        </label>
        <Button
          type="primary"
          icon={<Save size={16} />}
          loading={saving}
          disabled={!canSave}
          onClick={save}
        >
          {t("models.save")}
        </Button>
      </div>
      {config.enabled && config.models.length === 0 ? (
        <p className={styles.imageGenerationWarning}>
          {t("models.videoGenerationNeedsModel")}
        </p>
      ) : null}
    </Card>
  );
}
