import { describe, expect, it } from "vitest";

import type { UtilityClassifiedArtifact } from "@/api/types";
import { splitStandaloneTextArtifacts } from "./StructuredTextArtifacts";

function classifiedArtifact(
  content: string,
  sourceText: string,
  title: string,
  kind: UtilityClassifiedArtifact["kind"] = "creative",
): UtilityClassifiedArtifact {
  const start = content.indexOf(sourceText);
  return {
    kind,
    title,
    source_text: sourceText,
    start,
    end: start + sourceText.length,
  };
}

describe("splitStandaloneTextArtifacts", () => {
  it("keeps semantic-looking text unchanged without an intelligent verdict", () => {
    const content = `好，再来一首五言绝句

《溪边独坐》

流水过青苔，
斜阳入酒杯。

还要再来一首吗？`;

    expect(splitStandaloneTextArtifacts(content)).toEqual([
      { type: "markdown", content },
    ]);
  });

  it("uses the exact classified source without swallowing adjacent prose", () => {
    const content = `好，再来一首五言绝句

《溪边独坐》

流水过青苔，

斜阳入酒杯。

山风知我意，

先遣落花来。

——押“苔、杯、来”韵，写溪边独坐、闲适自得。

还要再来一首吗？`;
    const sourceText = `《溪边独坐》

流水过青苔，

斜阳入酒杯。

山风知我意，

先遣落花来。`;

    expect(
      splitStandaloneTextArtifacts(content, [
        classifiedArtifact(content, sourceText, "溪边独坐"),
      ]),
    ).toEqual([
      {
        type: "markdown",
        content: "好，再来一首五言绝句",
      },
      {
        type: "artifact",
        kind: "creative",
        label: "作品",
        title: "溪边独坐",
        body: `流水过青苔，

斜阳入酒杯。

山风知我意，

先遣落花来。`,
        copyContent: sourceText,
        fileExtension: "txt",
      },
      {
        type: "markdown",
        content: `——押“苔、杯、来”韵，写溪边独坐、闲适自得。

还要再来一首吗？`,
      },
    ]);
  });

  it("supports short works without a length threshold", () => {
    const content = "《无题》\n\n风来。";
    const segments = splitStandaloneTextArtifacts(content, [
      classifiedArtifact(content, content, "无题"),
    ]);

    expect(segments[0]).toMatchObject({
      type: "artifact",
      kind: "creative",
      title: "无题",
      body: "风来。",
    });
  });

  it("removes orphan dividers and a duplicate title beside an artifact card", () => {
    const sourceText = `你不必再教我一遍。
那些反复说过的偏好，我都记得。`;
    const content = `可以。给灵犀专业版写一首：

---

**灵犀**

${sourceText}

---

写产品诗比写新闻稿难。`;

    expect(
      splitStandaloneTextArtifacts(content, [
        classifiedArtifact(content, sourceText, "灵犀"),
      ]),
    ).toEqual([
      {
        type: "markdown",
        content: "可以。给灵犀专业版写一首：",
      },
      {
        type: "artifact",
        kind: "creative",
        label: "作品",
        title: "灵犀",
        body: sourceText,
        copyContent: sourceText,
        fileExtension: "txt",
      },
      {
        type: "markdown",
        content: "写产品诗比写新闻稿难。",
      },
    ]);
  });

  it("keeps horizontal rules when no artifact card is created", () => {
    const content = "前言\n\n---\n\n正文";

    expect(splitStandaloneTextArtifacts(content)).toEqual([
      { type: "markdown", content },
    ]);
  });

  it.each([
    ["correspondence", "信函", "主题：收到\n\n好的。", "收到"],
    ["copy", "文案", "周末还给自己。", "周末文案"],
    ["script", "脚本", "各位来宾，大家下午好。", "开场主持稿"],
  ] as const)(
    "renders a validated %s classification",
    (kind, label, sourceText, title) => {
      const content = `这里是成品：\n\n${sourceText}\n\n需要调整再告诉我。`;
      const artifact = splitStandaloneTextArtifacts(content, [
        classifiedArtifact(content, sourceText, title, kind),
      ]).find((segment) => segment.type === "artifact");

      expect(artifact).toMatchObject({ kind, label, title });
    },
  );

  it("rejects a verdict whose offsets do not reproduce the original source", () => {
    const content = "《夜航》\n\n江灯摇碎一船星。";
    const verdict = classifiedArtifact(content, content, "夜航");
    verdict.end -= 1;

    expect(splitStandaloneTextArtifacts(content, [verdict])).toEqual([
      { type: "markdown", content },
    ]);
  });

  it("rejects ambiguous repeated source text", () => {
    const content = "风来。\n\n风来。";
    const verdict: UtilityClassifiedArtifact = {
      kind: "creative",
      title: "无题",
      source_text: "风来。",
      start: 0,
      end: 3,
    };

    expect(splitStandaloneTextArtifacts(content, [verdict])).toEqual([
      { type: "markdown", content },
    ]);
  });

  it("supports multiple non-overlapping classified works", () => {
    const first = "《夜航》\n\n江灯摇碎一船星。";
    const second = "《山居》\n\n一帘月色落琴弦。";
    const content = `先给你两版：\n\n${first}\n\n${second}`;
    const artifacts = splitStandaloneTextArtifacts(content, [
      classifiedArtifact(content, first, "夜航"),
      classifiedArtifact(content, second, "山居"),
    ]).filter((segment) => segment.type === "artifact");

    expect(artifacts.map((artifact) => artifact.title)).toEqual([
      "夜航",
      "山居",
    ]);
  });
});
