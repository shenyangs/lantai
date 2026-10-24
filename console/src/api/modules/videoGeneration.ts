import { request } from "../request";
import type { VideoGenerationConfig } from "../types/videoGeneration";

export const videoGenerationApi = {
  getVideoGenerationConfig: () =>
    request<VideoGenerationConfig>("/video-generation/config"),

  updateVideoGenerationConfig: (body: VideoGenerationConfig) =>
    request<VideoGenerationConfig>("/video-generation/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
