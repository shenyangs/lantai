import { describe, expect, it, vi } from "vitest";

vi.mock("../request", () => ({ request: vi.fn() }));

import { request } from "../request";
import { workspaceGrantsApi } from "./workspaceGrants";

describe("workspaceGrantsApi", () => {
  it("claims a signed desktop selection without sending a raw path", async () => {
    vi.mocked(request).mockResolvedValue({
      id: "wsg_example",
      project_root: "/Users/example/project",
    });

    await workspaceGrantsApi.claim("signed-receipt");

    expect(request).toHaveBeenCalledWith("/workspace-grants/claim", {
      method: "POST",
      body: JSON.stringify({ selection_receipt: "signed-receipt" }),
    });
  });
});
