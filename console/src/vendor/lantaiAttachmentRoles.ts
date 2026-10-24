export type LantaiAttachmentRole =
  | "reference"
  | "edit_target"
  | "style_reference"
  | "output_template";

export interface LantaiAttachmentRoleItem {
  name: string;
  role?: LantaiAttachmentRole;
  roleConfirmed?: boolean;
}

export const DEFAULT_ATTACHMENT_ROLE: LantaiAttachmentRole = "reference";

export const ATTACHMENT_ROLE_OPTIONS: ReadonlyArray<{
  value: LantaiAttachmentRole;
  label: string;
  description: string;
}> = [
  {
    value: "reference",
    label: "参考资料",
    description: "用于理解背景和提取信息，不直接改写原文件",
  },
  {
    value: "edit_target",
    label: "需要修改",
    description: "这是本次需要编辑、改写或处理的原文件",
  },
  {
    value: "style_reference",
    label: "样式参考",
    description: "只参考版式、语气或视觉风格",
  },
  {
    value: "output_template",
    label: "输出模板",
    description: "最终结果需要遵循它的结构或格式",
  },
] as const;

const ROLE_CONTEXT_PATTERN =
  /<lantai_attachment_roles>\s*([\s\S]*?)\s*<\/lantai_attachment_roles>/g;

export function isLantaiAttachmentRole(
  value: unknown,
): value is LantaiAttachmentRole {
  return ATTACHMENT_ROLE_OPTIONS.some((option) => option.value === value);
}

export function getAttachmentRoleLabel(
  role: LantaiAttachmentRole | undefined,
): string {
  return (
    ATTACHMENT_ROLE_OPTIONS.find(
      (option) => option.value === (role || DEFAULT_ATTACHMENT_ROLE),
    )?.label || "参考资料"
  );
}

export function buildAttachmentRoleContext(
  attachments: LantaiAttachmentRoleItem[],
): string {
  if (attachments.length === 0) return "";
  const assignments = attachments.map((attachment) => ({
    name: attachment.name,
    role: attachment.role || DEFAULT_ATTACHMENT_ROLE,
    label: getAttachmentRoleLabel(attachment.role),
  }));
  return `<lantai_attachment_roles>\n${JSON.stringify(assignments)}\n</lantai_attachment_roles>`;
}

export function parseAttachmentRoleContext(
  text: string,
): Map<string, LantaiAttachmentRole> {
  const roles = new Map<string, LantaiAttachmentRole>();
  for (const match of text.matchAll(ROLE_CONTEXT_PATTERN)) {
    try {
      const parsed = JSON.parse(match[1] || "[]");
      if (!Array.isArray(parsed)) continue;
      parsed.forEach((item) => {
        if (
          item &&
          typeof item === "object" &&
          typeof item.name === "string" &&
          isLantaiAttachmentRole(item.role)
        ) {
          roles.set(item.name, item.role);
        }
      });
    } catch {
      // Corrupt role metadata should never block rendering an old message.
    }
  }
  return roles;
}

export function stripAttachmentRoleContext(text: string): string {
  return text
    .replace(ROLE_CONTEXT_PATTERN, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

export function shouldConfirmAttachmentRoles(
  attachments: LantaiAttachmentRoleItem[],
): boolean {
  return (
    attachments.length >= 2 &&
    attachments.some((attachment) => !attachment.roleConfirmed)
  );
}
