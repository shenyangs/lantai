import type { ChannelKey } from "./constants";

export const CHANNELS_WITH_ACCESS_CONTROL: ChannelKey[] = [
  "telegram",
  "dingtalk",
  "discord",
  "slack",
  "feishu",
  "wecom",
  "mattermost",
  "matrix",
  "wechat",
  "imessage",
  "onebot",
  "yuanbao",
];

export const TWILIO_CONSOLE_URL = "https://console.twilio.com";

export const BASE_CHANNEL_FORM_FIELDS = [
  "enabled",
  "bot_prefix",
  "filter_tool_messages",
  "filter_thinking",
  "show_thinking",
  "show_tool_calls",
  "show_tool_results",
  "tool_call_max_length",
  "tool_result_max_length",
  "no_text_debounce",
  "security_version",
  "dm_policy",
  "group_policy",
  "allow_from",
  "deny_message",
  "require_mention",
  "isBuiltin",
];

export function getDefaultMediaDir(workspaceDir: string | null | undefined) {
  return workspaceDir ? `${workspaceDir}/media` : "~/.lantai/media";
}

export function getCustomExtraFieldKeys(
  values: Record<string, unknown> | undefined,
) {
  if (!values) return [];
  return Object.keys(values).filter(
    (key) => !BASE_CHANNEL_FORM_FIELDS.includes(key),
  );
}
