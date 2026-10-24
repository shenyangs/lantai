import { describe, expect, it } from "vitest";
import { parseDreamDiaryExcerpt } from "./dreamDiary";

describe("parseDreamDiaryExcerpt", () => {
  it("turns the internal diary markdown into user-facing records", () => {
    const entries = parseDreamDiaryExcerpt(`# 澜台梦境日记

> 这是供用户复核的整理报告。

## 2026-07-31T15:00:00Z · dream_internal_id

- 触发：scheduled
- 模式：受治理整理
- REM：已生成反思摘要
- 审阅：4 条
- 达标：1 条

### 候选
- [0.83] 用户偏好简洁表达（信息稳定）

### REM 反思（隔离内容）

> 以下内容只供用户浏览，不参与评分、检索或记忆晋升。

这轮记录的证据比较集中。

## 2026-08-01T03:32:50Z · dream_newer_id

- 触发：manual
- 模式：影子整理
- REM：已关闭
- 审阅：0 条
- 达标：0 条

### 候选
- 本轮没有达到晋升门槛的候选。
`);

    expect(entries).toHaveLength(2);
    expect(entries[0]).toMatchObject({
      completedAt: "2026-08-01T03:32:50Z",
      trigger: "手动整理",
      mode: "仅预览（未保存）",
      remStatus: "未生成整理说明",
      reviewed: 0,
      eligible: 0,
      candidates: [],
    });
    expect(entries[1].candidates[0]).toEqual({
      score: "0.83",
      content: "用户偏好简洁表达（信息稳定）",
    });
    expect(entries[1].reflection).toBe("这轮记录的证据比较集中。");
    expect(entries[1].mode).toBe("已整理");
    expect(entries[1].remStatus).toBe("已生成整理说明");
    expect(JSON.stringify(entries)).not.toContain("dream_internal_id");
  });

  it("ignores a truncated prefix and keeps complete records", () => {
    const entries = parseDreamDiaryExcerpt(`- 达标：0 条

### 候选
- 本轮没有达到晋升门槛的候选。

## 2026-08-01T04:00:00Z · dream_complete
- 触发：scheduled
- 审阅：2 条
- 达标：0 条
### 候选
- 本轮没有达到晋升门槛的候选。`);

    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({
      completedAt: "2026-08-01T04:00:00Z",
      trigger: "定时整理",
      reviewed: 2,
    });
  });
});
