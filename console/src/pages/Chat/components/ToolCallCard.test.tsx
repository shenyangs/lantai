import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const { mockInspectPreview } = vi.hoisted(() => ({
  mockInspectPreview: vi.fn(),
}));

vi.mock("../../../api/modules/runtime", () => ({
  runtimeApi: {
    inspectPreview: mockInspectPreview,
  },
}));

import ToolCallCard from "./ToolCallCard";

const textToolData = {
  status: "completed",
  content: [
    {
      data: {
        name: "memory_search",
        arguments: { query: "沈行知名字由来取名含义" },
      },
    },
    {
      data: {
        output: [
          {
            type: "text",
            text: "命中 MEMORY.md 中的姓名设定与角色说明。",
          },
        ],
      },
    },
  ],
};

const memoryToolData = {
  status: "completed",
  content: [
    {
      data: {
        name: "memory_search",
        arguments: { query: "沈行知名字由来取名含义" },
      },
    },
    {
      data: {
        output: [
          {
            type: "text",
            text: JSON.stringify([
              {
                path: "/Users/example/Library/Application Support/lantai-console/working/workspaces/default/MEMORY.md",
                start_line: 1,
                end_line: 27,
                score: 0.36,
                snippet:
                  '---\nsummary: "Agent 长期记忆 — 工具设置与经验教训"\nread_when:\n  - 手动引导工作区\n---\n\n## 工具设置\n\nSkills 定义工具怎么用。这文件记你的具体情况。',
              },
            ]),
          },
        ],
      },
    },
  ],
};

const runningToolData = {
  status: "running",
  content: [
    {
      data: {
        name: "web_fetch",
        arguments: { url: "https://example.com/report" },
      },
    },
  ],
};

const failedToolData = {
  status: "failed",
  content: [
    {
      data: {
        name: "read_file",
        arguments: { file_path: "/tmp/MISSING.md" },
      },
    },
    {
      data: {
        error: "Error: file not found",
      },
    },
  ],
};

