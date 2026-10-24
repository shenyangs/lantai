import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import VideoGenerationToolCard from "./VideoGenerationToolCard";

const messageError = vi.hoisted(() => vi.fn());
const messageSuccess = vi.hoisted(() => vi.fn());

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "chat.videoGeneration.title": "生成视频",
        "chat.videoGeneration.failed": "生成失败",
        "chat.videoGeneration.generating": "生成中",
        "chat.videoGeneration.completed": "生成完成",
        "chat.videoGeneration.generatingVideo": "正在生成视频...",
        "chat.videoGeneration.waitHint": "通常需要数分钟",
        "chat.videoGeneration.failedTitle": "视频生成失败",
        "chat.videoGeneration.emptyResult": "模型没有返回视频。",
        "chat.videoGeneration.videoAlt": "生成视频播放器",
        "chat.videoGeneration.saved": "视频已保存",
        "chat.videoGeneration.temporaryLink":
          "本地缓存失败，这是临时链接，请尽快下载保存。",
        "common.download": "下载",
      })[key] ?? key,
  }),
}));

vi.mock("@/hooks/useAppMessage", () => ({
  useAppMessage: () => ({
    message: { error: messageError, success: messageSuccess },
  }),
}));

describe("VideoGenerationToolCard", () => {
  afterEach(() => {
    delete window.electronAPI;
    messageError.mockClear();
    messageSuccess.mockClear();
    vi.restoreAllMocks();
  });

  it("keeps a stable generation stage while the asynchronous task runs", () => {
    render(
      <VideoGenerationToolCard
        data={{
          status: "running",
          content: [
            {
              data: {
                name: "generate_video",
                arguments: { prompt: "雨后的城市慢慢亮起" },
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在生成视频...");
    expect(screen.getByText("雨后的城市慢慢亮起")).toBeVisible();
  });

  it("renders an inline player, model summary and desktop download", async () => {
    const user = userEvent.setup();
    const saveFileAs = vi.fn().mockResolvedValue({ success: true });
    Object.defineProperty(window, "electronAPI", {
      configurable: true,
      value: { isDesktop: true, saveFileAs },
    });
    render(
      <VideoGenerationToolCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "generate_video",
                arguments: { prompt: "海边日落" },
              },
            },
            {
              data: {
                output: [
                  {
                    type: "video",
                    video_url: "/tmp/lantai/generated_video.mp4",
                    source: {
                      type: "url",
                      url: "/tmp/lantai/generated_video.mp4",
                    },
                  },
                  {
                    type: "text",
                    text: "已生成视频。模型：TokenDance / Seedance 2.0。",
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByLabelText("生成视频播放器")).toHaveAttribute("controls");
    expect(screen.getByText(/Seedance 2.0/)).toBeVisible();

    await user.click(screen.getByRole("button", { name: "下载" }));

    expect(saveFileAs).toHaveBeenCalledWith({
      sourcePath: "/tmp/lantai/generated_video.mp4",
      filename: "generated_video.mp4",
    });
    expect(messageSuccess).toHaveBeenCalledWith("视频已保存");
  });

  it("reports an empty terminal video result as a failure", () => {
    render(
      <VideoGenerationToolCard
        data={{
          status: "completed",
          content: [
            { data: { name: "generate_video" } },
            { data: { output: [] } },
          ],
        }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("模型没有返回视频。");
  });

  it("warns when the playable result is only a temporary remote link", () => {
    render(
      <VideoGenerationToolCard
        data={{
          status: "completed",
          content: [
            { data: { name: "generate_video" } },
            {
              data: {
                output: [
                  {
                    type: "video",
                    video_url: "https://cdn.example/video.mp4?token=secret",
                    metadata: { durable: false },
                  },
                ],
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText(/临时链接/)).toBeVisible();
  });
});
