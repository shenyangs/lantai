import { describe, expect, it } from "vitest";
import { parseQuquBlocks, stripQuquFromArtifactContent } from "./ququ";

describe("parseQuquBlocks", () => {
  it("parses semantic lead and echo placement markers", () => {
    expect(
      parseQuquBlocks(
        "~>[lead] 我先把这道题的边界锁死。\n\n# 正文\n\n~>[echo] 这道题真正难的是别把苦难说轻了。",
      ),
    ).toEqual([
      {
        type: "ququ",
        placement: "lead",
        text: "我先把这道题的边界锁死。",
      },
      { type: "markdown", text: "\n# 正文\n" },
      {
        type: "ququ",
        placement: "echo",
        text: "这道题真正难的是别把苦难说轻了。",
      },
    ]);
  });

  it("splits assistant markdown and ququ lines into separate blocks", () => {
    expect(
      parseQuquBlocks(
        "# 标题\n\n正文第一段\n~>这个报错看起来普通，八成藏在输入形状里\n- item",
      ),
    ).toEqual([
      { type: "markdown", text: "# 标题\n\n正文第一段" },
      {
        type: "ququ",
        placement: "echo",
        text: "这个报错看起来普通，八成藏在输入形状里",
      },
      { type: "markdown", text: "- item" },
    ]);
  });

  it("hides nested protocol markers from previously persisted ququ", () => {
    expect(parseQuquBlocks("~>[echo] ~>PPT还没动手，提问的人刚说完困。"))
      .toEqual([
        {
          type: "ququ",
          placement: "echo",
          text: "PPT还没动手，提问的人刚说完困。",
        },
      ]);
  });

  it("does not treat normal markdown blockquotes as ququ", () => {
    expect(parseQuquBlocks("> 引用\n\n正文")).toEqual([
      { type: "markdown", text: "> 引用\n\n正文" },
    ]);
  });

  it("keeps ququ-looking text inside fenced code as markdown", () => {
    expect(parseQuquBlocks("```txt\n~>not ququ\n```\n~>真蛐蛐")).toEqual([
      { type: "markdown", text: "```txt\n~>not ququ\n```" },
      { type: "ququ", placement: "echo", text: "真蛐蛐" },
    ]);
  });

  it("removes only real ququ lines from artifact content", () => {
    expect(
      stripQuquFromArtifactContent(
        "~>产物外旁白\n\n# 正文\n\n```txt\n~>代码里的原文\n```\n\n~>另一句旁白",
      ),
    ).toBe("# 正文\n\n```txt\n~>代码里的原文\n```");
  });
});
