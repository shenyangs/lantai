import { describe, expect, it } from "vitest";
import type {
  LayeredMemorySnapshot,
  UserMemorySnapshot,
  UserUnderstandingSnapshot,
} from "../../../api/types";
import {
  buildMemoryChanges,
  buildMemoryTopics,
  filterMemoryTopics,
} from "./model";

const snapshot: UserMemorySnapshot = {
  entries: [
    {
      id: "style-1",
      type: "style",
      content: "用户的长期偏好：偏好克制、直接的中文表达。",
      source: "agent",
      status: "active",
      confidence: 0.96,
      reason: "",
      created_at: "2026-09-01T00:00:00Z",
      updated_at: "2026-09-05T08:00:00Z",
    },
    {
      id: "style-archived",
      type: "style",
      content: "已经停止使用的旧偏好。",
      source: "agent",
      status: "archived",
      confidence: 0.9,
      reason: "",
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-02T00:00:00Z",
    },
  ],
  stats: {
    total: 1,
    auto: 1,
    manual: 0,
    conflict: 0,
    by_type: { identity: 0, workflow: 0, style: 1, instruction: 0 },
  },
  profile_summary: {
    summary: "",
    by_type: { identity: [], workflow: [], style: [], instruction: [] },
  },
};

const layered: LayeredMemorySnapshot = {
  stats: {
    l0_conversations: 1,
    l1_atoms: 2,
    l2_scenes: 1,
    offload_entries: 0,
  },
  atoms: [],
  scenes: [
    {
      scene_name: "book",
      title: "小说投稿",
      path: "/memory/book.md",
      atom_count: 2,
      updated_at: "2026-09-05T09:00:00Z",
      sample: ["目前正在准备投稿。", "最终稿已经完成。"],
    },
  ],
  persona: "",
  system2: {
    models: [
      {
        key: "decision",
        title: "决策方式",
        summary: "需要明确推荐和理由。",
        evidence_count: 3,
        updated_at: "2026-09-05T07:00:00Z",
      },
    ],
    open_intentions: [],
    evolution_chains: [],
  },
  offload: [],
  storage_path: "",
  updated_at: "2026-09-05T09:00:00Z",
};

const understanding: UserUnderstandingSnapshot = {
  confirmed: [],
  hypotheses: [],
  open_loops: [],
  stats: { confirmed: 0, hypotheses: 0, open_loops: 0, retired: 0 },
};

