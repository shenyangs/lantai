import { describe, expect, it } from "vitest";
import { browserPairingErrorMessage } from "./index";

describe("browser pairing recovery copy", () => {
  it("turns an expired code into a clear next step", () => {
    expect(
      browserPairingErrorMessage(new Error("pairing code expired")),
    ).toContain("生成新码");
  });

  it("preserves a useful retry path for connection failures", () => {
    expect(browserPairingErrorMessage(new Error("Failed to fetch"))).toContain(
      "客户端仍在运行",
    );
  });
});
