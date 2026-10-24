import { describe, expect, it } from "vitest";
import type {
  WikiPageSummary,
  WikiQueueSummary,
  WikiSourceInfo,
} from "../../../api/types";
import {
  buildKnowledgeItems,
  knowledgeKindLabel,
  knowledgeSourceStatus,
  knowledgeStatusLabel,
  summarizeKnowledge,
} from "./presentation";

const now = "2026-08-12T10:00:00Z";

const source = (overrides: Partial<WikiSourceInfo> = {}): WikiSourceInfo => ({
  filename: "guide.pdf",
  path: ".wiki/raw/sources/files/guide.pdf",
  size: 1024,
  created_time: now,
  modified_time: now,
  updated_at: Date.parse(now),
  kind: "binary",
  extension: ".pdf",
  sha256: "hash",
  original_path: "guide.pdf",
  linked_pages: ["sources/guide.md"],
  status: "ready",
  extraction_status: "text_extracted",
  ...overrides,
});

const page = (overrides: Partial<WikiPageSummary> = {}): WikiPageSummary => ({
  filename: "note.md",
  path: ".wiki/pages/note.md",
  size: 128,
  created_time: now,
  modified_time: now,
  updated_at: Date.parse(now),
  title: "研究笔记",
  summary: "一条可复用的研究结论",
  links: [],
  backlinks: [],
  sources: [],
  page_type: "page",
  category: "root",
  ...overrides,
});

const emptyQueue: WikiQueueSummary = {
  pending: 0,
  running: 0,
  finished: 0,
  failed: 0,
  cancelled: 0,
  total: 0,
  latest: [],
};

describe("knowledge workspace presentation", () => {
  it("maps internal source states to four user-facing states", () => {
    expect(knowledgeSourceStatus(source())).toBe("ready");
    expect(knowledgeSourceStatus(source({ status: "running" }))).toBe(
      "processing",
    );
    expect(
      knowledgeSourceStatus(source({ extraction_status: "metadata_only" })),
    ).toBe("attention");
    expect(knowledgeSourceStatus(source({ semantic_status: "failed" }))).toBe(
      "attention",
    );
    expect(
      knowledgeSourceStatus(
        source({
          linked_pages: [],
          extraction_status: "unknown",
          extracted_chars: 0,
        }),
      ),
    ).toBe("saved");
    expect(
      knowledgeSourceStatus(
        source({
          extraction_status: "metadata_only",
          extracted_chars: 0,
          semantic_status: "local_structured",
        }),
      ),
    ).toBe("ready");
  });

  it("merges sources and pages while hiding duplicate generated source pages", () => {
    const importedSource = source({
      linked_pages: ["sources/guide.md"],
    });
    const items = buildKnowledgeItems(
      [
        page({ filename: "sources/guide.md", category: "sources" }),
        page({ filename: "queries/answer.md", category: "queries" }),
      ],
      [importedSource],
    );

    expect(items).toHaveLength(2);
    expect(items.map((item) => item.id)).toEqual(
      expect.arrayContaining([
        "source:.wiki/raw/sources/files/guide.pdf",
        "page:queries/answer.md",
      ]),
    );
    expect(
      knowledgeKindLabel(items.find((item) => item.kind === "page")!),
    ).toBe("对话整理");
  });

  it("uses semantic content instead of a generated technical filename", () => {
    const [item] = buildKnowledgeItems(
      [],
      [
        source({
          filename: "mp.weixin.qq.com-e5aac58c.md",
          original_path: "https://mp.weixin.qq.com/s/example",
          kind: "web",
          semantic_summary:
            "让 Agent 读懂业务，办公产品开始进入应用生成阶段。后续补充。",
        }),
      ],
    );

    expect(item.title).toBe("让 Agent 读懂业务，办公产品开始进入应用生成阶段");
    expect(item.description).toContain("后续补充");
  });

  it("replaces a conversational page preamble with its useful summary", () => {
    const [item] = buildKnowledgeItems(
      [
        page({
          title: "搜了一下，应该是这一件：",
          summary: "OpenAI 发布了一份新的智能体协作说明。更多内容。",
        }),
      ],
      [],
    );

    expect(item.title).toBe("OpenAI 发布了一份新的智能体协作说明");
  });

  it("surfaces queue, failure and pending-confirmation counts without jargon", () => {
    const items = buildKnowledgeItems([page()], [source({ status: "failed" })]);
    const summary = summarizeKnowledge(
      items,
      { ...emptyQueue, pending: 2 },
      { items: [], stats: { pending: 3, total: 3 } },
    );

    expect(summary).toMatchObject({
      ready: 1,
      saved: 0,
      processing: 2,
      attention: 3,
      failed: 1,
      total: 2,
    });
    expect(knowledgeStatusLabel("processing")).toBe("正在整理");
    expect(knowledgeStatusLabel("saved")).toBe("已保存，待整理");
    expect(knowledgeStatusLabel("failed")).toBe("整理失败");
  });
});
