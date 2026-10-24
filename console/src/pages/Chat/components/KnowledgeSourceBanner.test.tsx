import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import KnowledgeSourceBanner, {
  knowledgeScopeExplanation,
} from "./KnowledgeSourceBanner";

vi.mock("@/vendor/lantaiChat", () => ({
  useChatAnywhereSessionsState: () => ({
    currentSessionId: "local-1",
    sessions: [
      {
        id: "local-1",
        meta: {
          knowledge_source: {
            path: ".wiki/raw/sources/7-9月媒体专项预算.xlsx",
            filename: "7-9月媒体专项预算.xlsx",
          },
        },
      },
    ],
  }),
}));

describe("KnowledgeSourceBanner", () => {
  it("keeps the bound source visible above the composer", () => {
    render(<KnowledgeSourceBanner />);

    expect(screen.getByText("仅依据")).toBeInTheDocument();
    expect(screen.getByText("7-9月媒体专项预算.xlsx")).toBeInTheDocument();
    expect(
      screen.getByRole("status", {
        name: /当前对话已选择 7-9月媒体专项预算\.xlsx/,
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("仅依据所选资料")).toBeInTheDocument();
    expect(
      screen.getByText("检索仅限所选资料；实际引用会在回答中显示来源链接"),
    ).toBeInTheDocument();
  });

  it("describes expanded scope without calling it limited", () => {
    expect(knowledgeScopeExplanation(false)).toBe(
      "优先使用所选资料；回答也可检索并补充其他信息",
    );
    expect(knowledgeScopeExplanation(false)).not.toContain("仅限");
  });
});
