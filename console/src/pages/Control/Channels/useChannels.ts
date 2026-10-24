import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import api from "../../../api";
import { useAgentStore } from "../../../stores/agentStore";
import { createLatestRequestGate } from "../../../utils/latestRequest";
import type { ChannelDescriptor } from "../../../api/types";

type ChannelMap = Record<string, Record<string, unknown>>;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function normalizeChannels(value: unknown): ChannelMap {
  if (!isPlainObject(value)) return {};

  return Object.fromEntries(
    Object.entries(value).filter(([, config]) => isPlainObject(config)),
  ) as ChannelMap;
}

function normalizeChannelTypes(value: unknown, channels: ChannelMap): string[] {
  const clean = (items: unknown[]) =>
    items
      .filter((item): item is string => typeof item === "string")
      .map((item) => item.trim())
      .filter(Boolean);

  if (Array.isArray(value)) {
    return clean(value);
  }

  if (isPlainObject(value)) {
    const possibleArrays = [
      value.types,
      value.channelTypes,
      value.channel_types,
      value.channels,
      value.data,
      value.items,
    ];
    for (const candidate of possibleArrays) {
      if (Array.isArray(candidate)) {
        return clean(candidate);
      }
    }
  }

  return Object.keys(channels);
}

export function useChannels() {
  const { selectedAgent } = useAgentStore();
  const [channels, setChannels] = useState<ChannelMap>({});
  const [channelTypes, setChannelTypes] = useState<string[]>([]);
  const [descriptors, setDescriptors] = useState<
    Record<string, ChannelDescriptor>
  >({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const channelsLoadGateRef = useRef(createLatestRequestGate());

  const fetchChannels = useCallback(async () => {
    const ownsState = channelsLoadGateRef.current.begin();
    setLoading(true);
    setError("");
    try {
      const [data, types, descriptorData] = await Promise.all([
        api.listChannels(),
        api.listChannelTypes(),
        api.listChannelDescriptors(),
      ]);
      if (!ownsState()) return;
      const normalizedChannels = normalizeChannels(data);
      setChannels(normalizedChannels);
      setChannelTypes(normalizeChannelTypes(types, normalizedChannels));
      setDescriptors(
        Object.fromEntries(
          (Array.isArray(descriptorData) ? descriptorData : []).map((item) => [
            item.key,
            item,
          ]),
        ),
      );
    } catch (error) {
      if (!ownsState()) return;
      console.error("❌ Failed to load channels:", error);
      setChannels({});
      setChannelTypes([]);
      setDescriptors({});
      setError(
        "渠道配置加载失败。请检查连接后重试；当前不会把它当作空配置保存。 ",
      );
    } finally {
      if (ownsState()) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setChannels({});
    setChannelTypes([]);
    setDescriptors({});
    void fetchChannels();
    return () => channelsLoadGateRef.current.invalidate();
  }, [fetchChannels, selectedAgent]);

  // Built-in channels come first (in a fixed order), then custom channels
  const builtinOrder = useMemo(
    () => [
      "console",
      "dingtalk",
      "feishu",
      "wecom",
      "wechat",
      "telegram",
      "onebot",
      "qq",
      "imessage",
      "discord",
      "slack",
      "matrix",
      "mattermost",
      "mqtt",
      "voice",
      "sip",
      "xiaoyi",
      "yuanbao",
    ],
    [],
  );

  const orderedKeys = useMemo(
    () => [
      ...builtinOrder.filter((k) => channelTypes.includes(k)),
      ...channelTypes.filter((k) => !builtinOrder.includes(k)),
    ],
    [builtinOrder, channelTypes],
  );

  // Read isBuiltin from API response
  const isBuiltin = useCallback(
    (key: string) => Boolean(channels[key]?.isBuiltin),
    [channels],
  );

  return {
    channels,
    descriptors,
    channelTypes,
    orderedKeys,
    isBuiltin,
    loading,
    error,
    fetchChannels,
  };
}
