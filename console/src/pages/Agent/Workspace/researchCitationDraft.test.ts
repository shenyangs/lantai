import { expect, it } from "vitest";
import { codepointSlice, selectionSpan, rebaseClaims } from "./researchCitationDraft";
const evidence = [{ source_version_id: "s1", start: 6, end: 9, quote: "证据文" }];
it("uses backend codepoint offsets for emoji and the selected duplicate occurrence", () => {
  const text = "😀相同 结论 相同";
  expect(selectionSpan(text, 8, 10)).toEqual({ start: 7, end: 9 });
  expect(codepointSlice(text, 7, 9)).toBe("相同");
});
it("shifts untouched anchors and drops only anchors crossing an edit", () => {
  const claims = [{ start: 2, end: 4, evidence }, { start: 5, end: 7, evidence }];
  expect(rebaseClaims("前😀结论 后文", "新增前😀结论 后文", claims)).toEqual([
    { start: 4, end: 6, evidence }, { start: 7, end: 9, evidence },
  ]);
  expect(rebaseClaims("前😀结论 后文", "前😀改论 后文", claims)).toEqual([claims[1]]);
  expect(rebaseClaims("前😀结论 后文", "前😀结论新 后文", claims)).toEqual([
    claims[0], { start: 6, end: 8, evidence },
  ]);
  expect(rebaseClaims("前😀结论 后文", "", claims)).toEqual([]);
});
