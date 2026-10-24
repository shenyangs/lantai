import type { CustomizationRegistryItem } from "../../../api/modules/customization";

export type CustomizationFieldLevel = "common" | "expert";

interface FieldPresentation {
  description: string;
  level?: CustomizationFieldLevel;
  placeholder?: string;
  optionLabels?: Record<string, string>;
  optionDescriptions?: Record<string, string>;
}

const fieldPresentations: Record<string, FieldPresentation> = {
  "appearance.theme_mode": {
    description: "跟随系统，或固定使用浅色、深色外观。",
    optionLabels: { system: "跟随系统", light: "浅色", dark: "深色" },
    optionDescriptions: {
      system: "系统切换明暗外观时，澜台会自动同步。",
      light: "始终使用浅色外观。",
      dark: "始终使用深色外观。",
    },
  },
  "appearance.palette": {
    description: "改变按钮、选中状态和重点信息使用的颜色。",
    optionLabels: {
      lantai: "澜台原色",
      mist: "雾灰",
      pine: "松绿",
      blue: "海蓝",
      warm: "暖橙",
      violet: "柔紫",
      custom: "自定义",
    },
    optionDescriptions: {
      lantai: "澜台默认的克制蓝灰配色。",
      mist: "更中性、更低对比的灰色调。",
      pine: "安静的绿色调。",
      blue: "清晰的蓝色调。",
      warm: "更温暖、更有亲和力的橙棕色调。",
      violet: "柔和的紫色调。",
      custom: "使用外观设置中保存的自定义颜色。",
    },
  },
  "appearance.sidebar_material": {
    description: "控制左侧导航栏是纯色还是带有系统半透明材质。",
    optionLabels: {
      solid: "纯色·最清晰",
      "light-glass": "轻透",
      "mist-glass": "雾面半透明",
      "deep-glass": "深度半透明",
    },
    optionDescriptions: {
      solid: "对比最稳定，适合大多数环境。",
      "light-glass": "轻微透出桌面背景。",
      "mist-glass": "更明显的柔和磨砂效果。",
      "deep-glass": "透视最强，复杂背景下文字对比可能降低。",
    },
  },
  "appearance.composer_size": {
    description: "调整聊天输入区的默认高度和留白。",
    optionLabels: {
      compact: "紧凑",
      comfortable: "舒适·推荐",
      spacious: "宽敞",
    },
    optionDescriptions: {
      compact: "节省垂直空间，适合小窗口。",
      comfortable: "在输入空间与内容可见范围之间保持平衡。",
      spacious: "更适合经常输入长提示词。",
    },
  },
  "appearance.design_tokens": {
    description: "用受限的 CSS 变量微调界面。格式错误的内容不会被应用。",
    level: "expert",
    placeholder: '{\n  "--lt-accent": "#45647f"\n}',
  },
  "appearance.app_icon": {
    description: "引用已安装在本机的澜台图标。建议优先使用外观页的图标选择器。",
    level: "expert",
    placeholder: "例如：default",
  },
  "navigation.sidebar.memory.visible": {
    description: "在左侧导航中显示“记忆”入口；隐藏不会删除记忆。",
  },
  "navigation.sidebar.agents.visible": {
    description: "在左侧导航中显示“智能体”入口。",
  },
  "navigation.sidebar.skills.visible": {
    description: "在左侧导航中显示“技能”入口；隐藏不会停用技能。",
  },
  "navigation.sidebar.tools.visible": {
    description: "在左侧导航中显示“工具”入口。",
  },
  "conversation.show_inner_voice": {
    description: "在对话中显示蛐蛐的观察与提醒。",
  },
  "conversation.show_thinking": {
    description: "显示安全的推理状态；原始内部推理不会公开，不影响回答质量。",
  },
  "conversation.show_tool_calls": {
    description: "显示读取文件、搜索等工具的调用过程。",
  },
  "conversation.show_token_usage": {
    description: "在回答旁显示 Token 用量信息。",
  },
  "persona.presence": {
    description: "决定澜台在没有必要打断你时，表达个性与陪伴感的程度。",
    optionLabels: {
      full: "鲜明",
      balanced: "平衡·推荐",
      quiet: "安静",
      off: "关闭人格表达",
    },
    optionDescriptions: {
      full: "更主动地表达判断、关心和个性。",
      balanced: "在重要时刻表达，其余时候保持克制。",
      quiet: "主要完成任务，减少额外表达。",
      off: "只保留功能性回答，不主动体现人格。",
    },
  },
  "persona.user_address": {
    description: "澜台在对话中如何称呼你；留空时不使用固定称呼。",
    placeholder: "例如：Sam（可留空）",
  },
  "persona.proactivity": { description: "决定澜台主动补充建议和提醒的倾向。" },
  "models.routing.policy": {
    description: "指定不同任务使用哪些模型。错误路由会在应用前被阻止。",
    level: "expert",
    placeholder: '{\n  "default": "auto"\n}',
  },
  "agents.enabled": {
    description: "限制这个配置档可以调用的专业智能体；“*”表示全部。",
    level: "expert",
    placeholder: "每行一个智能体 ID；* 表示全部",
  },
  "work_methods.default": {
    description: "新对话默认采用的工作法 ID；对话内仍可单独切换。",
    level: "expert",
    placeholder: "例如：standard",
  },
  "memory.capture.enabled": {
    description: "允许澜台从对话中整理候选记忆；关闭不会删除已有记忆。",
  },
  "memory.recall.max_results": {
    description: "每次回答最多参考多少条相关记忆。数值越高，上下文越丰富。",
  },
  "permissions.tools.allowed": {
    description: "限制这个配置档可以使用的工具；不会授予系统尚未允许的权限。",
    level: "expert",
    placeholder: "每行一个工具 ID；* 表示全部已授权工具",
  },
  "permissions.filesystem.allowed_roots": {
    description: "进一步限制可访问的目录。留空表示不增加配置档级目录授权。",
    level: "expert",
    placeholder: "每行一个完整目录路径",
  },
  "automation.proactivity": {
    description: "控制澜台提出自动化建议的频率，不会让任务未经确认直接运行。",
  },
  "automation.quiet_hours": {
    description: "设置不主动提醒你的时间段，使用 24 小时制。",
    level: "expert",
    placeholder: '{\n  "start": "22:00",\n  "end": "08:00"\n}',
  },
  "delivery.default_format": {
    description: "没有指定格式时，澜台优先采用的交付文件类型。",
    optionLabels: {
      auto: "自动选择·推荐",
      markdown: "Markdown 文档",
      docx: "Word 文档",
      pdf: "PDF 文档",
      pptx: "演示文稿",
      xlsx: "Excel 表格",
    },
    optionDescriptions: {
      auto: "由澜台根据任务内容选择最合适的格式。",
      markdown: "适合纯文本、说明和可继续编辑的内容。",
      docx: "适合正式长文档。",
      pdf: "适合固定版式的最终交付。",
      pptx: "适合汇报和演示。",
      xlsx: "适合结构化数据与表格分析。",
    },
  },
  "delivery.naming_pattern": {
    description: "控制生成文件的默认名称；{title} 会替换为作品标题。",
    placeholder: "例如：{title}",
  },
};

