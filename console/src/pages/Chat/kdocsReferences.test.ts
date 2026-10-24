import { describe, expect, it } from "vitest";
import {
  documentReferenceKey,
  documentToKdocsReference,
  excerptToKdocsReference,
  loadKdocsReferences,
  mergeKdocsReference,
  resolveKdocsSubmitDraft,
  saveKdocsReferences,
  snapshotKdocsReferencesForRequest,
} from "./kdocsReferences";
import type { KdocsDocumentItem } from "../../api/types/kdocs";

const document = (driveId = "drive-a"): KdocsDocumentItem => ({
  key: `${driveId}:file-1`,
  drive_id: driveId,
  file_id: "file-1",
  parent_id: "0",
  name: "周报.docx",
  type: "docx",
  suffix: "docx",
  is_folder: false,
  size: 12,
  version: "7",
  content_hash: "hash-7",
  link_id: "",
  original_url: "https://www.kdocs.cn/l/weekly",
  location: "我的云文档 / 周报",
  owner: null,
  modifier: null,
  shared: false,
  permissions: { can_read: true },
});

describe("KDocs chat reference state", () => {
  it("uses drive and file together as stable identity", () => {
    expect(documentReferenceKey("drive-a", "file-1")).not.toBe(
      documentReferenceKey("drive-b", "file-1"),
    );
  });

  it("keeps read state when the same reference is refreshed", () => {
    const first = documentToKdocsReference(document());
    const reading = { ...first, read_status: "reading" as const };
    const read = {
      ...first,
      read_status: "read" as const,
      content_snapshot: {
        text: "正文",
        format: "markdown",
        truncated: false,
      },
    };
    expect(mergeKdocsReference([reading], read)).toEqual([read]);
  });

  it("keeps distinct excerpts from the same document location", () => {
    const first = excerptToKdocsReference(
      document(),
      "营收同比增长 18%",
      "经营摘要",
    );
    const repeat = excerptToKdocsReference(
      document(),
      "营收同比增长 18%",
      "经营摘要",
    );
    const second = excerptToKdocsReference(
      document(),
      "毛利率提升 3 个百分点",
      "经营摘要",
    );

    expect(first.key).toBe(repeat.key);
    expect(first.key).not.toBe(second.key);
    expect(mergeKdocsReference([first], second)).toHaveLength(2);
  });

  it("persists references per agent and conversation", () => {
    const memory = new Map<string, string>();
    const storage = {
      getItem: (key: string) => memory.get(key) ?? null,
      setItem: (key: string, value: string) => void memory.set(key, value),
    };
    const reference = documentToKdocsReference(document());
    saveKdocsReferences("agent-a", "chat-a", [reference], storage);
    expect(loadKdocsReferences("agent-a", "chat-a", storage)).toEqual([
      reference,
    ]);
    expect(loadKdocsReferences("agent-a", "chat-b", storage)).toEqual([]);
  });

  it("freezes a deep snapshot at the request boundary", () => {
    const reference = {
      ...documentToKdocsReference(document()),
      read_status: "read" as const,
      read_snapshot: {
        version: "7",
        content_hash: "hash-7",
        read_at: "2026-09-20T08:00:00.000Z",
      },
      content_snapshot: {
        text: "发送时的正文",
        format: "markdown",
        truncated: false,
      },
    };

    const snapshot = snapshotKdocsReferencesForRequest([reference]);
    reference.content_snapshot.text = "发送后的本地变化";
    reference.read_snapshot.version = "8";

    expect(snapshot[0].content_snapshot?.text).toBe("发送时的正文");
    expect(snapshot[0].read_snapshot?.version).toBe("7");
    expect(snapshot[0]).not.toBe(reference);
  });

  it("blocks submission while a document is reading", () => {
    const reading = {
      ...documentToKdocsReference(document()),
      read_status: "reading" as const,
    };

    expect(resolveKdocsSubmitDraft("请总结", [reading])).toEqual({
      blocked: true,
      value: "请总结",
    });
  });

  it("creates an explicit prompt for a reference-only message", () => {
    const reference = documentToKdocsReference(document());

    expect(resolveKdocsSubmitDraft("  ", [reference])).toEqual({
      blocked: false,
      value: "请阅读并处理我引用的金山云文档。",
    });
  });
});
