export const ECHARTS_SOURCE_LIMIT = 200_000;
export const ECHARTS_NODE_LIMIT = 20_000;
export const ECHARTS_DEPTH_LIMIT = 24;

const UNSAFE_KEYS = new Set(["__proto__", "prototype", "constructor"]);
const UNSAFE_TEXT =
  /(?:javascript:|data:text\/html|<\s*script\b|on[a-z]+\s*=)/i;
const REMOTE_IMAGE = /^(?:image:\/\/)?(?:https?:|file:|data:)/i;

export type EChartsOption = Record<string, unknown>;

export interface ParsedEChartsArtifact {
  title: string;
  description?: string;
  height: number;
  option: EChartsOption;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function assertSafeValue(
  value: unknown,
  depth: number,
  count: { value: number },
  key = "",
) {
  count.value += 1;
  if (count.value > ECHARTS_NODE_LIMIT) {
    throw new Error("图表配置过于复杂");
  }
  if (depth > ECHARTS_DEPTH_LIMIT) {
    throw new Error("图表配置嵌套过深");
  }
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("图表配置包含无效数字");
    return;
  }
  if (typeof value === "string") {
    if (UNSAFE_TEXT.test(value)) {
      throw new Error("图表配置包含不安全文本");
    }
    if ((key === "symbol" || key === "icon") && REMOTE_IMAGE.test(value)) {
      throw new Error("图表配置不允许加载外部图像");
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => assertSafeValue(item, depth + 1, count, key));
    return;
  }
  if (!isPlainObject(value)) {
    throw new Error("图表配置只能包含 JSON 数据");
  }
  Object.entries(value).forEach(([entryKey, entryValue]) => {
    if (UNSAFE_KEYS.has(entryKey)) {
      throw new Error(`图表配置包含不安全字段：${entryKey}`);
    }
    assertSafeValue(entryValue, depth + 1, count, entryKey);
  });
}

function textValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function inferTitle(option: EChartsOption): string {
  const title = option.title;
  if (isPlainObject(title)) {
    return textValue(title.text) || "数据图表";
  }
  if (Array.isArray(title)) {
    for (const item of title) {
      if (isPlainObject(item)) {
        const text = textValue(item.text);
        if (text) return text;
      }
    }
  }
  return "数据图表";
}

function normalizedHeight(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return 360;
  return Math.min(720, Math.max(240, Math.round(value)));
}

export function parseEChartsArtifact(source: string): ParsedEChartsArtifact {
  if (!source.trim()) throw new Error("图表配置为空");
  if (source.length > ECHARTS_SOURCE_LIMIT) {
    throw new Error("图表配置超过 200 KB 限制");
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch {
    throw new Error("图表配置不是有效的 JSON");
  }
  if (!isPlainObject(parsed)) {
    throw new Error("图表配置必须是 JSON 对象");
  }
  assertSafeValue(parsed, 0, { value: 0 });

  const hasEnvelope = Object.prototype.hasOwnProperty.call(parsed, "option");
  const option = hasEnvelope ? parsed.option : parsed;
  if (!isPlainObject(option)) {
    throw new Error("option 必须是 JSON 对象");
  }

  return {
    title:
      textValue(hasEnvelope ? parsed.title : undefined) || inferTitle(option),
    description: textValue(hasEnvelope ? parsed.description : undefined),
    height: normalizedHeight(hasEnvelope ? parsed.height : undefined),
    option,
  };
}
