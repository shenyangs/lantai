import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "../request";
import { workMethodApi } from "./workMethod";

vi.mock("../request", () => ({ request: vi.fn() }));

const mockedRequest = vi.mocked(request);

describe("workMethodApi", () => {
  beforeEach(() => mockedRequest.mockReset());

  it("lists, saves and deletes agent-scoped work methods", async () => {
    const draft = {
      name: "编辑审校",
      summary: "先核事实，再统一表达。",
      instructions: ["核验事实"],
      scope: ["编辑"],
      acceptance: ["事实有来源"],
      max_steps: 12,
      max_tool_calls: 30,
      max_wall_seconds: 1800,
      max_no_progress_iterations: 3,
    };

    await workMethodApi.listWorkMethods();
    await workMethodApi.saveWorkMethod("editorial review", draft);
    await workMethodApi.deleteWorkMethod("editorial review");

    expect(mockedRequest).toHaveBeenNthCalledWith(1, "/work-methods");
    expect(mockedRequest).toHaveBeenNthCalledWith(
      2,
      "/work-methods/editorial%20review",
      { method: "PUT", body: JSON.stringify(draft) },
    );
    expect(mockedRequest).toHaveBeenNthCalledWith(
      3,
      "/work-methods/editorial%20review",
      { method: "DELETE" },
    );
  });
});
