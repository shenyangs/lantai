import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { LantaiRuntimeWebUI, useChatAnywhereSessionsState } from "./lantaiChat";

const sse = (payload: unknown) =>
  new TextEncoder().encode(`data: ${JSON.stringify(payload)}\n\n`);
let observedSession: any;
function ProbeCard({ data }: { data: any }) {
  const { sessions, currentSessionId } = useChatAnywhereSessionsState();
  observedSession = sessions.find((session) => session.id === currentSessionId);
  return (
    <div>
      {(data?.output || []).map((row: any) => (
        <section key={row.id}>
          {(row.content || []).map((item: any) => item.text || "").join("")}
        </section>
      ))}
    </div>
  );
}
it.each(["list", "history"] as const)(
  "keeps the active second turn identity when first-turn %s arrives late",
  async (delayedSnapshot) => {
    observedSession = undefined;
    const controllers: ReadableStreamDefaultController<Uint8Array>[] = [];
    const fetchStream = vi.fn(async (request: any) => {
      if (request.biz_params?.operation === "steer")
        return new Response(
          `data: ${JSON.stringify({ object: "turn_receipt", status: "steered", receipt: { operation: "steer", status: "steered" } })}\n\n`,
        );
      return new Response(
        new ReadableStream<Uint8Array>({
          start(controller) {
            controllers.push(controller);
            const runId = `run-${controllers.length}`;
            controller.enqueue(
              sse({
                object: "response",
                status: "in_progress",
                metadata: {
                  initial_status: true,
                  run_id: runId,
                  turn_receipt: {
                    run_id: runId,
                    attempt_id: `${runId}:attempt-1`,
                  },
                },
              }),
            );
          },
        }),
      );
    });
    let resolveOldHydration: ((value: any) => void) | undefined;
    let resolveOldList: ((value: any) => void) | undefined;
    const getSessionList = vi
      .fn()
      .mockResolvedValueOnce([{ id: "session-1", messages: [] }])
      .mockImplementationOnce(() =>
        delayedSnapshot === "list"
          ? new Promise((resolve) => {
              resolveOldList = resolve;
            })
          : Promise.resolve([{ id: "session-1", messages: [] }]),
      )
      .mockResolvedValue([{ id: "session-1", messages: [] }]);
    const getSession = vi
      .fn()
      .mockResolvedValueOnce({ id: "session-1", messages: [] })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOldHydration = resolve;
          }),
      )
      .mockResolvedValue({ id: "session-1", messages: [] });
    render(
      <LantaiRuntimeWebUI
        options={{
          api: { fetch: fetchStream },
          cards: { LantaiRuntimeResponseCard: ProbeCard },
          sender: { placeholder: "说点什么" },
          session: {
            api: {
              getSessionList,
              getSession,
              updateSession: async () => [{ id: "session-1", messages: [] }],
            },
          },
        }}
      />,
    );
    const textarea = await screen.findByPlaceholderText("说点什么");
    await waitFor(() => expect(getSession).toHaveBeenCalledTimes(1));
    fireEvent.change(textarea, { target: { value: "第一轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() =>
      expect(observedSession?.runtimeState?.run_id).toBe("run-1"),
    );
    act(() => {
      controllers[0].enqueue(
        sse({
          object: "message",
          id: "answer-1",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "第一条回复", status: "completed" }],
        }),
      );
      controllers[0].enqueue(
        sse({ object: "response", id: "response-1", status: "completed" }),
      );
      controllers[0].close();
    });
    await waitFor(() =>
      expect(
        delayedSnapshot === "list" ? resolveOldList : resolveOldHydration,
      ).toBeTypeOf("function"),
    );
    fireEvent.change(textarea, { target: { value: "第二轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
    await waitFor(() =>
      expect(observedSession?.runtimeState?.run_id).toBe("run-2"),
    );
    await act(async () => {
      const oldSnapshot = {
        id: "session-1",
        messages: [],
        generating: false,
        status: "idle",
        runtimeState: {
          status: "completed",
          run_id: "run-1",
          attempt_id: "run-1:attempt-1",
          can_stop: false,
          can_reconnect: false,
        },
      };
      if (delayedSnapshot === "list") resolveOldList?.([oldSnapshot]);
      else resolveOldHydration?.(oldSnapshot);
      await Promise.resolve();
    });
    expect(screen.getByText("第一条回复")).toBeInTheDocument();
    expect(screen.getByText("第二轮")).toBeInTheDocument();
    fireEvent.change(textarea, { target: { value: "补充第二轮" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter", metaKey: true });
    await waitFor(() => expect(fetchStream).toHaveBeenCalledTimes(3));
    expect(fetchStream.mock.calls[2][0].biz_params.operation).toBe("steer");
    expect(fetchStream.mock.calls[2][0].biz_params.meta.expected_run_id).toBe(
      "run-2",
    );
    expect(
      fetchStream.mock.calls[2][0].biz_params.meta.expected_attempt_id,
    ).toBe("run-2:attempt-1");
    expect(observedSession.__primaryStreamActive).toBe(true);
    expect(observedSession.generating).toBe(true);
    expect(observedSession.runtimeState.status).toBe("running");
    act(() => {
      controllers[1].enqueue(
        sse({
          object: "message",
          id: "answer-2",
          type: "message",
          status: "completed",
          role: "assistant",
          content: [{ type: "text", text: "第二条回复", status: "completed" }],
        }),
      );
      controllers[1].enqueue(
        sse({ object: "response", id: "response-2", status: "completed" }),
      );
      controllers[1].close();
    });
    await screen.findByText("第二条回复");
  },
);
