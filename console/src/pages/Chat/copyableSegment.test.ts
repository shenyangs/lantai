import { describe, expect, it } from "vitest";
import {
  looksLikeCommand,
  looksLikeReusableLongText,
  shouldOfferSegmentCopy,
  shouldRenderDenseSegment,
} from "./copyableSegment";

describe("copyable segment classification", () => {
  it("does not render normal long Chinese narration as a dense copy block", () => {
    const text =
      "江苏宜兴出生的，在无锡长大的。宜兴给我的东西很具体——紫砂、老街、手艺人，知道好东西要经得起看。无锡给我另一种东西——南长街的烟火气，太湖边的开阔，商业和日常混在一起的那种热闹。";

    expect(looksLikeReusableLongText(text)).toBe(false);
    expect(shouldOfferSegmentCopy(text)).toBe(true);
    expect(shouldRenderDenseSegment(text)).toBe(false);
  });

  it("keeps shell commands dense and copyable", () => {
    expect(looksLikeCommand("npm run build")).toBe(true);
    expect(shouldOfferSegmentCopy("npm run build")).toBe(true);
    expect(shouldRenderDenseSegment("npm run build")).toBe(true);
  });

  it("keeps reusable image prompts dense", () => {
    const text =
      "cinematic portrait, soft rim lighting, detailed background, editorial composition, warm color grading, 35mm render, high texture detail";

    expect(looksLikeReusableLongText(text)).toBe(true);
    expect(shouldRenderDenseSegment(text)).toBe(true);
  });
});
