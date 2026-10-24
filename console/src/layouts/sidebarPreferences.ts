export const SIDEBAR_VISIBILITY_STORAGE_KEY = "lantai:sidebar-visible-nav:v7";

const LEGACY_SIDEBAR_VISIBILITY_STORAGE_KEYS = [
  "lantai:sidebar-visible-nav:v6",
  "lantai:sidebar-visible-nav:v5",
  "lantai:sidebar-visible-nav:v4",
  "lantai:sidebar-visible-nav:v3",
  "lantai:sidebar-visible-nav:v2",
] as const;

export const SIDEBAR_NAV_PREFERENCES_EVENT =
  "lantai:sidebar-preferences-changed";

export interface SidebarNavPreferenceItem {
  key: string;
  label: string;
  group: "常用" | "控制" | "工作区" | "能力" | "系统";
  description: string;
  defaultVisible: boolean;
  pinned?: boolean;
}

export const SIDEBAR_NAV_ITEMS: SidebarNavPreferenceItem[] = [
  {
    key: "chat",
    label: "聊天",
    group: "常用",
    description: "进入对话、调用智能体和工具。",
    defaultVisible: true,
    pinned: true,
  },
  {
    key: "works",
    label: "作品",
    group: "常用",
    description: "收藏沈行知与你完成的小工具、小游戏和文档。",
    defaultVisible: true,
    pinned: true,
  },
  {
    key: "daily-brief",
    label: "简报",
    group: "常用",
    description: "查看每日简报、来源与趋势更新。",
    defaultVisible: true,
  },
  {
    key: "channels",
    label: "渠道",
    group: "常用",
    description: "连接外部会话入口并查看渠道最近对话。",
    defaultVisible: true,
  },
  {
    key: "reminders",
    label: "今日安排",
    group: "控制",
    description: "在统一时间线上查看提醒和定时执行。",
    defaultVisible: true,
  },
  {
    key: "cron-jobs",
    label: "定时任务",
    group: "控制",
    description: "创建和管理按时间触发的自动执行。",
    defaultVisible: true,
  },
  {
    key: "workspace",
    label: "知识库",
    group: "工作区",
    description: "添加和管理可在聊天中直接使用的知识与资料。",
    defaultVisible: true,
  },
  {
    key: "memory",
    label: "记忆",
    group: "工作区",
    description: "查看长期记忆、用户画像和偏好沉淀。",
    defaultVisible: true,
  },
  {
    key: "agents",
    label: "智能体",
    group: "能力",
    description: "管理不同角色、提示词和运行配置。",
    defaultVisible: true,
  },
  {
    key: "capabilities",
    label: "能力中心",
    group: "能力",
    description: "查看可组合能力、流水线和执行记录。",
    defaultVisible: false,
  },
  {
    key: "skills",
    label: "技能",
    group: "能力",
    description: "管理可复用技能和智能体扩展能力。",
    defaultVisible: true,
  },
  {
    key: "tools",
    label: "工具",
    group: "能力",
    description: "管理内置工具的启用状态和调用策略。",
    defaultVisible: true,
  },
  {
    key: "settings",
    label: "设置",
    group: "系统",
    description: "调整模型、权限、外观、连接和运行参数。",
    defaultVisible: true,
    pinned: true,
  },
];

const defaultVisibleKeys = new Set(
  SIDEBAR_NAV_ITEMS.filter((item) => item.defaultVisible).map(
    (item) => item.key,
  ),
);

const pinnedKeys = new Set(
  SIDEBAR_NAV_ITEMS.filter((item) => item.pinned).map((item) => item.key),
);

const knownKeys = new Set(SIDEBAR_NAV_ITEMS.map((item) => item.key));

function normalizeSidebarVisibleKeys(value: unknown) {
  if (!Array.isArray(value)) return new Set(defaultVisibleKeys);
  const result = new Set<string>();
  value.forEach((key) => {
    const normalized = String(key || "").trim();
    if (knownKeys.has(normalized)) result.add(normalized);
  });
  pinnedKeys.forEach((key) => result.add(key));
  return result.size ? result : new Set(defaultVisibleKeys);
}

export function loadSidebarVisibleKeys() {
  if (typeof window === "undefined") {
    return new Set(defaultVisibleKeys);
  }

  try {
    const raw = window.localStorage.getItem(SIDEBAR_VISIBILITY_STORAGE_KEY);
    if (raw) return normalizeSidebarVisibleKeys(JSON.parse(raw));

    for (const legacyKey of LEGACY_SIDEBAR_VISIBILITY_STORAGE_KEYS) {
      const legacyRaw = window.localStorage.getItem(legacyKey);
      if (!legacyRaw) continue;
      const migrated = normalizeSidebarVisibleKeys(JSON.parse(legacyRaw));
      // Daily Brief is a new, user-facing workbench. Surface it once for
      // existing installations while keeping an explicit v7 hide choice.
      migrated.add("daily-brief");
      window.localStorage.setItem(
        SIDEBAR_VISIBILITY_STORAGE_KEY,
        JSON.stringify(Array.from(migrated)),
      );
      return migrated;
    }

    return new Set(defaultVisibleKeys);
  } catch {
    return new Set(defaultVisibleKeys);
  }
}

export function saveSidebarVisibleKeys(keys: Iterable<string>) {
  const result = new Set<string>();
  for (const key of keys) {
    const normalized = String(key || "").trim();
    if (knownKeys.has(normalized)) result.add(normalized);
  }
  pinnedKeys.forEach((key) => result.add(key));

  try {
    window.localStorage.setItem(
      SIDEBAR_VISIBILITY_STORAGE_KEY,
      JSON.stringify(Array.from(result)),
    );
  } catch {
    // ignore storage failures in restricted browser contexts
  }

  return result;
}

export function resetSidebarVisibleKeys() {
  const result = new Set(defaultVisibleKeys);
  try {
    window.localStorage.removeItem(SIDEBAR_VISIBILITY_STORAGE_KEY);
  } catch {
    // ignore storage failures in restricted browser contexts
  }
  return result;
}

export function notifySidebarPreferencesChanged() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(SIDEBAR_NAV_PREFERENCES_EVENT));
}

const CUSTOMIZATION_SIDEBAR_ITEMS: Record<string, string> = {
  "navigation.sidebar.memory.visible": "memory",
  "navigation.sidebar.agents.visible": "agents",
  "navigation.sidebar.skills.visible": "skills",
  "navigation.sidebar.tools.visible": "tools",
};

export function applyCustomizationSidebarValues(
  values: Record<string, unknown>,
  sources?: Record<
    string,
    { layer?: string; profile_id?: string | null; revision?: number | null }
  >,
) {
  const next = loadSidebarVisibleKeys();
  for (const [itemId, navKey] of Object.entries(CUSTOMIZATION_SIDEBAR_ITEMS)) {
    // Factory values describe the product baseline. They must not overwrite
    // the user's sidebar preference; otherwise the Appearance switch can say
    // “on” while the entry stays hidden. Explicit profile/override layers still
    // take precedence as intended by the customization system.
    if (sources?.[itemId]?.layer === "factory") continue;
    const visible = values[itemId];
    if (visible === true) next.add(navKey);
    if (visible === false) next.delete(navKey);
  }
  // Profile values are an overlay, not a manual sidebar preference. Persisting
  // the merged result caused a hidden profile item to remain hidden after its
  // profile override was removed.
  return next;
}

export function isSidebarNavVisible(key: string, visibleKeys: Set<string>) {
  if (!knownKeys.has(key)) return true;
  return visibleKeys.has(key);
}