export function getFieldPresentation(item: CustomizationRegistryItem) {
  const presentation = fieldPresentations[item.id];
  return {
    description:
      presentation?.description ||
      item.description ||
      "调整这项设置在当前配置档中的表现。",
    level:
      presentation?.level ||
      (item.value_type === "object" || item.risk === "high"
        ? "expert"
        : "common"),
    placeholder: presentation?.placeholder,
    optionLabels: presentation?.optionLabels || {},
    optionDescriptions: presentation?.optionDescriptions || {},
  } satisfies Required<Pick<FieldPresentation, "description" | "level">> &
    FieldPresentation;
}

export function getOptionLabel(
  item: CustomizationRegistryItem,
  value: unknown,
) {
  return (
    getFieldPresentation(item).optionLabels[String(value)] || String(value)
  );
}

export function getOptionDescription(
  item: CustomizationRegistryItem,
  value: unknown,
) {
  return getFieldPresentation(item).optionDescriptions[String(value)] || "";
}

export function valuesEqual(left: unknown, right: unknown) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function formatCustomizationValue(
  item: CustomizationRegistryItem,
  value: unknown,
) {
  if (item.enum?.length) return getOptionLabel(item, value);
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (value === null || value === undefined || value === "") return "未设置";
  if (Array.isArray(value))
    return value.length === 1 && value[0] === "*"
      ? "全部"
      : `${value.length} 项`;
  if (item.value_type === "number") {
    if (item.maximum === 1) return `${Math.round(Number(value) * 100)}%`;
    if (item.id === "memory.recall.max_results") return `${value} 条`;
  }
  if (typeof value === "object")
    return Object.keys(value as object).length ? "已配置" : "未配置";
  return String(value);
}

export function getNumberMeaning(
  item: CustomizationRegistryItem,
  value: number,
) {
  if (item.id === "memory.recall.max_results") {
    if (value === 0) return "不自动参考记忆";
    if (value <= 5) return "少量参考";
    if (value <= 12) return "平衡·推荐";
    return "更多上下文";
  }
  if (value <= 0.2) return "很少";
  if (value <= 0.45) return "较少";
  if (value <= 0.7) return "适中·推荐";
  return "较多";
}

export function booleanStateLabel(
  item: CustomizationRegistryItem,
  value: boolean,
) {
  if (item.id.startsWith("navigation.sidebar.")) return value ? "显示" : "隐藏";
  if (item.id === "memory.capture.enabled")
    return value ? "允许整理" : "暂停整理";
  return value ? "开启" : "关闭";
}
