import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import ImageGenerationToolCard from "./ImageGenerationToolCard";

const messageError = vi.hoisted(() => vi.fn());

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, values?: { index?: number }) => {
      const translations: Record<string, string> = {
        "chat.imageGeneration.title": "生成图片",
        "chat.imageGeneration.failed": "生成失败",
        "chat.imageGeneration.generating": "生成中",
        "chat.imageGeneration.completed": "生成完成",
        "chat.imageGeneration.generatingImage": "正在生成图片...",
        "chat.imageGeneration.waitHint": "通常需要 10-30 秒",
        "chat.imageGeneration.failedTitle": "图片生成失败",
        "chat.imageGeneration.emptyResult": "模型没有返回图片。",
        "common.copy": "复制",
        "common.download": "下载",
      };
      if (key === "chat.imageGeneration.imageAlt") {
        return `生成图片 ${values?.index ?? ""}`.trim();
      }
      return translations[key] ?? key;
    },
  }),
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: { error: messageError },
  }),
}));

describe("ImageGenerationToolCard", () => {
  afterEach(() => {
    delete window.electronAPI;
    messageError.mockClear();
    vi.restoreAllMocks();
  });

  it("keeps a stable generation stage while the tool is running", () => {
    render(
      <ImageGenerationToolCard
        data={{
          status: "running",
          content: [
            {
              data: {
                name: "generate_image",
                arguments: { prompt: "月光下的山谷" },
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("正在生成图片...")).toBeInTheDocument();
    expect(screen.getByText("月光下的山谷")).toBeInTheDocument();
  });

  it("keeps a call-only completed event pending until a terminal result arrives", () => {
    render(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "generate_image",
                arguments: { prompt: "静夜思的月光" },
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.getByText("正在生成图片...")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reports an empty terminal image result as a failure", () => {
    render(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            { data: { name: "generate_image" } },
            { data: { output: [] } },
          ],
        }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("模型没有返回图片。");
  });

  it("copies the generated image itself instead of its URL text", async () => {
    const user = userEvent.setup();
    Object.defineProperty(window, "isSecureContext", {
      configurable: true,
      value: true,
    });
    const copyImageFromUrl = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: { copyImageFromUrl },
    });
    const clipboardWrite = vi
      .spyOn(navigator.clipboard, "writeText")
      .mockResolvedValue();
    render(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "generate_image",
                arguments: { prompt: "安静的湖面" },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "image",
                    image_url: "https://images.example/lake.png",
                    source: {
                      type: "url",
                      url: "https://images.example/lake.png",
                    },
                    metadata: {
                      workspace_path:
                        "/managed/workspace/.lantai/generated_images/lake.png",
                      managed_path: "/managed/media/generated_images/lake.png",
                    },
                  },
                  {
                    type: "text",
                    text: "已生成 1 张图片。\n模型：Miyang / Image",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByRole("img", { name: "生成图片 1" })).toHaveAttribute(
      "src",
      "https://images.example/lake.png",
    );
    expect(screen.getByText(/模型：Miyang/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "复制" }));
    expect(copyImageFromUrl).toHaveBeenCalledWith({
      url: "https://images.example/lake.png",
      localPaths: [
        "/managed/media/generated_images/lake.png",
        "/managed/workspace/.lantai/generated_images/lake.png",
      ],
    });
    expect(clipboardWrite).not.toHaveBeenCalled();
  });

  it("downloads generated image bytes through the desktop save dialog", async () => {
    const user = userEvent.setup();
    const saveImageFromUrl = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: { saveImageFromUrl },
    });
    render(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            { data: { name: "generate_image" } },
            {
              data: {
                output: [
                  {
                    type: "image",
                    image_url: "https://images.example/lake.png",
                    metadata: {
                      managed_path: "/managed/media/generated_images/lake.png",
                    },
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: "下载" }));

    expect(saveImageFromUrl).toHaveBeenCalledWith({
      url: "https://images.example/lake.png",
      localPaths: ["/managed/media/generated_images/lake.png"],
    });
  });

  it("reports a generated image download failure instead of doing nothing", async () => {
    const user = userEvent.setup();
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: {
        saveImageFromUrl: vi.fn().mockResolvedValue({
          success: false,
          error: "图片链接已失效",
        }),
      },
    });
    render(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            { data: { name: "generate_image" } },
            {
              data: {
                output: [
                  {
                    type: "image",
                    image_url: "https://images.example/expired.png",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: "下载" }));

    expect(messageError).toHaveBeenCalledWith("图片链接已失效");
  });

  it("opens a generated image preview with zoom controls", async () => {
    const user = userEvent.setup();
    render(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            { data: { name: "generate_image" } },
            {
              data: {
                output: [
                  {
                    type: "image",
                    image_url: "https://images.example/lake.png",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    await user.click(screen.getByRole("img", { name: "生成图片 1" }));

    expect(
      document.querySelector(".ant-image-preview-operations-operation-zoomIn"),
    ).toBeInTheDocument();
    expect(
      document.querySelector(".ant-image-preview-operations-operation-zoomOut"),
    ).toBeInTheDocument();
  });

  it("updates the running stage in place when the image result arrives", () => {
    const { container, rerender } = render(
      <ImageGenerationToolCard
        data={{
          status: "running",
          content: [
            {
              data: {
                name: "generate_image",
                arguments: { prompt: "雨夜里的窗" },
              },
            },
          ],
        }}
      />,
    );
    const card = container.querySelector("[data-image-generation-card='1']");

    expect(card).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();

    rerender(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "generate_image",
                arguments: { prompt: "雨夜里的窗" },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "image",
                    image_url: "https://images.example/window.png",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(container.querySelector("[data-image-generation-card='1']")).toBe(
      card,
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "生成图片 1" })).toHaveAttribute(
      "src",
      "https://images.example/window.png",
    );
  });

  it("shows generation errors instead of an empty generic tool card", () => {
    render(
      <ImageGenerationToolCard
        data={{
          status: "failed",
          content: [
            { data: { name: "generate_image" } },
            { data: { error: "All configured image models failed" } },
          ],
        }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "All configured image models failed",
    );
  });

  it("surfaces legacy text errors instead of hiding them as an empty result", () => {
    render(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            { data: { name: "generate_image" } },
            {
              data: {
                output: [
                  {
                    type: "text",
                    text:
                      "Error: All configured image models failed: " +
                      "image size must be at least 3686400 pixels",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "image size must be at least 3686400 pixels",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent(
      "模型没有返回图片。",
    );
  });

  it("shows reference editing and artifact lineage", () => {
    render(
      <ImageGenerationToolCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "generate_image",
                arguments: {
                  prompt: "保留构图，改成水彩风格",
                  reference_images: ["/tmp/reference.png"],
                },
              },
            },
            {
              data: {
                output: {
                  content: [
                    {
                      type: "image",
                      image_url: "https://images.example/edited.png",
                    },
                  ],
                  metadata: {
                    operation: "edit",
                    generation_id: "image-gen-123",
                    parent_artifact_id: "artifact-original",
                    reference_images: ["/tmp/reference.png"],
                  },
                },
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("参考图编辑")).toBeInTheDocument();
    expect(screen.getByText(/基于 1 张参考图/)).toBeInTheDocument();
    expect(screen.getByText(/源版本 artifact-original/)).toBeInTheDocument();
  });
});
