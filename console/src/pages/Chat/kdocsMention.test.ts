import { describe, expect, it } from "vitest";
import { matchKdocsMention, replaceKdocsMention } from "./kdocsMention";

describe("KDocs @ mention parsing", () => {
  it("opens after a sentence boundary and preserves surrounding draft text", () => {
    const value = "请比较 @周报";
    const match = matchKdocsMention(value, value.length);
    expect(match).toEqual({ start: 4, end: value.length, query: "周报" });
    const replacement = replaceKdocsMention(value, match!, "经营周报.docx");
    expect(replacement.value).toBe("请比较 @《经营周报.docx》 ");
    expect(replacement.cursor).toBe(replacement.value.length);
  });

  it("does not treat email addresses or a second at sign as document mentions", () => {
    expect(matchKdocsMention("sam@example.com", 15)).toBeNull();
    expect(matchKdocsMention("请看@@周报", 6)).toBeNull();
  });
});