describe("ToolCallCard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    mockInspectPreview.mockReset();
  });

  it("renders a compact output summary without exposing input params", () => {
    const { container } = render(<ToolCallCard data={textToolData} />);

    expect(screen.getByText("执行轨迹")).toBeInTheDocument();
    expect(screen.getByText("memory_search")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.getByText(/命中 MEMORY\.md/)).toBeInTheDocument();
    expect(
      container.querySelector('[data-chat-tool-call="1"]'),
    ).toHaveAttribute("data-tool-status", "done");
    expect(screen.queryByText(/"query"/)).not.toBeInTheDocument();
    expect(screen.queryByText("调用详情")).not.toBeInTheDocument();
  });

  it("shows process, call details and cleaned result after expansion", async () => {
    const user = userEvent.setup();
    render(<ToolCallCard data={textToolData} />);

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    expect(screen.getByText("过程摘要")).toBeInTheDocument();
    expect(screen.getByText("工具调用")).toBeInTheDocument();
    expect(screen.getByText("调用详情")).toBeInTheDocument();
    expect(screen.getByText("返回结果")).toBeInTheDocument();
    expect(screen.getAllByText(/命中 MEMORY\.md/).length).toBeGreaterThan(1);
    expect(screen.getByText(/沈行知名字由来取名含义/)).toBeInTheDocument();
  });

  it("cleans nested memory_search JSON into readable hits", async () => {
    const user = userEvent.setup();
    render(<ToolCallCard data={memoryToolData} />);

    expect(screen.getByText(/返回 1 条结果 · MEMORY\.md/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    expect(screen.getByText(/\[1\] MEMORY\.md:1-27/)).toBeInTheDocument();
    expect(screen.getByText(/摘要：Agent 长期记忆/)).toBeInTheDocument();
    expect(screen.getByText(/Skills 定义工具怎么用/)).toBeInTheDocument();
    expect(screen.queryByText(/start_line/)).not.toBeInTheDocument();
    expect(screen.queryByText(/score/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\\"path\\"/)).not.toBeInTheDocument();
  });

  it("shows a running tool as an execution ledger item", async () => {
    const user = userEvent.setup();
    const { container } = render(<ToolCallCard data={runningToolData} />);

    expect(screen.getByText("执行轨迹")).toBeInTheDocument();
    expect(screen.getByText("执行中")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("执行中");
    expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
    expect(
      container.querySelector('[data-chat-tool-call="1"]'),
    ).toHaveAttribute("data-tool-status", "running");
    expect(
      screen.getByText(/读取网页 · https:\/\/example\.com/),
    ).toBeInTheDocument();
    expect(screen.getByText("等待结果")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    expect(screen.getByText(/web_fetch 正在执行/)).toBeInTheDocument();
    expect(
      screen.getAllByText(/https:\/\/example\.com\/report/).length,
    ).toBeGreaterThan(0);
  });

  it("keeps failed tool errors inspectable", async () => {
    const user = userEvent.setup();
    const { container } = render(<ToolCallCard data={failedToolData} />);

    expect(screen.getByText("未完成")).toBeInTheDocument();
    expect(screen.queryByText("需要处理")).not.toBeInTheDocument();
    expect(screen.getByText(/Error: file not found/)).toBeInTheDocument();
    expect(
      container.querySelector('[data-chat-tool-call="1"]'),
    ).toHaveAttribute("data-tool-status", "failed");

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    expect(screen.getByText(/read_file 这一步没有完成/)).toBeInTheDocument();
    expect(screen.getByText(/任务可以继续尝试其他方式/)).toBeInTheDocument();
    expect(screen.getAllByText(/MISSING\.md/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Error: file not found/).length).toBeGreaterThan(
      1,
    );
  });

  it("treats an explicit business failure as failed even when IPC transport completed", () => {
    const { container } = render(
      <ToolCallCard
        data={{
          status: "completed",
          content: [
            { data: { name: "save_settings", arguments: {} } },
            {
              data: {
                output: { success: false, error: "版本冲突，未保存" },
              },
            },
          ],
        }}
      />,
    );

    expect(screen.getByText("未完成")).toBeInTheDocument();
    expect(container.querySelector('[data-chat-tool-call="1"]')).toHaveAttribute(
      "data-tool-status",
      "failed",
    );
  });

  it("accepts an explicitly completed void operation as done", () => {
    const { container } = render(
      <ToolCallCard
        data={{
          status: "completed",
          content: [{ data: { name: "close_preview", arguments: {} } }],
        }}
      />,
    );

    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(container.querySelector('[data-chat-tool-call="1"]')).toHaveAttribute(
      "data-tool-status",
      "done",
    );
  });

  it("does not infer success from output when the terminal status is unknown", () => {
    const { container } = render(
      <ToolCallCard
        data={{
          status: "mystery",
          content: [
            { data: { name: "remote_write", arguments: {} } },
            { data: { output: { request_id: "req-1" } } },
          ],
        }}
      />,
    );

    expect(screen.getByText("结果待核实")).toBeInTheDocument();
    expect(container.querySelector('[data-chat-tool-call="1"]')).toHaveAttribute(
      "data-tool-status",
      "unknown",
    );
  });

  it("uses the shell command intent as the compact title and keeps the raw tool name in details", async () => {
    const user = userEvent.setup();
    render(
      <ToolCallCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "execute_shell_command",
                arguments: { command: "pnpm test -- --runInBand" },
              },
            },
            { data: { output: "3 passed" } },
          ],
        }}
      />,
    );

    expect(screen.getByText("运行测试")).toBeInTheDocument();
    expect(screen.queryByText("execute_shell_command")).not.toBeInTheDocument();
    expect(screen.getByText(/运行测试 · pnpm test/)).toBeInTheDocument();
    expect(screen.getByText(/3 passed/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    expect(screen.getByText("execute_shell_command")).toBeInTheDocument();
  });

  it("surfaces local preview URLs and checks preview health after expansion", async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn().mockResolvedValue({ ok: true } as Response);
    render(
      <ToolCallCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "execute_shell_command",
                arguments: { command: "npm run dev" },
              },
            },
            { data: { output: "Local: http://localhost:5173/" } },
          ],
        }}
      />,
    );

    expect(screen.getByText(/启动预览 · localhost:5173/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    expect(screen.getByText("预览检查")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /localhost:5173/ }),
    ).toHaveAttribute("href", "http://localhost:5173/");
    expect(fetch).toHaveBeenCalledWith(
      "http://localhost:5173/",
      expect.objectContaining({ mode: "no-cors" }),
    );
  });

  it("backfeeds unreachable local preview health as a repair signal", async () => {
    const user = userEvent.setup();
    global.fetch = vi.fn().mockRejectedValue(new Error("refused"));
    mockInspectPreview.mockResolvedValue({
      url: "http://localhost:5173/",
      fetch_url: "http://localhost:5173/",
      ok: false,
      summary: "本地预览不可达。",
      findings: [
        {
          severity: "critical",
          category: "availability",
          message: "本地预览不可达。",
          evidence: "Connection refused",
        },
      ],
      network_errors: [
        {
          url: "http://localhost:5173/",
          status: null,
          error: "Connection refused",
        },
      ],
      console_errors: [],
      page_errors: [],
    });
    const postSpy = vi
      .spyOn(window, "postMessage")
      .mockImplementation(() => undefined);
    render(
      <ToolCallCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "execute_shell_command",
                arguments: { command: "npm run dev" },
              },
            },
            { data: { output: "Local: http://localhost:5173/" } },
          ],
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    await waitFor(() => {
      expect(mockInspectPreview).toHaveBeenCalledWith({
        url: "http://localhost:5173/",
        record_event: false,
      });
      expect(postSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          type: "preview:runtime:error",
          source: "preview-harness",
          summary: "本地预览不可达。",
          url: "http://localhost:5173/",
        }),
        window.location.origin,
      );
    });
  });

  it("surfaces deployment links from deploy output", async () => {
    const user = userEvent.setup();
    render(
      <ToolCallCard
        data={{
          status: "completed",
          content: [
            {
              data: {
                name: "execute_shell_command",
                arguments: { command: "vercel deploy --prod" },
              },
            },
            { data: { output: "Production: https://demo.vercel.app" } },
          ],
        }}
      />,
    );

    expect(screen.getByText(/发布部署 · demo.vercel.app/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    expect(screen.getByText("部署结果")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /demo.vercel.app/ }),
    ).toHaveAttribute("href", "https://demo.vercel.app");
  });

  it("truncates very long detail previews", async () => {
    const user = userEvent.setup();
    render(
      <ToolCallCard
        data={{
          status: "completed",
          content: [
            { data: { name: "execute_shell_command", arguments: {} } },
            { data: { output: "A".repeat(21_000) } },
          ],
        }}
      />,
    );

    await user.click(screen.getByRole("button", { name: /展开工具调用/ }));

    expect(screen.getByText(/已截断预览/)).toBeInTheDocument();
  });

  it("does not report copy success when the clipboard capability is missing", async () => {
    const user = userEvent.setup();
    const descriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");

    try {
      render(<ToolCallCard data={textToolData} />);
      await user.click(screen.getByRole("button", { name: /展开工具调用/ }));
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: undefined,
      });
      const copyButton = screen.getByRole("button", {
        name: "复制返回结果",
      });

      fireEvent.click(copyButton);

      expect(copyButton.querySelector('[data-lucide="check"]')).toBeNull();
      await waitFor(() =>
        expect(screen.getByRole("alert")).toHaveTextContent(
          "无法访问剪贴板，请手动选择文本复制",
        ),
      );
    } finally {
      if (descriptor) {
        Object.defineProperty(navigator, "clipboard", descriptor);
      } else {
        Reflect.deleteProperty(navigator, "clipboard");
      }
    }
  });
});
