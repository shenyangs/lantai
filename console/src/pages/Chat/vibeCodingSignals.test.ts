import { describe, expect, it } from "vitest";
import {
  buildVibeCodingSignal,
  extractUrls,
  isDeploymentUrl,
  isLocalPreviewUrl,
  shortUrlLabel,
} from "./vibeCodingSignals";

describe("vibeCodingSignals", () => {
  it("extracts unique URLs and trims sentence punctuation", () => {
    expect(
      extractUrls(
        'ready at "http://localhost:5173/".',
        "again http://localhost:5173/",
        "deploy: https://demo.vercel.app/path)",
      ),
    ).toEqual(["http://localhost:5173/", "https://demo.vercel.app/path"]);
  });

  it("classifies local preview and deployment URLs", () => {
    expect(isLocalPreviewUrl("http://localhost:5173/")).toBe(true);
    expect(isLocalPreviewUrl("http://127.0.0.1:5173/")).toBe(true);
    expect(isLocalPreviewUrl("https://example.com")).toBe(false);
    expect(isDeploymentUrl("https://demo.vercel.app")).toBe(true);
    expect(isDeploymentUrl("http://demo.vercel.app")).toBe(false);
  });

  it("builds a preview signal before deployment links", () => {
    const signal = buildVibeCodingSignal({
      toolName: "execute_shell_command",
      args: { command: "npm run dev" },
      resultText: "Local: http://localhost:5173/ Network: https://demo.vercel.app",
    });

    expect(signal).toMatchObject({
      kind: "preview",
      url: "http://localhost:5173/",
      summary: "预览地址 · localhost:5173",
    });
  });

  it("requires a deployment hint before surfacing public links as deploy output", () => {
    expect(
      buildVibeCodingSignal({
        toolName: "execute_shell_command",
        args: { command: "curl https://demo.vercel.app" },
        resultText: "ok",
      }),
    ).toBeNull();

    expect(
      buildVibeCodingSignal({
        toolName: "execute_shell_command",
        args: { command: "vercel deploy --prod" },
        resultText: "Production: https://demo.vercel.app",
      }),
    ).toMatchObject({
      kind: "deployment",
      summary: "部署链接 · demo.vercel.app",
    });
  });

  it("keeps URL labels compact", () => {
    expect(shortUrlLabel("https://example.com/a/b")).toBe("example.com/a/b");
  });
});