describe("memory topic model", () => {
  it("maps legacy scene corrections to a distinct canonical scope identifier", () => {
    const atom = {
      id: "a",
      type: "identity",
      content: "Atlas 项目资料",
      priority: 2,
      confidence: 1,
      scene_name: "api",
      source_message_ids: [],
      source_record_ids: [],
      evidence: "",
      created_at: "",
      updated_at: "",
      status: "active",
      metadata: {},
    };
    const input: UserMemorySnapshot = {
      ...snapshot,
      entries: [
        {
          ...snapshot.entries[0],
          id: "atlas",
          content: atom.content,
          subject: "project",
          scope: "project",
          scope_id: "atlas",
          canonical_atom_ids: ["a"],
        },
        {
          ...snapshot.entries[0],
          id: "fix",
          content: "已纠正的 Atlas 资料",
          reason: "用户纠正主题:project:api|接口资料",
        },
      ],
    };
    const topics = buildMemoryTopics(
      input,
      {
        ...layered,
        atoms: [atom],
        scenes: [
          {
            scene_name: "api",
            title: "接口资料",
            path: "",
            atom_count: 1,
            updated_at: "",
            sample: [atom.content],
          },
        ],
        system2: undefined,
      },
      understanding,
    );
    expect(topics).toHaveLength(1);
    expect(topics[0]).toMatchObject({
      id: "project:atlas",
      kind: "project",
      summary: "已纠正的 Atlas 资料",
    });
  });

  it("keeps identically titled projects in separate scopes", () => {
    const topics = buildMemoryTopics(
      {
        ...snapshot,
        entries: ["a", "b"].map((id) => ({
          ...snapshot.entries[0],
          id,
          scope: "project",
          scope_id: id,
          subject: "project",
        })),
      },
      { ...layered, scenes: [], system2: undefined },
      understanding,
    );
    expect(topics.map((topic) => topic.id).sort()).toEqual([
      "project:a",
      "project:b",
    ]);
  });

  it("merges distinct personal evidence rather than dropping the whole System2 group", () => {
    const topics = buildMemoryTopics(
      snapshot,
      {
        ...layered,
        scenes: [],
        system2: {
          models: [
            {
              key: "communication_model",
              title: "表达偏好",
              summary: "偏好英文引用",
              sample: ["偏好英文引用"],
            },
          ],
        },
      },
      understanding,
    );
    expect(topics.filter((topic) => topic.kind === "profile")).toHaveLength(1);
    expect(
      topics.find((topic) => topic.id === "profile:style")?.details.join(" "),
    ).toContain("英文引用");
  });

  it("keeps thread scenes in one topic and preserves old scene corrections after reclassification", () => {
    const atom = {
      id: "a",
      type: "style",
      content: "旧资料",
      priority: 2,
      confidence: 1,
      scene_name: "client",
      source_message_ids: [],
      source_record_ids: [],
      evidence: "",
      created_at: "",
      updated_at: "",
      status: "active",
      metadata: { subject: "user", scope: "thread" },
    };
    const scene = {
      scene_name: "client",
      title: "客户事项",
      path: "",
      atom_count: 1,
      updated_at: "",
      sample: ["旧资料"],
    };
    const input: UserMemorySnapshot = {
      ...snapshot,
      entries: [
        {
          ...snapshot.entries[0],
          content: "旧资料",
          subject: "user",
          scope: "thread",
          scope_id: "client",
          canonical_atom_ids: ["a"],
        },
      ],
    };
    const scenes: LayeredMemorySnapshot = {
      ...layered,
      atoms: [atom],
      scenes: [scene],
      system2: undefined,
    };
    const topics = buildMemoryTopics(input, scenes, understanding);
    expect(topics).toHaveLength(1);
    expect(topics[0].id).toBe("experience:client");
    input.entries.push({
      ...snapshot.entries[0],
      id: "correction",
      content: "新资料",
      reason: "用户纠正主题:project:client|客户事项",
    });
    const corrected = buildMemoryTopics(input, scenes, understanding);
    expect(corrected).toHaveLength(1);
    expect(corrected[0].summary).toBe("新资料");
  });

  it("routes ownership and scope independently of memory type without losing evidence", () => {
    const base = snapshot.entries[0];
    const classified: UserMemorySnapshot = {
      ...snapshot,
      entries: [
        {
          ...base,
          id: "user",
          type: "identity",
          content: "用户是编辑",
          subject: "user",
          scope: "global",
          canonical_fact_id: "user-fact",
        },
        {
          ...base,
          id: "duplicate",
          type: "identity",
          content: "用户是编辑",
          subject: "user",
          scope: "global",
          canonical_fact_id: "user-fact",
        },
        {
          ...base,
          id: "api",
          type: "identity",
          content: "HUMAN API 提供开放接口",
          subject: "project",
          scope: "global",
        },
        {
          ...base,
          id: "cafe",
          type: "identity",
          content: "咖啡馆项目计划",
          subject: "project",
          scope: "project",
        },
        {
          ...base,
          id: "thread",
          type: "style",
          content: "这次需要详细展开",
          subject: "user",
          scope: "thread",
        },
        {
          ...base,
          id: "unknown",
          type: "identity",
          content: "归属还需核对",
          subject: "unknown",
          scope: "global",
        },
      ],
    };
    const topics = buildMemoryTopics(
      classified,
      { ...layered, scenes: [], system2: undefined },
      understanding,
    );
    expect(
      topics.find((topic) => topic.id === "profile:identity")?.evidenceCount,
    ).toBe(1);
    expect(
      topics
        .filter((topic) => topic.kind === "profile")
        .flatMap((topic) => topic.entries)
        .map((entry) => entry.id),
    ).toEqual(["user"]);
    expect(
      topics
        .filter((topic) => topic.kind === "project")
        .flatMap((topic) => topic.details)
        .join(" "),
    ).toContain("HUMAN API");
    expect(
      topics
        .filter((topic) => topic.kind === "experience")
        .flatMap((topic) => topic.details)
        .join(" "),
    ).toContain("这次需要详细展开");
    expect(topics.flatMap((topic) => topic.details).join(" ")).toContain(
      "归属还需核对",
    );
  });

  it("turns existing layers into user-facing topics without empty categories", () => {
    const topics = buildMemoryTopics(snapshot, layered, understanding);

    expect(topics.map((topic) => topic.title)).toEqual([
      "小说投稿",
      "你的表达偏好",
      "决策方式",
    ]);
    expect(
      topics.find((topic) => topic.title === "你的表达偏好")?.details,
    ).toEqual(["用户偏好克制、直接的中文表达"]);
    expect(topics.some((topic) => topic.title === "关于你")).toBe(false);
    expect(topics.some((topic) => topic.summary.includes("已经停止使用"))).toBe(
      false,
    );
  });

  it("searches topic titles and details without exposing storage categories", () => {
    const topics = buildMemoryTopics(snapshot, layered, understanding);

    expect(
      filterMemoryTopics(topics, "最终稿", "all").map((topic) => topic.title),
    ).toEqual(["小说投稿"]);
    expect(
      filterMemoryTopics(topics, "", "theme").map((topic) => topic.title),
    ).toEqual(["决策方式"]);
  });

  it("binds an explicit correction to its aggregate topic without polluting profile", () => {
    const correctedSnapshot: UserMemorySnapshot = {
      ...snapshot,
      entries: [
        ...snapshot.entries,
        {
          id: "correction-1",
          type: "workflow",
          content: "小说已经完成定稿。",
          source: "manual",
          status: "active",
          confidence: 1,
          reason: "用户纠正主题:project:book|小说投稿",
          created_at: "2026-09-05T10:00:00Z",
          updated_at: "2026-09-05T10:00:00Z",
        },
      ],
    };

    const topics = buildMemoryTopics(correctedSnapshot, layered, understanding);
    const project = topics.find((topic) => topic.id === "project:book");

    expect(project).toMatchObject({
      summary: "小说已经完成定稿",
      details: ["小说已经完成定稿"],
    });
    expect(project?.entries.map((entry) => entry.id)).toEqual(["correction-1"]);
    expect(topics.some((topic) => topic.id === "profile:workflow")).toBe(false);

    const changes = buildMemoryChanges(correctedSnapshot, {
      enabled: false,
      schedule: "0 23 * * *",
      rem_enabled: true,
      min_score: 0.68,
      latest_run: null,
      runs: [],
      next_run_at: "",
      diary_path: "",
      diary_excerpt: "",
      safety: {
        confirmed_only_injection: true,
        assistant_and_tool_evidence_blocked: true,
        diary_is_promotion_source: false,
      },
    });
    expect(
      changes.find((change) => change.id.includes("correction-1")),
    ).toMatchObject({
      verb: "纠正了",
      title: "小说投稿",
      topicId: "project:book",
    });
  });

  it("keeps the semantic memory type when correcting every aggregate topic family", () => {
    const semanticLayered: LayeredMemorySnapshot = {
      ...layered,
      system2: {
        models: [
          {
            key: "user_profile",
            title: "用户画像",
            summary: "旧画像",
          },
          {
            key: "communication_model",
            title: "表达偏好",
            summary: "旧表达偏好",
          },
          {
            key: "standing_rules",
            title: "长期规则",
            summary: "旧长期规则",
          },
          {
            key: "workflow_model",
            title: "工作方式与项目模型",
            summary: "旧工作方式",
          },
        ],
        open_intentions: [],
        evolution_chains: [],
      },
    };
    const correctionMatrix = [
      ["theme:user_profile", "用户画像", "identity", "新画像"],
      ["theme:communication_model", "表达偏好", "style", "新表达偏好"],
      ["theme:standing_rules", "长期规则", "instruction", "新长期规则"],
      ["theme:workflow_model", "工作方式与项目模型", "workflow", "新工作方式"],
      ["project:book", "小说投稿", "workflow", "小说已完成投稿"],
    ] as const;
    const semanticSnapshot: UserMemorySnapshot = {
      ...snapshot,
      entries: correctionMatrix.map(
        ([topicId, title, type, content], index) => ({
          id: `matrix-correction-${index}`,
          type,
          content,
          source: "manual",
          status: "active",
          confidence: 1,
          reason: `用户纠正主题:${topicId}|${title}`,
          created_at: `2026-09-05T1${index}:00:00Z`,
          updated_at: `2026-09-05T1${index}:00:00Z`,
        }),
      ),
    };

    const topics = buildMemoryTopics(
      semanticSnapshot,
      semanticLayered,
      understanding,
    );

    correctionMatrix.forEach(([topicId, , type, content]) => {
      expect(topics.find((topic) => topic.id === topicId)).toMatchObject({
        correctionType: type,
        summary: content,
        details: [content],
      });
    });
  });

  it("builds recent changes from actual entry and organization records", () => {
    const changes = buildMemoryChanges(snapshot, {
      enabled: true,
      schedule: "0 23 * * *",
      rem_enabled: true,
      min_score: 0.68,
      scheduled: true,
      latest_run: {
        run_id: "dream-1",
        status: "completed",
        trigger: "scheduled",
        shadow: false,
        completed_at: "2026-09-05T10:00:00Z",
        counts: { reviewed: 4, eligible: 1, staged: 1, excluded: 3 },
        mutations: [
          {
            fact_id: "fact-1",
            after: { content: "以后正式方案先给明确推荐。" },
          },
        ],
      },
      runs: [],
      next_run_at: "",
      diary_path: "",
      diary_excerpt: "",
      safety: {
        confirmed_only_injection: true,
        assistant_and_tool_evidence_blocked: true,
        diary_is_promotion_source: false,
      },
    });

    expect(changes[0]).toMatchObject({
      verb: "送入核对",
      detail: "以后正式方案先给明确推荐。",
    });
    expect(changes.some((change) => change.verb === "停止使用")).toBe(true);
    expect(changes.find((change) => change.verb === "停止使用")).toMatchObject({
      reason: "你或系统已明确停止使用这条旧说法",
      impact: "以后回答不会主动采用；原始聊天和来源仍然保留",
    });
  });
});
