import {
  Drawer,
  Form,
  Input,
  InputNumber,
  Switch,
  Button,
  Select,
} from "@/vendor/lantaiDesign";
import { useAppMessage } from "../../../../hooks/useAppMessage";
import { Alert, ConfigProvider, Spin } from "antd";
import { LinkOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import type { FormInstance } from "antd";
import { useCallback, useEffect } from "react";
import { getChannelLabel, type ChannelKey } from "./constants";
import {
  CHANNELS_WITH_ACCESS_CONTROL,
  TWILIO_CONSOLE_URL,
  getDefaultMediaDir,
} from "./channelDrawerSchema";
import {
  AccessControlFields,
  BasicChannelFields,
  CustomExtraFields,
} from "./ChannelDrawerSections";
import { useChannelQrcode } from "./useChannelQrcode";
import styles from "../index.module.less";
import { useTheme } from "../../../../contexts/ThemeContext";
import { useAgentStore } from "../../../../stores/agentStore";
import type { ChannelDescriptor } from "../../../../api/types";

interface ChannelDrawerProps {
  open: boolean;
  activeKey: ChannelKey | null;
  activeLabel: string;
  form: FormInstance<Record<string, unknown>>;
  saving: boolean;
  initialValues: Record<string, unknown> | undefined;
  isBuiltin: boolean;
  descriptor?: ChannelDescriptor;
  onClose: () => void;
  onSubmit: (values: Record<string, unknown>) => void;
  onDirtyChange?: (dirty: boolean) => void;
}

export function ChannelDrawer({
  open,
  activeKey,
  activeLabel,
  form,
  saving,
  initialValues,
  isBuiltin,
  descriptor,
  onClose,
  onSubmit,
  onDirtyChange,
}: ChannelDrawerProps) {
  const { t } = useTranslation();
  const { isDark } = useTheme();
  const { selectedAgent, agents } = useAgentStore();
  const currentAgent = agents.find((a) => a.id === selectedAgent);
  const defaultMediaDir = getDefaultMediaDir(currentAgent?.workspace_dir);
  const label = activeKey ? getChannelLabel(activeKey, t) : activeLabel;
  const { message } = useAppMessage();

  // WeChat QR code hook
  const wechatQrcode = useChannelQrcode({
    channel: "wechat",
    successStatus: "confirmed",
    successCredentialKey: "bot_token",
    pollInterval: 2000,
    onSuccess: useCallback(
      (credentials: Record<string, string>) => {
        form.setFieldsValue({ bot_token: credentials.bot_token });
        message.success(t("channels.wechatLoginSuccess"));
      },
      [form, message, t],
    ),
    onError: useCallback(
      (type: "fetch" | "expired") => {
        if (type === "expired") {
          message.warning(t("channels.wechatQrcodeExpired"));
        } else {
          message.error(t("channels.wechatQrcodeFailed"));
        }
      },
      [message, t],
    ),
  });

  // DingTalk QR code hook
  const dingtalkQrcode = useChannelQrcode({
    channel: "dingtalk",
    successStatus: "success",
    successCredentialKey: "client_id",
    pollInterval: 5000,
    onSuccess: useCallback(
      (credentials: Record<string, string>) => {
        form.setFieldsValue({
          client_id: credentials.client_id,
          client_secret: credentials.client_secret,
        });
        message.success(t("channels.dingtalkAuthSuccess"));
      },
      [form, message, t],
    ),
    onExpired: useCallback(() => {
      message.warning(t("channels.dingtalkQrcodeExpired"));
    }, [message, t]),
    onError: useCallback(
      (type: "fetch" | "expired") => {
        if (type === "expired") {
          message.warning(t("channels.dingtalkQrcodeExpired"));
        } else {
          message.error(t("channels.dingtalkQrcodeFailed"));
        }
      },
      [message, t],
    ),
  });

  // WeCom QR code hook
  const wecomQrcode = useChannelQrcode({
    channel: "wecom",
    successStatus: "success",
    successCredentialKey: "bot_id",
    pollInterval: 3000,
    onSuccess: useCallback(
      (credentials: Record<string, string>) => {
        form.setFieldsValue({
          bot_id: credentials.bot_id,
          secret: credentials.secret,
        });
        message.success(t("channels.wecomAuthSuccess"));
      },
      [form, message, t],
    ),
    onError: useCallback(
      (_type: "fetch" | "expired") => {
        message.error(t("channels.wecomQrcodeFailed"));
      },
      [message, t],
    ),
  });

  useEffect(() => {
    wechatQrcode.reset();
    dingtalkQrcode.reset();
    wecomQrcode.reset();
  }, [
    activeKey,
    open,
    wechatQrcode.reset,
    dingtalkQrcode.reset,
    wecomQrcode.reset,
  ]);

  // ── Builtin channel-specific fields ─────────────────────────────────────

  const renderBuiltinExtraFields = (key: ChannelKey) => {
    switch (key) {
      case "matrix":
        return (
          <>
            <Form.Item
              name="homeserver"
              label="Homeserver URL"
              rules={[{ required: true }]}
            >
              <Input placeholder="https://matrix.org" />
            </Form.Item>
            <Form.Item
              name="user_id"
              label="User ID"
              rules={[{ required: true }]}
            >
              <Input placeholder="@bot:matrix.org" />
            </Form.Item>
            <Form.Item
              name="access_token"
              label="Access Token"
              rules={[{ required: true }]}
            >
              <Input.Password placeholder="syt_..." />
            </Form.Item>
          </>
        );

      case "imessage":
        return (
          <>
            <Form.Item
              name="db_path"
              label="DB Path"
              rules={[{ required: true, message: "Please input DB path" }]}
            >
              <Input placeholder="~/Library/Messages/chat.db" />
            </Form.Item>
            <Form.Item
              name="poll_sec"
              label="Poll Interval (sec)"
              rules={[
                { required: true, message: "Please input poll interval" },
              ]}
            >
              <InputNumber min={0.1} step={0.1} style={{ width: "100%" }} />
            </Form.Item>
          </>
        );

      case "discord":
        return (
          <>
            <Form.Item
              name="bot_token"
              label="Bot Token"
              rules={[{ required: true }]}
            >
              <Input.Password placeholder="Discord bot token" />
            </Form.Item>
            <Form.Item name="http_proxy" label="HTTP Proxy">
              <Input placeholder="http://127.0.0.1:18118" />
            </Form.Item>
            <Form.Item name="http_proxy_auth" label="HTTP Proxy Auth">
              <Input placeholder="user:password" />
            </Form.Item>
            <Form.Item
              name="accept_bot_messages"
              label={t("channels.acceptBotMessages")}
              valuePropName="checked"
              tooltip={t("channels.acceptBotMessagesTooltip")}
            >
              <Switch />
            </Form.Item>
          </>
        );

      case "slack":
        return (
          <>
            <ConfigProvider prefixCls="ant">
              <Alert
                type="info"
                showIcon
                message="使用 Slack Socket Mode，无需公网回调地址。请为应用开启 Socket Mode、消息事件和所需文件权限。"
                style={{ marginBottom: 16 }}
              />
            </ConfigProvider>
            <Form.Item
              name="bot_token"
              label="Bot Token"
              rules={[{ required: true }]}
            >
              <Input.Password placeholder="xoxb-..." />
            </Form.Item>
            <Form.Item
              name="app_token"
              label="App Token"
              rules={[{ required: true }]}
            >
              <Input.Password placeholder="xapp-..." />
            </Form.Item>
            <Form.Item name="proxy" label="HTTP / WebSocket Proxy">
              <Input placeholder="http://127.0.0.1:7890" />
            </Form.Item>
            <Form.Item name="media_dir" label="媒体目录">
              <Input placeholder={defaultMediaDir} />
            </Form.Item>
          </>
        );

      case "dingtalk":
        return (
          <>
            <ConfigProvider prefixCls="ant">
              <Alert
                type="info"
                showIcon
                message={t("channels.dingtalkSetupGuide")}
                style={{ marginBottom: 16 }}
              />
            </ConfigProvider>
            <Form.Item label={t("channels.dingtalkScanAuth")}>
              <Button
                type="primary"
                block
                loading={dingtalkQrcode.loading}
                onClick={dingtalkQrcode.fetchQrcode}
              >
                {t("channels.dingtalkGetQrcode")}
              </Button>
              {dingtalkQrcode.loading && (
                <div style={{ textAlign: "center", marginTop: 12 }}>
                  <Spin />
                </div>
              )}
              {dingtalkQrcode.qrcodeImg && !dingtalkQrcode.loading && (
                <div style={{ textAlign: "center", marginTop: 12 }}>
                  <img
                    src={`data:image/png;base64,${dingtalkQrcode.qrcodeImg}`}
                    alt="DingTalk QR Code"
                    style={{ width: 200, height: 200 }}
                  />
                  <div
                    style={{
                      marginTop: 8,
                      fontSize: 12,
                      color: isDark
                        ? "rgba(255,255,255,0.45)"
                        : "rgba(0,0,0,0.45)",
                    }}
                  >
                    {t("channels.dingtalkScanHint")}
                  </div>
                </div>
              )}
            </Form.Item>
            <Form.Item
              name="client_id"
              label="Client ID"
              rules={[{ required: true }]}
            >
              <Input placeholder="dingxxxxx" />
            </Form.Item>
            <Form.Item
              name="client_secret"
              label="Client Secret"
              rules={[{ required: true }]}
            >
              <Input.Password />
            </Form.Item>
            <Form.Item
              name="message_type"
              label="Message Type"
              tooltip="markdown: regular messages; card: AI interactive card"
            >
              <Select
                options={[
                  { label: "markdown", value: "markdown" },
                  { label: "card", value: "card" },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="cron_message_type"
              label="Cron Message Type"
              tooltip="Message type for cron/scheduled task sends. Independent from the chat message type above."
            >
              <Select
                options={[
                  { label: "markdown", value: "markdown" },
                  { label: "card", value: "card" },
                ]}
              />
            </Form.Item>
            <Form.Item
              noStyle
              shouldUpdate={(prev, cur) =>
                prev.message_type !== cur.message_type ||
                prev.cron_message_type !== cur.cron_message_type
              }
            >
              {({ getFieldValue }) => {
                const needsCard =
                  getFieldValue("message_type") === "card" ||
                  getFieldValue("cron_message_type") === "card";
                if (!needsCard) return null;
                return (
                  <>
                    <Form.Item
                      name="card_template_id"
                      label="Card Template ID"
                      rules={[
                        {
                          required: true,
                          message:
                            "Please input card template id when message_type=card",
                        },
                      ]}
                    >
                      <Input placeholder="dt_card_template_xxx" />
                    </Form.Item>
                    <Form.Item
                      name="card_template_key"
                      label="Card Template Key"
                      tooltip="Must exactly match the template variable name"
                    >
                      <Input placeholder="content" />
                    </Form.Item>
                    <Form.Item
                      name="robot_code"
                      label="Robot Code"
                      tooltip="Recommended to configure explicitly for group chats"
                    >
                      <Input placeholder="robot code (default client_id)" />
                    </Form.Item>
                  </>
                );
              }}
            </Form.Item>
            <Form.Item
              name="at_sender_on_reply"
              label={t("channels.atSenderOnReply")}
              tooltip={t("channels.atSenderOnReplyTooltip")}
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
          </>
        );

      case "feishu":
        return (
          <>
            <Form.Item
              name="domain"
              label={t("channels.feishuRegion")}
              initialValue="feishu"
              tooltip={t("channels.feishuRegionTooltip")}
            >
              <Select>
                <Select.Option value="feishu">
                  {t("channels.feishuChina")}
                </Select.Option>
                <Select.Option value="lark">
                  {t("channels.feishuInternational")}
                </Select.Option>
              </Select>
            </Form.Item>
            <Form.Item
              name="app_id"
              label="App ID"
              rules={[{ required: true }]}
            >
              <Input placeholder="cli_xxx" />
            </Form.Item>
            <Form.Item
              name="app_secret"
              label="App Secret"
              rules={[{ required: true }]}
            >
              <Input.Password placeholder="App Secret" />
            </Form.Item>
            <Form.Item name="encrypt_key" label="Encrypt Key">
              <Input placeholder="Optional, for event encryption" />
            </Form.Item>
            <Form.Item name="verification_token" label="Verification Token">
              <Input placeholder="Optional" />
            </Form.Item>
            <Form.Item name="media_dir" label={t("channels.wechatMediaDir")}>
              <Input placeholder={defaultMediaDir} />
            </Form.Item>
          </>
        );

      case "qq":
        return (
          <>
            <Form.Item
              name="app_id"
              label="App ID"
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="client_secret"
              label="Client Secret"
              rules={[{ required: true }]}
            >
              <Input.Password />
            </Form.Item>
            <Form.Item
              name="ack_message"
              label={t("channels.ackMessage")}
              tooltip={t("channels.ackMessageTooltip")}
            >
              <Input placeholder={t("channels.ackMessagePlaceholder")} />
            </Form.Item>
          </>
        );

      case "telegram":
        return (
          <>
            <Form.Item
              name="bot_token"
              label="Bot Token"
              rules={[{ required: true }]}
            >
              <Input.Password placeholder="Telegram bot token from BotFather" />
            </Form.Item>
            <Form.Item name="http_proxy" label="HTTP Proxy">
              <Input placeholder="http://127.0.0.1:18118" />
            </Form.Item>
            <Form.Item name="http_proxy_auth" label="HTTP Proxy Auth">
              <Input placeholder="user:password" />
            </Form.Item>
            <Form.Item
              name="show_typing"
              label="Show Typing"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
          </>
        );

      case "mqtt":
        return (
          <>
            <Form.Item
              name="host"
              label="MQTT Host"
              rules={[{ required: true }]}
            >
              <Input placeholder="127.0.0.1" />
            </Form.Item>
            <Form.Item
              name="port"
              label="MQTT Port"
              rules={[
                { required: true },
                {
                  type: "number",
                  min: 1,
                  max: 65535,
                  message: "Port must be between 1 and 65535",
                },
              ]}
            >
              <InputNumber
                min={1}
                max={65535}
                style={{ width: "100%" }}
                placeholder="1883"
              />
            </Form.Item>
            <Form.Item
              name="transport"
              label="Transport"
              initialValue="tcp"
              rules={[{ required: true }]}
            >
              <Select>
                <Select.Option value="tcp">MQTT (tcp)</Select.Option>
                <Select.Option value="websockets">
                  WS (websockets)
                </Select.Option>
              </Select>
            </Form.Item>
            <Form.Item
              name="clean_session"
              label="Clean Session"
              valuePropName="checked"
            >
              <Switch defaultChecked />
            </Form.Item>
            <Form.Item
              name="qos"
              label="QoS"
              initialValue="2"
              rules={[{ required: true }]}
            >
              <Select>
                <Select.Option value="0">At Most Once (0)</Select.Option>
                <Select.Option value="1">At Least Once (1)</Select.Option>
                <Select.Option value="2">Exactly Once (2)</Select.Option>
              </Select>
            </Form.Item>
            <Form.Item name="username" label="MQTT Username">
              <Input placeholder="Leave blank to disable / not use" />
            </Form.Item>
            <Form.Item name="password" label="MQTT Password">
              <Input.Password placeholder="Leave blank to disable / not use" />
            </Form.Item>
            <Form.Item
              name="subscribe_topic"
              label="Subscribe Topic"
              rules={[{ required: true }]}
            >
              <Input placeholder="server/+/up" />
            </Form.Item>
            <Form.Item
              name="publish_topic"
              label="Publish Topic"
              rules={[{ required: true }]}
            >
              <Input placeholder="client/{client_id}/down" />
            </Form.Item>
            <Form.Item
              name="tls_enabled"
              label="TLS Enabled"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
            <Form.Item name="tls_ca_certs" label="TLS CA Certs">
              <Input placeholder="Path to CA certificates file" />
            </Form.Item>
            <Form.Item name="tls_certfile" label="TLS Certfile">
              <Input placeholder="Path to client certificate file" />
            </Form.Item>
            <Form.Item name="tls_keyfile" label="TLS Keyfile">
              <Input placeholder="Path to client private key file" />
            </Form.Item>
          </>
        );

      case "mattermost":
        return (
          <>
            <Form.Item
              name="url"
              label="Mattermost URL"
              rules={[{ required: true }]}
            >
              <Input placeholder="https://mattermost.example.com" />
            </Form.Item>
            <Form.Item
              name="bot_token"
              label="Bot Token"
              rules={[{ required: true }]}
            >
              <Input.Password placeholder="Mattermost bot token" />
            </Form.Item>
            <Form.Item name="media_dir" label={t("channels.wechatMediaDir")}>
              <Input placeholder={defaultMediaDir} />
            </Form.Item>
            <Form.Item
              name="show_typing"
              label="Show Typing"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
            <Form.Item
              name="thread_follow_without_mention"
              label="Thread Follow Without Mention"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
          </>
        );

      case "voice":
        return (
          <>
            <ConfigProvider prefixCls="ant">
              <Alert
                type="info"
                showIcon
                message={t("channels.voiceSetupGuide")}
                style={{ marginBottom: 16 }}
              />
            </ConfigProvider>
            <Form.Item
              name="twilio_account_sid"
              label={t("channels.twilioAccountSid")}
              rules={[{ required: true }]}
            >
              <Input placeholder="ACxxxxxxxx" />
            </Form.Item>
            <Form.Item
              name="twilio_auth_token"
              label={t("channels.twilioAuthToken")}
              rules={[{ required: true }]}
            >
              <Input.Password />
            </Form.Item>
            <Form.Item name="phone_number" label={t("channels.phoneNumber")}>
              <Input placeholder="+15551234567" />
            </Form.Item>
            <Form.Item
              name="phone_number_sid"
              label={t("channels.phoneNumberSid")}
              tooltip={t("channels.phoneNumberSidHelp")}
            >
              <Input placeholder="PNxxxxxxxx" />
            </Form.Item>
            <Form.Item name="tts_provider" label={t("channels.ttsProvider")}>
              <Input placeholder="google" />
            </Form.Item>
            <Form.Item name="tts_voice" label={t("channels.ttsVoice")}>
              <Input placeholder="en-US-Journey-D" />
            </Form.Item>
            <Form.Item name="stt_provider" label={t("channels.sttProvider")}>
              <Input placeholder="deepgram" />
            </Form.Item>
            <Form.Item name="language" label={t("channels.language")}>
              <Input placeholder="en-US" />
            </Form.Item>
            <Form.Item
              name="welcome_greeting"
              label={t("channels.welcomeGreeting")}
            >
              <Input.TextArea rows={2} />
            </Form.Item>
          </>
        );

      case "sip":
        return (
          <>
            <ConfigProvider prefixCls="ant">
              <Alert
                type="info"
                showIcon
                message={t("channels.sipSetupGuide")}
                style={{ marginBottom: 16 }}
              />
            </ConfigProvider>
            <Form.Item
              name="sip_mode"
              label={t("channels.sipMode")}
              tooltip={t("channels.sipModeTooltip")}
              initialValue="dev"
            >
              <Select
                options={[
                  { value: "dev", label: "Dev (pyVoIP)" },
                  { value: "livekit", label: "Production (LiveKit)" },
                ]}
              />
            </Form.Item>
            <Form.Item
              shouldUpdate={(
                prev: Record<string, unknown>,
                cur: Record<string, unknown>,
              ) => prev.sip_mode !== cur.sip_mode}
              noStyle
            >
              {({
                getFieldValue,
              }: {
                getFieldValue: (name: string) => unknown;
              }) => (
                <Form.Item name="sip_server" label={t("channels.sipServer")}>
                  <Input
                    placeholder={
                      getFieldValue("sip_mode") === "livekit"
                        ? t("channels.sipServerPlaceholderLivekit")
                        : t("channels.sipServerPlaceholder")
                    }
                  />
                </Form.Item>
              )}
            </Form.Item>
            <Form.Item name="sip_username" label={t("channels.sipUsername")}>
              <Input placeholder="1001" />
            </Form.Item>
            <Form.Item name="sip_password" label={t("channels.sipPassword")}>
              <Input.Password />
            </Form.Item>
            <Form.Item
              name="sip_port"
              label={t("channels.sipPort")}
              rules={[
                {
                  type: "number",
                  min: 1,
                  max: 65535,
                },
              ]}
            >
              <InputNumber
                min={1}
                max={65535}
                style={{ width: "100%" }}
                placeholder="5061"
              />
            </Form.Item>
            <Form.Item
              name="sip_transport"
              label={t("channels.sipTransport")}
              initialValue="UDP"
            >
              <Select
                options={[
                  { value: "UDP", label: "UDP" },
                  { value: "TCP", label: "TCP" },
                  { value: "TLS", label: "TLS" },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="dashscope_api_key"
              label={t("channels.sipDashscopeApiKey")}
              tooltip={t("channels.sipDashscopeApiKeyTooltip")}
            >
              <Input.Password placeholder="sk-..." />
            </Form.Item>
            <Form.Item name="tts_provider" label={t("channels.ttsProvider")}>
              <Input placeholder="aliyun" />
            </Form.Item>
            <Form.Item name="tts_voice" label={t("channels.ttsVoice")}>
              <Input placeholder="longxiaochun" />
            </Form.Item>
            <Form.Item name="stt_provider" label={t("channels.sttProvider")}>
              <Input placeholder="aliyun" />
            </Form.Item>
            <Form.Item name="language" label={t("channels.language")}>
              <Input placeholder="zh-CN" />
            </Form.Item>
            <Form.Item
              name="welcome_greeting"
              label={t("channels.welcomeGreeting")}
            >
              <Input.TextArea rows={2} />
            </Form.Item>
            <Form.Item
              noStyle
              shouldUpdate={(prev, cur) => prev.sip_mode !== cur.sip_mode}
            >
              {({ getFieldValue }) => {
                if (getFieldValue("sip_mode") !== "livekit") return null;
                return (
                  <>
                    <Form.Item
                      name="livekit_url"
                      label={t("channels.livekitUrl")}
                      rules={[{ required: true }]}
                    >
                      <Input placeholder="ws://localhost:7880" />
                    </Form.Item>
                    <Form.Item
                      name="livekit_api_key"
                      label={t("channels.livekitApiKey")}
                      rules={[{ required: true }]}
                    >
                      <Input />
                    </Form.Item>
                    <Form.Item
                      name="livekit_api_secret"
                      label={t("channels.livekitApiSecret")}
                      rules={[{ required: true }]}
                    >
                      <Input.Password />
                    </Form.Item>
                    <Form.Item
                      name="livekit_sip_trunk_id"
                      label={t("channels.livekitSipTrunkId")}
                    >
                      <Input placeholder="ST_xxxx" />
                    </Form.Item>
                    <Form.Item
                      name="livekit_room_name"
                      label={t("channels.livekitRoomName")}
                      tooltip={t("channels.livekitRoomNameTooltip")}
                    >
                      <Input placeholder="sip-inbound" />
                    </Form.Item>
                  </>
                );
              }}
            </Form.Item>
          </>
        );

      case "wecom":
        return (
          <>
            <ConfigProvider prefixCls="ant">
              <Alert
                type="warning"
                showIcon
                message={t("channels.wecomSetupGuide")}
                style={{ marginBottom: 16 }}
              />
            </ConfigProvider>
            <Form.Item label={t("channels.wecomScanAuth")}>
              <Button
                type="primary"
                block
                loading={wecomQrcode.loading}
                onClick={wecomQrcode.fetchQrcode}
              >
                {t("channels.loginWeCom")}
              </Button>
              {wecomQrcode.loading && (
                <div style={{ textAlign: "center", marginTop: 12 }}>
                  <Spin />
                </div>
              )}
              {wecomQrcode.qrcodeImg && !wecomQrcode.loading && (
                <div style={{ textAlign: "center", marginTop: 12 }}>
                  <img
                    src={`data:image/png;base64,${wecomQrcode.qrcodeImg}`}
                    alt="WeCom QR Code"
                    style={{ width: 200, height: 200 }}
                  />
                  <div
                    style={{
                      marginTop: 8,
                      fontSize: 12,
                      color: isDark
                        ? "rgba(255,255,255,0.45)"
                        : "rgba(0,0,0,0.45)",
                    }}
                  >
                    {t("channels.wecomAuthHint")}
                  </div>
                </div>
              )}
            </Form.Item>
            <Form.Item
              name="bot_id"
              label="Bot ID"
              rules={[{ required: true, message: "Please input Bot ID" }]}
            >
              <Input placeholder="Bot ID from WeCom backend" />
            </Form.Item>
            <Form.Item
              name="secret"
              label="Secret"
              rules={[{ required: true, message: "Please input Secret" }]}
            >
              <Input.Password placeholder="Secret from WeCom backend" />
            </Form.Item>
            <Form.Item name="media_dir" label={t("channels.wechatMediaDir")}>
              <Input placeholder={defaultMediaDir} />
            </Form.Item>
            <Form.Item
              name="welcome_text"
              label={t("channels.welcomeText")}
              tooltip={t("channels.welcomeTextTooltip")}
            >
              <Input placeholder={t("channels.welcomeTextPlaceholder")} />
            </Form.Item>
            <Form.Item
              name="share_session_in_group"
              label={t("channels.onebotShareSessionInGroup")}
              valuePropName="checked"
              tooltip={t("channels.onebotShareSessionInGroupTooltip")}
            >
              <Switch />
            </Form.Item>
          </>
        );

      case "xiaoyi":
        return (
          <>
            <ConfigProvider prefixCls="ant">
              <Alert
                type="info"
                showIcon
                message={t("channels.xiaoyiSetupGuide")}
                style={{ marginBottom: 16 }}
              />
            </ConfigProvider>
            <Form.Item
              name="ak"
              label="Access Key (AK)"
              rules={[{ required: true, message: "Please input Access Key" }]}
            >
              <Input placeholder="Access Key from Huawei Developer Platform" />
            </Form.Item>
            <Form.Item
              name="sk"
              label="Secret Key (SK)"
              rules={[{ required: true, message: "Please input Secret Key" }]}
            >
              <Input.Password placeholder="Secret Key from Huawei Developer Platform" />
            </Form.Item>
            <Form.Item
              name="agent_id"
              label="Agent ID"
              rules={[{ required: true, message: "Please input Agent ID" }]}
            >
              <Input placeholder="Agent ID from XiaoYi platform" />
            </Form.Item>
            <Form.Item name="ws_url" label="WebSocket URL">
              <Input placeholder="wss://hag.cloud.huawei.com/openclaw/v1/ws/link" />
            </Form.Item>
          </>
        );

      case "wechat":
        return (
          <>
            <ConfigProvider prefixCls="ant">
              <Alert
                type="info"
                showIcon
                message={t("channels.wechatSetupGuide")}
                style={{ marginBottom: 16 }}
              />
              <Alert
                type="warning"
                showIcon
                message={t("channels.wechatContextTokenLimit")}
                style={{ marginBottom: 16 }}
              />
            </ConfigProvider>
            <Form.Item label={t("channels.wechatScanLogin")}>
              <Button
                type="primary"
                block
                loading={wechatQrcode.loading}
                onClick={wechatQrcode.fetchQrcode}
              >
                {t("channels.wechatGetQrcode")}
              </Button>
              {wechatQrcode.loading && (
                <div style={{ textAlign: "center", marginTop: 12 }}>
                  <Spin />
                </div>
              )}
              {wechatQrcode.qrcodeImg && !wechatQrcode.loading && (
                <div style={{ textAlign: "center", marginTop: 12 }}>
                  <img
                    src={`data:image/png;base64,${wechatQrcode.qrcodeImg}`}
                    alt="WeChat QR Code"
                    style={{ width: 200, height: 200 }}
                  />
                  <div
                    style={{
                      marginTop: 8,
                      fontSize: 12,
                      color: isDark
                        ? "rgba(255,255,255,0.45)"
                        : "rgba(0,0,0,0.45)",
                    }}
                  >
                    {t("channels.wechatScanHint")}
                  </div>
                </div>
              )}
            </Form.Item>
            <Form.Item
              name="bot_token"
              label={t("channels.wechatBotToken")}
              tooltip={t("channels.wechatBotTokenTooltip")}
            >
              <Input.Password
                placeholder={t("channels.wechatBotTokenPlaceholder")}
              />
            </Form.Item>
            <Form.Item
              name="bot_token_file"
              label={t("channels.wechatBotTokenFile")}
              tooltip={t("channels.wechatBotTokenFileTooltip")}
            >
              <Input placeholder="~/.lantai/wechat_bot_token" />
            </Form.Item>
            <Form.Item name="media_dir" label={t("channels.wechatMediaDir")}>
              <Input placeholder={defaultMediaDir} />
            </Form.Item>
            <Form.Item
              name="message_merge_enabled"
              label={t("channels.wechatMessageMerge")}
              valuePropName="checked"
              tooltip={t("channels.wechatMessageMergeTooltip")}
            >
              <Switch />
            </Form.Item>
            <Form.Item
              noStyle
              shouldUpdate={(prev, cur) =>
                prev.message_merge_enabled !== cur.message_merge_enabled
              }
            >
              {({ getFieldValue }) =>
                getFieldValue("message_merge_enabled") ? (
                  <Form.Item
                    name="message_merge_delay_ms"
                    label={t("channels.wechatMessageMergeDelayMs")}
                    tooltip={t("channels.wechatMessageMergeDelayMsTooltip")}
                    initialValue={0}
                    rules={[
                      {
                        validator: (_: unknown, value: unknown) => {
                          if (
                            value === null ||
                            value === undefined ||
                            value === ""
                          ) {
                            return Promise.resolve();
                          }
                          const num = Number(value);
                          if (!Number.isInteger(num) || num < 0) {
                            return Promise.reject(
                              new Error(
                                t(
                                  "channels.wechatMessageMergeDelayMsValidation",
                                ),
                              ),
                            );
                          }
                          return Promise.resolve();
                        },
                      },
                    ]}
                  >
                    <InputNumber
                      min={0}
                      step={100}
                      style={{ width: "100%" }}
                      placeholder="0"
                    />
                  </Form.Item>
                ) : null
              }
            </Form.Item>
          </>
        );

      case "onebot":
        return (
          <>
            <Form.Item
              name="ws_host"
              label="WebSocket Host"
              rules={[{ required: true }]}
            >
              <Input placeholder="0.0.0.0" />
            </Form.Item>
            <Form.Item
              name="ws_port"
              label="WebSocket Port"
              rules={[
                { required: true },
                {
                  type: "number",
                  min: 1,
                  max: 65535,
                  message: "Port must be between 1 and 65535",
                },
              ]}
            >
              <InputNumber
                min={1}
                max={65535}
                style={{ width: "100%" }}
                placeholder="6199"
              />
            </Form.Item>
            <Form.Item name="access_token" label="Access Token">
              <Input.Password placeholder="Access token for authentication" />
            </Form.Item>
            <Form.Item
              name="share_session_in_group"
              label={t("channels.onebotShareSessionInGroup")}
              valuePropName="checked"
              tooltip={t("channels.onebotShareSessionInGroupTooltip")}
            >
              <Switch />
            </Form.Item>
          </>
        );

      case "yuanbao":
        return (
          <>
            <ConfigProvider prefixCls="ant">
              <Alert
                type="info"
                showIcon
                message="腾讯元宝渠道通过签名令牌和 WebSocket 长连接接入，支持私聊、群聊及图片/音频/文件。"
                style={{ marginBottom: 16 }}
              />
            </ConfigProvider>
            <Form.Item
              name="app_id"
              label="App ID"
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="app_secret"
              label="App Secret"
              rules={[{ required: true }]}
            >
              <Input.Password />
            </Form.Item>
            <Form.Item name="api_domain" label="API Domain">
              <Input placeholder="bot.yuanbao.tencent.com" />
            </Form.Item>
            <Form.Item name="ws_url" label="WebSocket URL">
              <Input placeholder="留空使用官方端点" />
            </Form.Item>
            <Form.Item name="media_dir" label="媒体目录">
              <Input placeholder={defaultMediaDir} />
            </Form.Item>
            <Form.Item
              name="accept_bot_messages"
              label="接受其他机器人消息"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
          </>
        );

      default:
        return null;
    }
  };

  // ── Drawer title ─────────────────────────────────────────────────────────

  const drawerTitle = (
    <div className={styles.drawerTitle}>
      <span>
        {label
          ? `${label} ${t("channels.settings")}`
          : t("channels.channelSettings")}
      </span>
      {/* Official doc links removed for rebranding */}
      {activeKey === "voice" && (
        <Button
          type="text"
          size="small"
          icon={<LinkOutlined />}
          onClick={() =>
            window.open(TWILIO_CONSOLE_URL, "_blank", "noopener,noreferrer")
          }
          className={styles.dingtalkDocBtn}
          style={{ color: "#FF7F16" }}
        >
          {t("channels.voiceSetupLink")}
        </Button>
      )}
    </div>
  );

  // ── Render ───────────────────────────────────────────────────────────────

  const drawerFooter = (
    <div className={styles.formActions}>
      <Button onClick={onClose}>{t("common.cancel")}</Button>
      <Button type="primary" loading={saving} onClick={() => form.submit()}>
        {t("common.save")}
      </Button>
    </div>
  );

  return (
    <Drawer
      width={420}
      placement="right"
      title={drawerTitle}
      open={open}
      onClose={onClose}
      destroyOnClose
      footer={drawerFooter}
    >
      {activeKey && (
        <Form
          form={form}
          layout="vertical"
          initialValues={initialValues}
          onFinish={onSubmit}
          onValuesChange={() => onDirtyChange?.(true)}
        >
          <BasicChannelFields
            activeKey={activeKey}
            t={t}
            capabilities={descriptor?.capabilities}
          />

          {isBuiltin ? (
            renderBuiltinExtraFields(activeKey)
          ) : (
            <CustomExtraFields
              values={initialValues}
              fields={descriptor?.configFields}
            />
          )}

          {CHANNELS_WITH_ACCESS_CONTROL.includes(activeKey) && (
            <AccessControlFields t={t} />
          )}
        </Form>
      )}
    </Drawer>
  );
}
