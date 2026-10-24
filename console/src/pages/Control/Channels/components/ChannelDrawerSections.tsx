import {
  Form,
  Input,
  InputNumber,
  Select,
  Switch,
} from "@/vendor/lantaiDesign";
import type { TFunction } from "i18next";
import type { ChannelKey } from "./constants";
import { getCustomExtraFieldKeys } from "./channelDrawerSchema";
import type {
  ChannelCapabilities,
  ChannelPluginConfigField,
} from "../../../../api/types";

interface BasicChannelFieldsProps {
  activeKey: ChannelKey;
  t: TFunction;
  capabilities?: ChannelCapabilities;
}

export function BasicChannelFields({
  activeKey,
  t,
  capabilities,
}: BasicChannelFieldsProps) {
  return (
    <>
      <Form.Item
        name="enabled"
        label={t("common.enabled")}
        valuePropName="checked"
      >
        <Switch />
      </Form.Item>

      {activeKey !== "voice" && (
        <Form.Item name="bot_prefix" label="触发前缀">
          <Input placeholder="@bot" />
        </Form.Item>
      )}

      {activeKey !== "console" && (
        <>
          <Form.Item
            name="filter_tool_messages"
            label={t("channels.filterToolMessages")}
            valuePropName="checked"
            tooltip={t("channels.filterToolMessagesTooltip")}
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="filter_thinking"
            label={t("channels.filterThinking")}
            valuePropName="checked"
            tooltip={t("channels.filterThinkingTooltip")}
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="show_thinking"
            label="展示思考过程"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="show_tool_calls"
            label="展示工具调用"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="show_tool_results"
            label="展示工具结果"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="no_text_debounce"
            label="合并连续媒体消息"
            valuePropName="checked"
            tooltip="等待同一会话中的文字与媒体组合后再交给 Agent"
          >
            <Switch />
          </Form.Item>
          {capabilities?.streaming && (
            <Form.Item
              name="streaming_enabled"
              label="渐进式输出"
              valuePropName="checked"
              tooltip="生成过程中持续更新同一条消息"
            >
              <Switch />
            </Form.Item>
          )}
        </>
      )}
    </>
  );
}

interface CustomExtraFieldsProps {
  values: Record<string, unknown> | undefined;
  fields?: ChannelPluginConfigField[];
}

function renderPluginField(field: ChannelPluginConfigField) {
  if (field.type === "boolean") return <Switch />;
  if (field.type === "number") {
    return <InputNumber style={{ width: "100%" }} />;
  }
  if (field.type === "select") {
    return <Select options={field.options || []} />;
  }
  if (field.type === "password" || field.secret) {
    return <Input.Password />;
  }
  return <Input />;
}

export function CustomExtraFields({ values, fields }: CustomExtraFieldsProps) {
  if (fields?.length) {
    return (
      <>
        <div style={{ marginBottom: 8, fontWeight: 500 }}>插件配置</div>
        {fields.map((field) => (
          <Form.Item
            key={field.name}
            name={field.name}
            label={field.label}
            tooltip={field.description}
            initialValue={field.default}
            rules={field.required ? [{ required: true }] : undefined}
          >
            {renderPluginField(field)}
          </Form.Item>
        ))}
      </>
    );
  }
  const extraKeys = getCustomExtraFieldKeys(values);
  if (extraKeys.length === 0) return null;

  return (
    <>
      <div style={{ marginBottom: 8, fontWeight: 500 }}>扩展字段</div>
      {extraKeys.map((fieldKey) => {
        const value = values?.[fieldKey];
        return (
          <Form.Item key={fieldKey} name={fieldKey} label={fieldKey}>
            {typeof value === "boolean" ? (
              <Switch />
            ) : typeof value === "number" ? (
              <InputNumber style={{ width: "100%" }} />
            ) : (
              <Input />
            )}
          </Form.Item>
        );
      })}
    </>
  );
}

interface AccessControlFieldsProps {
  t: TFunction;
}

export function AccessControlFields({ t }: AccessControlFieldsProps) {
  return (
    <>
      <Form.Item
        name="dm_policy"
        label={t("channels.dmPolicy")}
        tooltip={t("channels.dmPolicyTooltip")}
        initialValue="allowlist"
      >
        <Select
          options={[
            { value: "open", label: t("channels.policyOpen") },
            { value: "allowlist", label: t("channels.policyAllowlist") },
            { value: "disabled", label: "禁用私聊" },
          ]}
        />
      </Form.Item>
      <Form.Item
        name="group_policy"
        label={t("channels.groupPolicy")}
        tooltip={t("channels.groupPolicyTooltip")}
        initialValue="allowlist"
      >
        <Select
          options={[
            { value: "open", label: t("channels.policyOpen") },
            { value: "allowlist", label: t("channels.policyAllowlist") },
            { value: "disabled", label: "禁用群聊" },
          ]}
        />
      </Form.Item>
      <Form.Item
        name="require_mention"
        label={t("channels.requireMention")}
        valuePropName="checked"
        tooltip={t("channels.requireMentionTooltip")}
      >
        <Switch />
      </Form.Item>
      <Form.Item
        name="allow_from"
        label={t("channels.allowFrom")}
        tooltip={t("channels.allowFromTooltip")}
        initialValue={[]}
      >
        <Select
          mode="tags"
          placeholder={t("channels.allowFromPlaceholder")}
          tokenSeparators={[","]}
        />
      </Form.Item>
    </>
  );
}
