import { request } from "../request";
import type { ImageGenerationConfig } from "../types/imageGeneration";

export const imageGenerationApi = {
  getImageGenerationConfig: () =>
    request<ImageGenerationConfig>("/image-generation/config"),

  updateImageGenerationConfig: (body: ImageGenerationConfig) =>
    request<ImageGenerationConfig>("/image-generation/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
};
