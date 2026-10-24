import { describe, expect, it } from "vitest";
import {
  buildAttachmentRoleContext,
  getAttachmentRoleLabel,
  parseAttachmentRoleContext,
  shouldConfirmAttachmentRoles,
  stripAttachmentRoleContext,
} from "./lantaiAttachmentRoles";

describe("lantaiAttachmentRoles", () => {
  it("round-trips structured role context without leaking it into display text", () => {
    const context = buildAttachmentRoleContext([
      { name: "原稿.docx", role: "edit_target" },
      { name: "品牌手册.pdf", role: "style_reference" },
    ]);
    const text = `请优化这份方案\n${context}`;
    const parsed = parseAttachmentRoleContext(text);

    expect(parsed.get("原稿.docx")).toBe("edit_target");
    expect(parsed.get("品牌手册.pdf")).toBe("style_reference");
    expect(stripAttachmentRoleContext(text)).toBe("请优化这份方案");
  });

  it("uses quiet, readable labels for every role", () => {
    expect(getAttachmentRoleLabel("reference")).toBe("参考资料");
    expect(getAttachmentRoleLabel("output_template")).toBe("输出模板");
  });

  it("only asks for review when a multi-file draft still has unconfirmed roles", () => {
    expect(
      shouldConfirmAttachmentRoles([
        { name: "a.docx", roleConfirmed: true },
        { name: "b.pdf", roleConfirmed: false },
      ]),
    ).toBe(true);
    expect(
      shouldConfirmAttachmentRoles([
        { name: "a.docx", roleConfirmed: true },
        { name: "b.pdf", roleConfirmed: true },
      ]),
    ).toBe(false);
    expect(
      shouldConfirmAttachmentRoles([{ name: "a.docx", roleConfirmed: false }]),
    ).toBe(false);
  });
});
