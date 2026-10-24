import { expect, it } from "vitest";

it("runs pure contracts without constructing a browser environment", () => {
  expect(typeof window).toBe("undefined");
  expect(typeof document).toBe("undefined");
});
