import { describe, expect, it } from "vitest";
import {
  isChatCapableModel,
  isImageGenerationModel,
  isTextToVideoModel,
  isVideoGenerationModel,
} from "./modelProtocol";

describe("modelProtocol", () => {
  it("keeps legacy models eligible without catalog protocol metadata", () => {
    expect(
      isChatCapableModel({ supported_protocols: [], show_in_chat: true }),
    ).toBe(true);
    expect(isImageGenerationModel({ supported_protocols: [] })).toBe(true);
  });

  it("requires explicit chat visibility without treating media as chat", () => {
    expect(
      isChatCapableModel({
        supported_protocols: ["openai:responses"],
        show_in_chat: false,
      }),
    ).toBe(false);
    expect(
      isChatCapableModel({
        supported_protocols: ["seedance:generations"],
        show_in_chat: true,
      }),
    ).toBe(false);
    expect(
      isVideoGenerationModel({
        supported_protocols: ["seedance:generations"],
      }),
    ).toBe(true);
    expect(
      isTextToVideoModel({
        id: "happyhorse-1.1-i2v",
        supported_protocols: ["happyhorse:video-synthesis"],
      }),
    ).toBe(false);
    expect(
      isTextToVideoModel({
        id: "happyhorse-1.1-t2v",
        supported_protocols: ["happyhorse:video-synthesis"],
      }),
    ).toBe(true);
  });

  it("separates TokenDance chat and image models", () => {
    expect(
      isChatCapableModel({ supported_protocols: ["openai:responses"] }),
    ).toBe(true);
    expect(
      isChatCapableModel({ supported_protocols: ["ark:image-generations"] }),
    ).toBe(false);
    expect(
      isImageGenerationModel({
        supported_protocols: ["ark:image-generations"],
      }),
    ).toBe(true);
    expect(
      isImageGenerationModel({
        supported_protocols: ["openai:chat-completions"],
      }),
    ).toBe(false);
  });
});
