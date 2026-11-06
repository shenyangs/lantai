import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { LantaiRuntimeWebUI } from "./lantaiChat";
import { setTextareaValue } from "../pages/Chat/utils";

it.each([true, false])(
  "submits the prepared composer draft and preserves attachments (rewrite=%s)",
  async (rewrite) => {
    const fetchStream = vi.fn(
      async () =>
        new Response(
          `data: ${JSON.stringify({ object: "response", id: "prepared-response", status: "completed", output: [] })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    const beforeSubmit = vi.fn(async () => {
      const textarea = screen.getByPlaceholderText(
        "说点什么",
      ) as HTMLTextAreaElement;
      if (rewrite) setTextareaValue(textarea, "/plan 原始问题");
      return true;
    });
    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          sender: {
            placeholder: "说点什么",
            beforeSubmit,
            attachments: {
              customRequest: (request: any) =>
                request.onSuccess({ url: "/files/preview/screen.png" }),
            },
          },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
            },
          },
        }}
      />,
    );
    const textarea = await screen.findByPlaceholderText("说点什么");
    const file = new File(["image"], "screen.png", { type: "image/png" });
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [file] },
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled(),
    );
    fireEvent.change(textarea, { target: { value: "原始问题" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    expect(beforeSubmit).toHaveBeenCalledTimes(1);
    expect(fetchStream.mock.calls[0][0].input[0].content).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "text",
          text: rewrite ? "/plan 原始问题" : "原始问题",
        }),
        expect.objectContaining({
          type: "image",
          filename: "screen.png",
          image_url: "/files/preview/screen.png",
        }),
      ]),
    );
  },
);

it.each([true, false])(
  "keeps the submitted text and attachments when edited during an async hook (rewrite=%s)",
  async (rewrite) => {
    let allow: ((value: boolean) => void) | undefined;
    const beforeSubmit = vi.fn(() => {
      if (rewrite) {
        setTextareaValue(
          screen.getByPlaceholderText("说点什么") as HTMLTextAreaElement,
          "/plan 原始问题",
        );
      }
      return new Promise<boolean>((resolve) => {
        allow = resolve;
      });
    });
    const fetchStream = vi.fn(
      async (_request: any) =>
        new Response(
          `data: ${JSON.stringify({ object: "response", id: "prepared-response", status: "completed", output: [] })}\n\n`,
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    );
    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          sender: {
            placeholder: "说点什么",
            beforeSubmit,
            attachments: {
              customRequest: (request: any) =>
                request.onSuccess({
                  url: `/files/preview/${request.file.name}`,
                }),
            },
          },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
            },
          },
        }}
      />,
    );
    const textarea = await screen.findByPlaceholderText("说点什么");
    const fileInput = document.querySelector('input[type="file"]')!;
    fireEvent.change(fileInput, {
      target: {
        files: [new File(["first"], "first.png", { type: "image/png" })],
      },
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "发送" })).toBeEnabled(),
    );
    fireEvent.change(textarea, { target: { value: "原始问题" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(allow).toBeTypeOf("function"));
    fireEvent.change(textarea, { target: { value: "尚未确认发送的新想法" } });
    fireEvent.change(fileInput, {
      target: {
        files: [new File(["later"], "later.png", { type: "image/png" })],
      },
    });
    await act(async () => {
      allow?.(true);
    });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(1));
    const sentContent = fetchStream.mock.calls[0][0].input[0].content;
    expect(sentContent).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          type: "text",
          text: rewrite ? "/plan 原始问题" : "原始问题",
        }),
        expect.objectContaining({
          type: "image",
          filename: "first.png",
          image_url: "/files/preview/first.png",
        }),
      ]),
    );
    expect(sentContent).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({ filename: "later.png" }),
      ]),
    );
  },
);

it.each(["blocked", "failed"] as const)(
  "does not send when an async beforeSubmit hook is %s",
  async (outcome) => {
    let finish: (() => void) | undefined;
    const beforeSubmit = vi.fn(
      () =>
        new Promise<boolean>((resolve, reject) => {
          finish = () =>
            outcome === "blocked"
              ? resolve(false)
              : reject(new Error("发送前处理失败"));
        }),
    );
    const fetchStream = vi.fn();
    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          sender: { placeholder: "说点什么", beforeSubmit },
          session: {
            api: {
              getSessionList: async () => [{ id: "session-1", messages: [] }],
              getSession: async () => ({ id: "session-1", messages: [] }),
            },
          },
        }}
      />,
    );
    const textarea = await screen.findByPlaceholderText("说点什么");
    fireEvent.change(textarea, { target: { value: "原始问题" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(finish).toBeTypeOf("function"));
    await act(async () => {
      finish?.();
    });
    expect(fetchStream).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("原始问题");
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
  },
);
