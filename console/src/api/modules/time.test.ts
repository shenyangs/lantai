import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../request", () => ({
  request: vi.fn(),
}));

import { request } from "../request";
import { timeApi } from "./time";

describe("timeApi", () => {
  beforeEach(() => vi.mocked(request).mockReset());

  it("loads the unified today timeline", async () => {
    await timeApi.getToday();
    expect(request).toHaveBeenCalledWith("/time/today");
  });
});
