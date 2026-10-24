import { useEffect, useState } from "react";
import { Form, Input, Modal } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import api from "../../../../../api";
import { useAppMessage } from "../../../../../hooks/useAppMessage";
import {
  normalizeOpenAICompatibleProvider,
  type OpenAICompatibleProviderValues,
} from "../../openAICompatibleProvider";

interface OpenAICompatibleProviderModalProps {
  open: boolean;
  onClose: () => void;
  onSaved: (providerId: string) => void | Promise<void>;
}

export function OpenAICompatibleProviderModal({
  open,
  onClose,
  onSaved,
}: OpenAICompatibleProviderModalProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [saving, setSaving] = useState(false);
  const [form] = Form.useForm<OpenAICompatibleProviderValues>();

  useEffect(() => {
    if (!open) return;
    form.resetFields();
    form.setFieldsValue({
      name: t("models.openAICompatibleDefaultName"),
    });
  }, [form, open, t]);

  const handleSubmit = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);

      const { request, modelId } =
        normalizeOpenAICompatibleProvider(values);
      const provider = await api.createCustomProvider(request);

      let discoveredModelIds = new Set<string>();
      try {
        const result = await api.discoverModels(provider.id, undefined, true);
        discoveredModelIds = new Set(
          (result.models || []).map((model) => model.id),
        );
      } catch {
        // Many OpenAI-compatible relays intentionally omit GET /models.
        // A manually supplied model ID still makes the provider usable.
      }

      if (modelId && !discoveredModelIds.has(modelId)) {
        await api.addModel(provider.id, { id: modelId, name: modelId });
        discoveredModelIds.add(modelId);
      }

      message.success(
        discoveredModelIds.size > 0
          ? t("models.openAICompatibleCreatedWithModels", {
              name: request.name,
              count: discoveredModelIds.size,
            })
          : t("models.openAICompatibleCreated", { name: request.name }),
      );
      await onSaved(provider.id);
      onClose();
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) return;
      const errorMessage =
        error instanceof Error
          ? error.message
          : t("models.providerCreateFailed");
      message.error(errorMessage);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={t("models.openAICompatibleTitle")}
      open={open}
      onCancel={onClose}
      onOk={handleSubmit}
      confirmLoading={saving}
      okText={t("models.openAICompatibleConnect")}
      cancelText={t("models.cancel")}
      destroyOnHidden
    >
      <p style={{ margin: "6px 0 18px", color: "var(--lt-text-subtle)" }}>
        {t("models.openAICompatibleDescription")}
      </p>
      <Form form={form} layout="vertical">
        <Form.Item
          name="name"
          label={t("models.providerNameLabel")}
          rules={[
            {
              required: true,
              whitespace: true,
              message: t("models.providerNameLabel"),
            },
          ]}
        >
          <Input
            autoFocus
            placeholder={t("models.openAICompatibleNamePlaceholder")}
          />
        </Form.Item>

        <Form.Item
          name="base_url"
          label={t("models.defaultBaseUrlLabel")}
          extra={t("models.openAICompatibleBaseUrlHint")}
          rules={[
            {
              required: true,
              whitespace: true,
              message: t("models.defaultBaseUrlLabel"),
            },
            {
              validator: async (_, value) => {
                if (!value) return;
                try {
                  const parsed = new URL(value.trim());
                  if (!["http:", "https:"].includes(parsed.protocol)) {
                    throw new Error("unsupported protocol");
                  }
                } catch {
                  throw new Error(t("models.invalidBaseUrl"));
                }
              },
            },
          ]}
        >
          <Input placeholder="https://gateway.example.com/v1" />
        </Form.Item>

        <Form.Item
          name="api_key"
          label={t("models.apiKey")}
          extra={t("models.openAICompatibleApiKeyHint")}
        >
          <Input.Password
            autoComplete="new-password"
            placeholder={t("models.openAICompatibleApiKeyPlaceholder")}
          />
        </Form.Item>

        <Form.Item
          name="model_id"
          label={t("models.openAICompatibleModelIdLabel")}
          extra={t("models.openAICompatibleModelIdHint")}
        >
          <Input placeholder="gpt-4.1-mini" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
