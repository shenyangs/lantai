import { describe, expect, it } from "vitest";
import {
  buildRegisteredToolDisplayName,
  buildRegisteredToolSummary,
  commandPhaseLabel,
  getToolRenderer,
} from "./registry";

describe("tool renderer registry", () => {
  it("classifies common vibe coding shell commands", () => {
    expect(commandPhaseLabel("pnpm test -- --runInBand")).toBe("运行测试");
    expect(commandPhaseLabel("npm run build")).toBe("构建项目");
    expect(commandPhaseLabel("npm run dev -- --host 0.0.0.0")).toBe("启动预览");
  });

  it.each([
    ["ls -la src", "查看文件"],
    ["find console/src -name '*.tsx'", "查看文件"],
    ["cat index.html", "读取文件"],
    ["sed -n '1,120p' README.md", "读取文件"],
    ["rg -n 'execute_shell_command' console/src", "搜索内容"],
    ["git status --short", "检查代码变更"],
    ["git diff --check", "检查代码变更"],
    ["echo 'rg -n TODO src'", "终端操作"],
    ["python -c 'print(\"git status\")'", "终端操作"],
    ["uname -a", "终端操作"],
  ])("labels %s as %s", (command, expected) => {
    expect(commandPhaseLabel(command)).toBe(expected);
  });

  it("uses a dynamic shell action as the display name with a safe fallback", () => {
    expect(
      buildRegisteredToolDisplayName({
        toolName: "execute_shell_command",
        args: { command: "rg -n 'TODO' src" },
        status: "done",
      }),
    ).toBe("搜索内容");

    expect(
      buildRegisteredToolDisplayName({
        toolName: "execute_shell_command",
        args: {},
        status: "done",
      }),
    ).toBe("终端操作");
  });

  it("builds compact summaries through registered renderers", () => {
    expect(
      buildRegisteredToolSummary({
        toolName: "execute_shell_command",
        args: { command: "pnpm test -- --runInBand" },
        status: "done",
      }),
    ).toContain("运行测试 · pnpm test");

    expect(
      buildRegisteredToolSummary({
        toolName: "TodoWrite",
        args: {
          todos: [
            { content: "read", status: "completed" },
            { content: "build", status: "pending" },
          ],
        },
        status: "done",
      }),
    ).toBe("更新任务清单 · 1/2 完成");
  });

  it("resolves mcp-prefixed tool names by short name", () => {
    expect(getToolRenderer("mcp__local__grep")).toBe(getToolRenderer("grep"));
  });

  it("surfaces preview and deployment URLs from shell output", () => {
    expect(
      buildRegisteredToolSummary({
        toolName: "execute_shell_command",
        args: { command: "npm run dev" },
        status: "done",
        resultText: "Local: http://localhost:5173/",
      }),
    ).toBe("启动预览 · localhost:5173");

    expect(
      buildRegisteredToolSummary({
        toolName: "execute_shell_command",
        args: { command: "vercel deploy --prod" },
        status: "done",
        resultText: "Production: https://demo.vercel.app",
      }),
    ).toBe("发布部署 · demo.vercel.app");
  });
});
