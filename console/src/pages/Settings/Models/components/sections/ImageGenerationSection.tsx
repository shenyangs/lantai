import { useEffect, useMemo, useState } from "react";
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
import { imageGenerationApi } from "@/api";
import type {
  ImageGenerationConfig,
  ModelSlotConfig,
  ProviderInfo,
} from "@/api/types";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "@/hooks/useAppMessage";
import { isProductVisibleProviderId } from "@/utils/providerVisibility";
import styles from "../../index.module.less";
import { isImageGenerationModel } from "../../modelProtocol";

interface ImageGenerationSectionProps {
  providers: ProviderInfo[];
}

interface SortableModelRowProps {
  id: string;
  index: number;
  label: string;
  disabled: boolean;
  onRemove: () => void;
}

const DEFAULT_CONFIG: ImageGenerationConfig = {
  enabled: false,
  models: [],
  timeout_seconds: 120,
  works_cover_enabled: false,
  works_cover_model: { provider_id: "", model: "" },
};

function slotId(slot: ModelSlotConfig): string {
  return `${slot.provider_id}\u0000${slot.model}`;
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
      <Tooltip title={t("models.imageGenerationReorder")}>
        <button
          type="button"
          className={styles.imageModelDragHandle}
          aria-label={t("models.imageGenerationReorder")}
          disabled={disabled}
          {...attributes}
          {...listeners}
        >
          <GripVertical size={16} />
        </button>
      </Tooltip>
      <span className={styles.imageModelRank}>{index + 1}</span>
      <span className={styles.imageModelLabel}>{label}</span>
      <Tooltip title={t("models.imageGenerationRemove")}>
        <Button
          type="text"
          className={styles.imageModelRemove}
          aria-label={t("models.imageGenerationRemove")}
          icon={<Trash2 size={16} />}
          disabled={disabled}
          onClick={onRemove}
        />
      </Tooltip>
    </div>
  );
}

export function ImageGenerationSection({
  providers,
}: ImageGenerationSectionProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [config, setConfig] = useState<ImageGenerationConfig>(DEFAULT_CONFIG);
  const [providerId, setProviderId] = useState("");
  const [modelId, setModelId] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
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
        if (provider.chat_model !== "OpenAIChatModel") return false;
        const hasModels = [
          ...(provider.models ?? []),
          ...(provider.extra_models ?? []),
        ].some(isImageGenerationModel);
        if (!hasModels || !provider.base_url) return false;
        return provider.require_api_key === false || Boolean(provider.api_key);
      }),
    [providers],
  );

  useEffect(() => {
    let active = true;
    void imageGenerationApi
      .getImageGenerationConfig()
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
  ].filter(isImageGenerationModel);
  const providerById = new Map(
    eligibleProviders.map((provider) => [provider.id, provider]),
  );
  const rowLabel = (slot: ModelSlotConfig) => {
    const provider = providerById.get(slot.provider_id);
    const model = [
      ...(provider?.models ?? []),
      ...(provider?.extra_models ?? []),
    ].find((item) => item.id === slot.model);
    return `${provider?.name ?? slot.provider_id} / ${model?.name ?? slot.model}`;
  };
  const canAdd = Boolean(providerId && modelId);
  const canSave =
    loaded && dirty && (!config.enabled || config.models.length > 0);

  const update = (next: ImageGenerationConfig) => {
    setConfig(next);
    setDirty(true);
  };

  const addModel = () => {
    if (!canAdd) return;
    const slot = { provider_id: providerId, model: modelId };
    if (config.models.some((item) => slotId(item) === slotId(slot))) {
      message.info(t("models.imageGenerationDuplicate"));
      return;
    }
    update({ ...config, models: [...config.models, slot] });
    setModelId("");
  };

  const removeModel = (id: string) => {
    const removesWorksCoverModel = slotId(config.works_cover_model) === id;
    update({
      ...config,
      models: config.models.filter((slot) => slotId(slot) !== id),
      ...(removesWorksCoverModel
        ? {
            works_cover_enabled: false,
            works_cover_model: { provider_id: "", model: "" },
          }
        : {}),
    });
  };

  const reorderModels = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return;
    const from = config.models.findIndex((slot) => slotId(slot) === active.id);
    const to = config.models.findIndex((slot) => slotId(slot) === over.id);
    if (from < 0 || to < 0) return;
    update({ ...config, models: arrayMove(config.models, from, to) });
  };

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    try {
      const saved =
        await imageGenerationApi.updateImageGenerationConfig(config);
      setConfig(saved);
      setDirty(false);
      message.success(t("models.imageGenerationUpdated"));
    } catch (error) {
      message.error(
        error instanceof Error ? error.message : t("models.failedToSave"),
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      className={styles.slotSection}
      data-testid="image-generation-settings"
      title={t("models.imageGenerationTitle")}
      extra={
        <Switch
          aria-label={t("models.imageGenerationEnabled")}
          checked={config.enabled}
          disabled={!loaded}
          onChange={(enabled) => update({ ...config, enabled })}
        />
      }
    >
      <p className={styles.imageGenerationIntro}>
        {t("models.imageGenerationDescription")}
      </p>

      <div className={styles.slotForm}>
        <div className={styles.slotField}>
          <label className={styles.slotLabel}>{t("models.provider")}</label>
          <Select
            style={{ width: "100%" }}
            value={providerId || undefined}
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
            disabled={!selectedProvider}
            value={modelId || undefined}
            placeholder={
              selectedProvider
                ? t("models.selectModel")
                : t("models.selectProvider")
            }
            options={selectedModels.map((model) => ({
              value: model.id,
              label: `${model.name} (${model.id})`,
            }))}
            onChange={setModelId}
          />
        </div>
        <div className={`${styles.slotField} ${styles.slotActionField}`}>
          <label
            className={`${styles.slotLabel} ${styles.visuallyHiddenLabel}`}
          >
            {t("models.imageGenerationAdd")}
          </label>
          <Button
            icon={<Plus size={16} />}
            disabled={!canAdd}
            onClick={addModel}
          >
            {t("models.imageGenerationAdd")}
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
                {t("models.imageGenerationEmpty")}
              </div>
            )}
          </div>
        </SortableContext>
      </DndContext>

      <div className={styles.imageGenerationFooter}>
        <label className={styles.imageGenerationTimeout}>
          <span>{t("models.imageGenerationTimeout")}</span>
          <InputNumber
            min={5}
            max={600}
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
          {t("models.imageGenerationNeedsModel")}
        </p>
      ) : null}
    </Card>
  );
}
