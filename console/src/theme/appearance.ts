export type AppearancePaletteId =
  | "lantai"
  | "mist"
  | "pine"
  | "blue"
  | "warm"
  | "violet"
  | "ink"
  | "custom";

export const DEFAULT_APPEARANCE_PALETTE: AppearancePaletteId = "mist";

export type ComposerSize = "compact" | "comfortable" | "spacious";
export type PersonaMode = "full" | "balanced" | "quiet" | "off";
export type WelcomeCulturalCalendar = "none" | "china" | "global" | "all";
export type WelcomeTone = "natural" | "concise" | "restrained";
export type SidebarMaterial =
  | "solid"
  | "light-glass"
  | "mist-glass"
  | "deep-glass";

/** Legacy persisted names remain readable; the UI exposes two application materials. */
export function resolveInterfaceMaterial(
  value: SidebarMaterial,
): "solid" | "mist-glass" {
  return value === "light-glass" ||
    value === "mist-glass" ||
    value === "deep-glass"
    ? "mist-glass"
    : "solid";
}

export interface AppearanceDisplaySettings {
  showInnerVoice: boolean;
  personaMode: PersonaMode;
  showThinking: boolean;
  showToolCalls: boolean;
  showTokenUsage: boolean;
  dynamicWelcome: boolean;
  aiWelcome: boolean;
  welcomeCulturalCalendar: WelcomeCulturalCalendar;
  welcomeTone: WelcomeTone;
  composerSize: ComposerSize;
  sidebarMaterial: SidebarMaterial;
}

export interface AppearancePalettePreview {
  accent: string;
  sidebar: string;
  surface: string;
  surfaceMuted: string;
  userBubble: string;
  assistantBubble: string;
  tool: string;
  code: string;
  text: string;
  muted: string;
  border: string;
}

export interface AppearancePalette {
  id: AppearancePaletteId;
  name: string;
  subtitle: string;
  swatch: string;
  accent: string;
  darkAccent?: string;
  preview: {
    light: AppearancePalettePreview;
    dark: AppearancePalettePreview;
  };
}

