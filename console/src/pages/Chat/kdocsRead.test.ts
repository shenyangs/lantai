import { describe, expect, it, vi } from "vitest";
import type { KdocsDocumentItem } from "../../api/types/kdocs";
import { readKdocsDocument, readKdocsDocumentSafely } from "./kdocsRead";

const document: KdocsDocumentItem = {
  key: "drive-a:file-a",
  drive_id: "drive-a",
  file_id: "file-a",
  parent_id: "0",
  name: "经营周报.docx",
  type: "docx",
  suffix: "docx",
  is_folder: false,
  size: 100,
  version: "12",
  content_hash: "hash-12",
  link_id: "",
  original_url: "",
  location: "我的云文档 / 周报",
  owner: null,
  modifier: null,
  shared: false,
  permissions: { can_read: true },
};

describe("KDocs document reading", () => {
  it("polls a pending task and records a partial read snapshot", async () => {
    const readDocument = vi
      .fn()
      .mockResolvedValueOnce({
        status: "pending",
        task_id: "task-a",
        content: "",
        content_format: "",
        name: "",
        truncated: false,
        warnings: [],
      })
      .mockResolvedValueOnce({
        status: "done",
        task_id: "task-a",
        content: "# 本周结论",
        content_format: "markdown",
        name: document.name,
        truncated: true,
        warnings: ["正文过长，仅返回前 12 万字"],
      });

    const result = await readKdocsDocument(document, {
      readDocument,
      wait: async () => undefined,
    });

    expect(readDocument).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ task_id: "task-a" }),
    );
    expect(result.read_status).toBe("partial");
    expect(result.content_snapshot?.text).toBe("# 本周结论");
    expect(result.read_snapshot).toEqual(
      expect.objectContaining({ version: "12", content_hash: "hash-12" }),
    );
  });

  it("turns connector fidelity warnings into a clear, non-failure explanation", async () => {
    const result = await readKdocsDocument(document, {
      readDocument: async () => ({
        status: "done",
        task_id: "",
        content: "# 已读取的正文",
        content_format: "markdown",
        name: document.name,
        truncated: false,
        warnings: [
          "For fine-grained OTL ops or full formatting, use otl.block_query",
        ],
      }),
    });

    expect(result.read_status).toBe("partial");
    expect(result.error_message).toBe(
      "正文已经可以使用；部分块级结构、精细排版或嵌入内容未展开。需要完整呈现时，请查看在线原文。",
    );
    expect(result.error_message).not.toContain("otl.block_query");
  });

  it("returns an explicit failed reference when reading fails", async () => {
    const result = await readKdocsDocumentSafely(document, {
      readDocument: async () => {
        throw new Error("你暂时没有访问这个文档的权限。");
      },
    });

    expect(result.read_status).toBe("failed");
    expect(result.error_message).toBe("你暂时没有访问这个文档的权限。");
    expect(result.content_snapshot).toBeUndefined();
  });
});
