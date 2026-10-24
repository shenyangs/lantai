import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { LantaiRuntimeWebUI } from "@/vendor/lantaiChat";
import QuquResponseCard from "./QuquResponseCard";

vi.mock("@/api", () => ({
  chatApi: { continueQuqu: vi.fn(), getAnalysisChart: vi.fn() },
  utilityIntelligenceApi: { classifyUtilityArtifacts: vi.fn() },
}));

vi.mock("echarts/core", () => ({
  use: vi.fn(),
  init: vi.fn(() => ({
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  })),
}));

const avatarSelector = '[data-lantai-response-avatar="1"]';

function MainChatResponseCard(props: Parameters<typeof QuquResponseCard>[0]) {
  // Match ChatPage's actual response card: progress belongs to the composer.
  // Do not mock the runtime/session hooks or replace the real response card.
  return <QuquResponseCard {...props} progressInComposer />;
}

function sseResponse(frames: unknown[]) {
  return new Response(
    frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join(""),
    { headers: { "Content-Type": "text/event-stream" } },
  );
}

describe("main chat first visible assistant identity", () => {
  it("paints the real avatar before fetch and does not fabricate reasoning while the provider is silent", async () => {
    let resolveFetch!: (response: Response) => void;
    let avatarPaintedBeforeFetch = false;
    const fetchStream = vi.fn(() => {
      avatarPaintedBeforeFetch = Boolean(
        document.querySelector(avatarSelector),
      );
      return new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      });
    });
    const { container } = render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { LantaiRuntimeResponseCard: MainChatResponseCard },
          welcome: { avatar: "/test-assistant.png", nick: "沈行知" },
          sender: { placeholder: "测试发送" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async (session) => [
                { id: "session-1", messages: session.messages || [] },
              ],
            },
          },
        }}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText("测试发送"), {
      target: { value: "把测试内容做成PPT" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));

    try {
      await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
      expect(avatarPaintedBeforeFetch).toBe(true);
      expect(container.querySelectorAll(avatarSelector)).toHaveLength(1);
      expect(container.querySelector(avatarSelector)).toHaveTextContent(
        "沈行知",
      );
      expect(screen.queryByText(/正在思考/)).not.toBeInTheDocument();
      expect(screen.queryByText("真实模型思考")).not.toBeInTheDocument();
      // The identity is not a second progress signal or a second live region.
      expect(
        container.querySelector('[data-lantai-assistant-pending="1"]'),
      ).toBeNull();
    } finally {
      await act(async () => {
        resolveFetch(
          sseResponse([
            {
              object: "message",
              id: "reasoning-1",
              type: "reasoning",
              status: "completed",
              role: "assistant",
              content: [{ type: "text", text: "真实模型思考" }],
            },
            {
              object: "message",
              id: "answer-1",
              type: "message",
              status: "completed",
              role: "assistant",
              content: [{ type: "text", text: "测试回复" }],
            },
            { object: "response", id: "response-1", status: "completed" },
          ]),
        );
      });
      await screen.findByText("测试回复");
    }

    expect(container.querySelectorAll(avatarSelector)).toHaveLength(1);
    expect(screen.getByText("思考", { exact: true })).toBeInTheDocument();
    fireEvent.click(screen.getByText("思考", { exact: true }));
    expect(screen.getByText("真实模型思考")).toBeInTheDocument();
  });

  it("keeps the identity after a server receipt while waiting for the first content", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({
      start(value) {
        controller = value;
      },
    });
    const send = (frame: unknown) =>
      controller.enqueue(
        new TextEncoder().encode(`data: ${JSON.stringify(frame)}\n\n`),
      );
    const fetchStream = vi.fn(async () => new Response(stream));
    const { container } = render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { LantaiRuntimeResponseCard: MainChatResponseCard },
          welcome: { avatar: "/test-assistant.png", nick: "沈行知" },
          sender: { placeholder: "测试回执" },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
              updateSession: async (session) => [
                { id: "session-1", messages: session.messages || [] },
              ],
            },
          },
        }}
      />,
    );
    fireEvent.change(screen.getByPlaceholderText("测试回执"), {
      target: { value: "测试首反馈" },
    });
    fireEvent.click(screen.getByRole("button", { name: /发\s*送/ }));
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(container.querySelectorAll(avatarSelector)).toHaveLength(1);
    try {
      await act(async () => {
        send({
          object: "response",
          status: "in_progress",
          output: [],
          metadata: {
            initial_status: true,
            run_id: "receipt-run",
            turn_receipt: {
              run_id: "receipt-run",
              attempt_id: "receipt-run:attempt-1",
            },
          },
        });
      });
      expect(container.querySelectorAll(avatarSelector)).toHaveLength(1);
      expect(screen.queryByText(/正在思考/)).not.toBeInTheDocument();
      expect(
        container.querySelector('[data-lantai-assistant-pending="1"]'),
      ).toBeNull();
    } finally {
      await act(async () => {
        send({
          object: "message",
          id: "receipt-answer",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "回执后真实回复" }],
          metadata: { run_id: "receipt-run" },
        });
        send({
          object: "response",
          status: "completed",
          metadata: { run_id: "receipt-run" },
        });
        controller.close();
      });
      await screen.findByText("回执后真实回复");
    }
    expect(container.querySelectorAll(avatarSelector)).toHaveLength(1);
  });
});