const appearancePaletteDefinitions: AppearancePalette[] = [
  {
    id: "mist",
    name: "石墨",
    subtitle: "默认主题色，中性而不抢内容",
    swatch: "#69717d",
    accent: "#69717d",
    darkAccent: "#b0b7c0",
    preview: {
      light: {
        accent: "#69717d",
        sidebar: "#f8f7f4",
        surface: "#fffdf9",
        surfaceMuted: "#f4f3ee",
        userBubble: "#464b49",
        assistantBubble: "#fffdf9",
        tool: "#e9e8e2",
        code: "#efefec",
        text: "#202124",
        muted: "#6d6d6d",
        border: "#d9d6ce",
      },
      dark: {
        accent: "#b0b7c0",
        sidebar: "#222222",
        surface: "#1b1b1b",
        surfaceMuted: "#222222",
        userBubble: "#535b58",
        assistantBubble: "#1f1f1f",
        tool: "#282828",
        code: "#151515",
        text: "rgba(245,245,245,0.92)",
        muted: "rgba(235,235,235,0.62)",
        border: "rgba(255,255,255,0.1)",
      },
    },
  },
  {
    id: "lantai",
    name: "墨蓝",
    subtitle: "冷静克制的墨蓝",
    swatch: "#41566d",
    accent: "#4b6581",
    darkAccent: "#95b9d9",
    preview: {
      light: {
        accent: "#4b6581",
        sidebar: "#f2f6f9",
        surface: "#ffffff",
        surfaceMuted: "#f1f5f8",
        userBubble: "#464b49",
        assistantBubble: "#ffffff",
        tool: "#e6edf3",
        code: "#edf2f6",
        text: "#202124",
        muted: "#6d6d6d",
        border: "#c8d5df",
      },
      dark: {
        accent: "#95b9d9",
        sidebar: "#151a21",
        surface: "#151b22",
        surfaceMuted: "#1c242d",
        userBubble: "#535b58",
        assistantBubble: "#18202a",
        tool: "#202a35",
        code: "#101419",
        text: "rgba(245,245,245,0.92)",
        muted: "rgba(235,235,235,0.62)",
        border: "rgba(255,255,255,0.1)",
      },
    },
  },
  {
    id: "pine",
    name: "澜绿",
    subtitle: "安静而清楚",
    swatch: "#16855f",
    accent: "#16855f",
    darkAccent: "#75d6a8",
    preview: {
      light: {
        accent: "#16855f",
        sidebar: "#f1faf4",
        surface: "#ffffff",
        surfaceMuted: "#eff8f2",
        userBubble: "#464b49",
        assistantBubble: "#ffffff",
        tool: "#dcefe4",
        code: "#ebf7ef",
        text: "#202124",
        muted: "#6d6d6d",
        border: "#bee2cb",
      },
      dark: {
        accent: "#75d6a8",
        sidebar: "#142219",
        surface: "#152018",
        surfaceMuted: "#1b2a20",
        userBubble: "#535b58",
        assistantBubble: "#18271d",
        tool: "#213528",
        code: "#0c140f",
        text: "rgba(245,245,245,0.92)",
        muted: "rgba(235,235,235,0.62)",
        border: "rgba(255,255,255,0.1)",
      },
    },
  },
  {
    id: "blue",
    name: "钴蓝",
    subtitle: "高识别的工程蓝",
    swatch: "#3457d5",
    accent: "#3457d5",
    darkAccent: "#8fa8ff",
    preview: {
      light: {
        accent: "#3457d5",
        sidebar: "#f2f6ff",
        surface: "#ffffff",
        surfaceMuted: "#f0f4ff",
        userBubble: "#464b49",
        assistantBubble: "#ffffff",
        tool: "#dde6ff",
        code: "#eef3ff",
        text: "#202124",
        muted: "#6d6d6d",
        border: "#c5d4fb",
      },
      dark: {
        accent: "#8fa8ff",
        sidebar: "#141d36",
        surface: "#151b32",
        surfaceMuted: "#1c2544",
        userBubble: "#535b58",
        assistantBubble: "#19213d",
        tool: "#24305a",
        code: "#0d1120",
        text: "rgba(245,245,245,0.92)",
        muted: "rgba(235,235,235,0.62)",
        border: "rgba(255,255,255,0.1)",
      },
    },
  },
  {
    id: "warm",
    name: "陶土",
    subtitle: "暖色阅读和计划",
    swatch: "#b86424",
    accent: "#b86424",
    darkAccent: "#e3a15d",
    preview: {
      light: {
        accent: "#b86424",
        sidebar: "#f9f0e8",
        surface: "#fffefa",
        surfaceMuted: "#faf0e3",
        userBubble: "#464b49",
        assistantBubble: "#fffefa",
        tool: "#f0dac4",
        code: "#f8ecdc",
        text: "#202124",
        muted: "#6d6d6d",
        border: "#dec09f",
      },
      dark: {
        accent: "#e3a15d",
        sidebar: "#211910",
        surface: "#241a13",
        surfaceMuted: "#302217",
        userBubble: "#535b58",
        assistantBubble: "#2b1e14",
        tool: "#3a2818",
        code: "#15100b",
        text: "rgba(245,245,245,0.92)",
        muted: "rgba(235,235,235,0.62)",
        border: "rgba(255,255,255,0.1)",
      },
    },
  },
  {
    id: "violet",
    name: "藤紫",
    subtitle: "创作感更强的紫调",
    swatch: "#7a4cc2",
    accent: "#7a4cc2",
    darkAccent: "#c3a6ff",
    preview: {
      light: {
        accent: "#7a4cc2",
        sidebar: "#f3f0fc",
        surface: "#ffffff",
        surfaceMuted: "#f6f3ff",
        userBubble: "#464b49",
        assistantBubble: "#ffffff",
        tool: "#e8e1fa",
        code: "#f0ecfb",
        text: "#202124",
        muted: "#6d6d6d",
        border: "#cabdeb",
      },
      dark: {
        accent: "#c3a6ff",
        sidebar: "#211936",
        surface: "#201932",
        surfaceMuted: "#2a2140",
        userBubble: "#535b58",
        assistantBubble: "#251d39",
        tool: "#342951",
        code: "#120f1c",
        text: "rgba(245,245,245,0.92)",
        muted: "rgba(235,235,235,0.62)",
        border: "rgba(255,255,255,0.1)",
      },
    },
  },
  {
    id: "custom",
    name: "自定义",
    subtitle: "直接覆盖主题变量",
    swatch:
      "linear-gradient(135deg, #4b6581 0%, #69717d 22%, #16855f 42%, #3457d5 62%, #b86424 80%, #7a4cc2 100%)",
    accent: "#69717d",
    darkAccent: "#b0b7c0",
    preview: {
      light: {
        accent: "#69717d",
        sidebar: "#f8f7f4",
        surface: "#fffdf9",
        surfaceMuted: "#f4f3ee",
        userBubble: "#464b49",
        assistantBubble: "#fffdf9",
        tool: "#e9e8e2",
        code: "#efefec",
        text: "#202124",
        muted: "#6d6d6d",
        border: "#d9d6ce",
      },
      dark: {
        accent: "#b0b7c0",
        sidebar: "#222222",
        surface: "#1b1b1b",
        surfaceMuted: "#222222",
        userBubble: "#535b58",
        assistantBubble: "#1f1f1f",
        tool: "#282828",
        code: "#151515",
        text: "rgba(245,245,245,0.92)",
        muted: "rgba(235,235,235,0.62)",
        border: "rgba(255,255,255,0.1)",
      },
    },
  },
];

