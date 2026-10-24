import { describe, expect, it } from "vitest";
import {
  buildMemoryEngineStatusView,
  buildMemoryImpressionView,
  buildMemoryOverviewView,
  isUserFacingMemoryEntry,
  looksLikeAssistantMetaMemory,
  looksLikeInternalMemory,
  pickPrimaryManualEntry,
  toConfidenceLabel,
  toDisplayChip,
  toDisplayEvidence,
  toReviewDisplaySentence,
  toDisplaySentence,
  toUserFacingSourceTitle,
} from "./presentation";

describe("memory presentation helpers", () => {
  it("keeps lead sentence readable", () => {
    expect(
      toDisplaySentence(
        "用户不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制。",
      ),
    ).toBe("用户不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制");
  });

  it("compresses workflow chips", () => {
    expect(
      toDisplayChip(
        "当前工作目录是 ~/Downloads/未命名文件夹，目录里已有 memory.md / MEMORY.md。",
        "workflow",
      ),
    ).toContain("目录已有 memory.md");
  });

  it("removes imported profile-section prefixes without leaving broken grammar", () => {
    const imported =
      "用户的长期偏好：长期横跨高校艺术设计教育、学术研究与产品设计。";

    expect(toDisplaySentence(imported, "identity")).toBe(
      "长期横跨高校艺术设计教育、学术研究与产品设计",
    );
    expect(toDisplayChip(imported, "identity")).toBe(
      "长期横跨高校艺术设计教育、学术研究与产品设计",
    );
    expect(toDisplayChip(imported, "identity")).not.toContain("的长期偏好");
  });

  it("humanizes local file paths before showing them to users", () => {
    const text =
      "安装这个技能 用户上传文件，已经下载到 /Users/example/Library/Containers/com.lantai.appstore/Data/Library/Application Support/lantai-console/working/workspaces/default/media/wps365_skill_20260511_090436.zip。";

    expect(looksLikeInternalMemory(text)).toBe(true);
    expect(toDisplaySentence(text, "identity")).toBe(
      "用户曾上传「wps365_skill_20260511_090436.zip」这类技能文件，希望澜台能完成安装和预装",
    );
  });

  it("does not let technical path memories become the profile headline", () => {
    const view = buildMemoryImpressionView(
      "identity",
      ["用户长期负责美股日报复盘和科技成长股研究。"],
      [
        {
          id: "1",
          type: "identity",
          content:
            "安装这个技能 用户上传文件，已经下载到 /Users/example/Library/Application Support/lantai-console/media/wps365_skill.zip。",
          source: "auto",
          status: "active",
          confidence: 0.95,
          reason: "",
          created_at: "",
          updated_at: "",
        },
        {
          id: "2",
          type: "identity",
          content:
            "沈行知身份角色背景：记者出身，后转公关/品牌营销/热点传播策略，曾带过辩论训练。",
          source: "auto",
          status: "active",
          confidence: 0.9,
          reason: "",
          created_at: "",
          updated_at: "",
        },
      ],
    );

    expect(view.lead).toContain("美股日报复盘");
    expect(view.lead).not.toContain("/Users/");
    expect(view.lead).not.toContain("沈行知");
  });

  it("keeps operational traces out of user-facing identity memories", () => {
    expect(
      isUserFacingMemoryEntry({
        id: "trace",
        type: "identity",
        content:
          "安装这个技能 用户上传文件，已经下载到 /Users/example/Library/Application Support/lantai-console/media/wps365_skill.zip。",
        source: "auto",
        status: "active",
        confidence: 0.95,
        reason: "",
        created_at: "",
        updated_at: "",
      }),
    ).toBe(false);

    expect(
      isUserFacingMemoryEntry({
        id: "identity",
        type: "identity",
        content:
          "沈行知身份角色背景：记者出身，后转公关/品牌营销/热点传播策略。",
        source: "auto",
        status: "active",
        confidence: 0.9,
        reason: "",
        created_at: "",
        updated_at: "",
      }),
    ).toBe(false);
  });

  it("recognizes assistant meta memories and keeps them out of the profile", () => {
    expect(
      looksLikeAssistantMetaMemory(
        "澜台中'行知'是实际用户的名字，'沈行知'是助手身份名。",
      ),
    ).toBe(true);
    expect(
      isUserFacingMemoryEntry({
        id: "manual-assistant-meta",
        type: "identity",
        content: "沈行知是澜台的默认工作伙伴身份名。",
        source: "manual",
        status: "active",
        confidence: 1,
        reason: "",
        created_at: "",
        updated_at: "",
      }),
    ).toBe(false);
  });

  it("hides raw session ids in source labels", () => {
    expect(
      toUserFacingSourceTitle({
        id: "1",
        type: "identity",
        content: "用户是一名日报分析师。",
        source: "auto",
        status: "active",
        confidence: 0.92,
        reason: "",
        created_at: "",
        updated_at: "",
        source_kind: "chat",
        source_title: "会话 1779101350466",
      }),
    ).toBe("最近一轮聊天");
  });

  it("turns low confidence into a human label", () => {
    expect(
      toConfidenceLabel({
        id: "1",
        type: "instruction",
        content: "默认用中文回答。",
        source: "auto",
        status: "active",
        confidence: 0.61,
        reason: "",
        created_at: "",
        updated_at: "",
      }),
    ).toBe("建议确认");
  });

  it("rewrites review copy into confirmable human language", () => {
    expect(
      toReviewDisplaySentence(
        "用户不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制。",
        "style",
      ),
    ).toBe(
      "澜台准备记住：你不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制",
    );
  });

  it("sanitizes evidence for user-facing details", () => {
    expect(
      toDisplayEvidence(
        "文件已经保存到 /Users/example/Library/Application Support/lantai-console/backend.log，可继续排查。",
        "workflow",
      ),
    ).toContain("文件是否已落地");
  });

  it("builds impression view from summary and entries", () => {
    const view = buildMemoryImpressionView(
      "style",
      [
        "用户不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制。",
        "用户偏好使用中文交流。",
        "用户希望默认少一点催促感。",
      ],
      [
        {
          id: "1",
          type: "style",
          content: "用户不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制。",
          source: "auto",
          status: "active",
          confidence: 0.9,
          reason: "",
          created_at: "",
          updated_at: "",
        },
        {
          id: "2",
          type: "style",
          content: "用户偏好使用中文交流。",
          source: "manual",
          status: "active",
          confidence: 1,
          reason: "",
          created_at: "",
          updated_at: "",
        },
      ],
    );

    expect(view.lead).toContain("用户不喜欢");
    expect(view.chips[0]).toBe("偏好使用中文交流");
    expect(view.chips[1]).toContain("默认少一点催促感");
    expect(view.stats).toBe("1 条手动补充 · 1 条澜台整理");
  });

  it("builds a top-level memory overview", () => {
    const overview = buildMemoryOverviewView(
      {
        identity: {
          lead: "用户长期在北京工作，已婚。",
          chips: ["长期在北京工作"],
          stats: "自动 1",
        },
        workflow: {
          lead: "当前工作目录是 ~/Downloads/未命名文件夹，目录里已有 memory.md / MEMORY.md。",
          chips: ["目录已有 memory.md"],
          stats: "自动 2",
        },
        style: {
          lead: "用户不喜欢用“有事直接说”这类话收尾，希望语气更自然、克制。",
          chips: ["偏好使用中文交流"],
          stats: "手动 1 · 自动 1",
        },
        instruction: {
          lead: "默认规则：回答尽量直接，不要反复确认。",
          chips: ["回答尽量直接"],
          stats: "自动 2",
        },
      },
      {
        identity: "身份记忆",
        workflow: "工作方式",
        style: "表达偏好",
        instruction: "用户指令",
      },
    );

    expect(overview.headline).toContain("已婚");
    expect(overview.narrative[0]).toContain("不喜欢");
    expect(overview.highlights).toHaveLength(4);
    expect(overview.chips).toContain("回答尽量直接");
  });

  it("builds a user-safe engine status view", () => {
    const view = buildMemoryEngineStatusView(
      {
        entries: [],
        stats: {
          total: 2,
          auto: 2,
          manual: 0,
          conflict: 1,
          by_type: {
            identity: 1,
            workflow: 1,
            style: 0,
            instruction: 0,
          },
        },
        profile_summary: {
          summary: "",
        },
      },
      {
        stats: {
          l0_conversations: 4,
          l1_atoms: 8,
          l2_scenes: 2,
          offload_entries: 0,
          system2_models: 1,
          open_intentions: 0,
        },
        atoms: [],
        scenes: [],
        persona: "",
        offload: [],
        storage_path: "",
        updated_at: "",
        engine_status: {
          overall: "partial",
          structured_memory: {
            status: "已就绪",
            visible_entries: 2,
            pending_entries: 1,
            total_entries: 2,
          },
          vector_index: {
            enabled: false,
            status: "未配置",
          },
          embedding: {
            enabled: false,
            status: "未配置",
          },
        },
      },
    );

    expect(view.overallLabel).toBe("部分可用");
    expect(view.summary).toContain("1 条待确认记忆");
    expect(view.rows[0]).toMatchObject({
      key: "structured",
      metric: "2",
      metricLabel: "条记忆",
    });
    expect(view.rows[1]).toMatchObject({
      key: "layered",
      metric: "4",
      metricLabel: "层有内容",
    });
    expect(view.rows[2].detail).toContain("结构化与分层记忆");
    expect(view.rows[2]).toMatchObject({
      metric: "—",
      metricLabel: "语义索引",
    });
  });

  it("does not report the engine ready before runtime status arrives", () => {
    const view = buildMemoryEngineStatusView(
      {
        entries: [],
        stats: {
          total: 0,
          auto: 0,
          manual: 0,
          conflict: 0,
          by_type: { identity: 0, workflow: 0, style: 0, instruction: 0 },
        },
        profile_summary: { summary: "" },
      },
      {
        stats: {
          l0_conversations: 0,
          l1_atoms: 0,
          l2_scenes: 0,
          offload_entries: 0,
          system2_models: 0,
          open_intentions: 0,
        },
        atoms: [],
        scenes: [],
        persona: "",
        offload: [],
        storage_path: "",
        updated_at: "",
      },
    );

    expect(view.summary).toBe("正在读取本机记忆引擎状态。");
    expect(view.rows[0]).toMatchObject({ status: "等待状态", tone: "partial" });
    expect(view.rows[1]).toMatchObject({ status: "等待状态", tone: "partial" });
  });

  it("explains a probed vector failure and the lexical fallback", () => {
    const view = buildMemoryEngineStatusView(
      {
        entries: [],
        stats: { total: 0, active: 0, conflict: 0, deprecated: 0 },
        updated_at: "",
      },
      {
        stats: {
          l0_conversations: 0,
          l1_atoms: 0,
          l2_scenes: 0,
          offload_entries: 0,
          system2_models: 0,
          open_intentions: 0,
        },
        atoms: [],
        scenes: [],
        persona: "",
        offload: [],
        storage_path: "",
        updated_at: "",
        engine_status: {
          overall: "partial",
          vector_index: {
            enabled: false,
            configured: true,
            status: "不可用",
            error: "embedding model checksum mismatch",
            probe: { write: false, query: false, delete: false },
          },
          embedding: {
            enabled: false,
            configured: true,
            status: "不可用",
            error: "embedding model checksum mismatch",
          },
        },
      },
    );

    expect(view.rows[2].tone).toBe("partial");
    expect(view.rows[2].detail).toContain("关键词检索");
    expect(view.rows[2].detail).toContain("checksum mismatch");
    expect(view.rows[3].detail).toContain("checksum mismatch");
  });

  it("prefers the latest active manual entry for persona editing", () => {
    const picked = pickPrimaryManualEntry(
      [
        {
          id: "1",
          type: "instruction",
          content: "默认规则：先给结论。",
          source: "auto",
          status: "active",
          confidence: 0.8,
          reason: "",
          created_at: "",
          updated_at: "2026-05-24T00:00:00Z",
        },
        {
          id: "2",
          type: "instruction",
          content: "默认规则：少一点确认。",
          source: "manual",
          status: "active",
          confidence: 1,
          reason: "",
          created_at: "",
          updated_at: "2026-05-24T00:00:01Z",
        },
      ],
      "instruction",
    );

    expect(picked?.id).toBe("2");
  });
});
