import type { ModelInfo } from "../../../api/types/provider";

const CHAT_PROTOCOLS = new Set([
  "openai:chat-completions",
  "openai:responses",
  "anthropic:messages",
  "gemini:generate-content",
]);

const IMAGE_GENERATION_PROTOCOLS = new Set([
  "openai:image-generations",
  "ark:image-generations",
]);

const VIDEO_GENERATION_PROTOCOLS = new Set([
  "seedance:generations",
  "kling:text2video",
  "kling:image2video",
  "kling:omni-video",
  "wan3:video-synthesis",
  "happyhorse:video-synthesis",
  "minimax:video_generation_v2",
]);

export function isChatCapableModel(
  model: Pick<ModelInfo, "supported_protocols" | "show_in_chat">,
): boolean {
  return supportsChatProtocol(model) && model.show_in_chat !== false;
}

export function supportsChatProtocol(
  model: Pick<ModelInfo, "supported_protocols">,
): boolean {
  const protocols = model.supported_protocols ?? [];
  return (
    protocols.length === 0 ||
    protocols.some((value) => CHAT_PROTOCOLS.has(value))
  );
}

export function isImageGenerationModel(
  model: Pick<ModelInfo, "supported_protocols">,
): boolean {
  const protocols = model.supported_protocols ?? [];
  return (
    protocols.length === 0 ||
    protocols.some((value) => IMAGE_GENERATION_PROTOCOLS.has(value))
  );
}

export function isVideoGenerationModel(
  model: Pick<ModelInfo, "supported_protocols">,
): boolean {
  return (model.supported_protocols ?? []).some((value) =>
    VIDEO_GENERATION_PROTOCOLS.has(value),
  );
}

export function isTextToVideoModel(
  model: Pick<ModelInfo, "id" | "supported_protocols">,
): boolean {
  const protocols = model.supported_protocols ?? [];
  if (
    protocols.some((value) =>
      [
        "seedance:generations",
        "kling:text2video",
        "kling:omni-video",
        "wan3:video-synthesis",
        "minimax:video_generation_v2",
      ].includes(value),
    )
  ) {
    return true;
  }
  return (
    protocols.includes("happyhorse:video-synthesis") &&
    model.id.toLowerCase().includes("t2v")
  );
}