// Brand OS keeps the workspace material neutral. A palette may change the
// accent used by selection, focus, primary actions, and running state, but it
// must not recolour the canvas, sidebar, cards, or content surfaces.
const neutralPreview = {
  light: {
    sidebar: "#e9e7e2",
    surface: "#fffdfb",
    surfaceMuted: "#f4f3ee",
    userBubble: "#464b49",
    assistantBubble: "#fffdfb",
    tool: "#e9e8e2",
    code: "#f7f7f4",
    text: "#202124",
    muted: "#6d6d6d",
    border: "#d9d6ce",
  },
  dark: {
    sidebar: "#242424",
    surface: "#1b1b1b",
    surfaceMuted: "#222222",
    userBubble: "#535b58",
    assistantBubble: "#1f1f1f",
    tool: "#282828",
    code: "#151515",
    text: "rgba(245,245,245,0.92)",
    muted: "rgba(235,235,235,0.62)",
    border: "rgba(255,255,255,0.1)",
  },
} as const;

const paletteOrder: AppearancePaletteId[] = [
  "mist",
  "pine",
  "lantai",
  "blue",
  "warm",
  "violet",
  "custom",
];

export const appearancePalettes: AppearancePalette[] =
  appearancePaletteDefinitions
    .map((palette) => ({
      ...palette,
      preview: {
        light: { ...neutralPreview.light, accent: palette.accent },
        dark: {
          ...neutralPreview.dark,
          accent: palette.darkAccent || palette.accent,
        },
      },
    }))
    .sort(
      (left, right) =>
        paletteOrder.indexOf(left.id) - paletteOrder.indexOf(right.id),
    );

export const defaultDisplaySettings: AppearanceDisplaySettings = {
  showInnerVoice: true,
  personaMode: "full",
  showThinking: true,
  showToolCalls: true,
  showTokenUsage: true,
  dynamicWelcome: true,
  aiWelcome: true,
  welcomeCulturalCalendar: "none",
  welcomeTone: "natural",
  composerSize: "comfortable",
  sidebarMaterial: "solid",
};

export const defaultCustomThemeCss = `:root {
  --lt-accent: #69717d;
  --lt-accent-hover: #4d5662;
  --lt-sidebar-bg: #e9e7e2;
  --lt-surface: #fffdfb;
  --lt-chat-user-bg: #464b49;
  --lt-chat-user-text: #f7f5f0;
}`;

export function getAppearancePalette(id: AppearancePaletteId) {
  return (
    appearancePalettes.find((palette) => palette.id === id) ??
    appearancePalettes.find(
      (palette) => palette.id === DEFAULT_APPEARANCE_PALETTE,
    ) ??
    appearancePalettes[0]
  );
}
