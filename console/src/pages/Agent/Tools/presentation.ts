import type { ToolInfo } from "../../../api/modules/tools";

export type ToolStateKey =
  | "disabled"
  | "needs_configuration"
  | "needs_verification"
  | "enabled";

export interface ToolStateView {
  key: ToolStateKey;
  label: string;
  detail: string;
}

function hasValue(value: unknown) {
  return value !== undefined && value !== null && value !== "";
}

export function describeToolState(tool: ToolInfo): ToolStateView {
  if (!tool.enabled) {
    return {
      key: "disabled",
      label: "已停用",
      detail: "当前智能体不会调用这项工具。",
    };
  }
  if (tool.requires_config) {
    const required = (tool.config_fields || []).filter(
      (field) => field.required,
    );
    const complete = required.every((field) =>
      hasValue(tool.config_values?.[field.name]),
    );
    if (!complete) {
      return {
        key: "needs_configuration",
        label: "待配置",
        detail: "补全必填配置后，才能尝试调用。",
      };
    }
    return {
      key: "needs_verification",
      label: "配置齐全 · 待验证",
      detail: "配置已保存，但尚不能代表真实调用成功。",
    };
  }
  return {
    key: "enabled",
    label: "已启用",
    detail: "当前智能体可以尝试调用，实际执行仍受权限规则约束。",
  };
}

export function primaryToolDescription(
  tool: ToolInfo,
  localizedDescription: string,
) {
  const localized = localizedDescription.trim();
  if (localized && localized !== tool.description) return localized;
  if (/[㐀-鿿]/u.test(localized)) return localized;
  return tool.category_description?.trim() || "按任务需要调用的扩展能力。";
}
