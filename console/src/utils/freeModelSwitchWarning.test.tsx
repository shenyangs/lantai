import type { TFunction } from "i18next";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { confirmModal } = vi.hoisted(() => ({
  confirmModal: vi.fn(),
}));

vi.mock("@/vendor/lantaiDesign", () => ({
  Checkbox: () => null,
  Modal: { confirm: confirmModal },
}));

import { confirmFreeModelSwitch } from "./freeModelSwitchWarning";

describe("confirmFreeModelSwitch", () => {
  beforeEach(() => {
    window.localStorage.clear();
    confirmModal.mockReset();
  });

  it("requires confirmation before switching to the free experience model", async () => {
    const confirmation = confirmFreeModelSwitch({
      provider: { id: "opencode", base_url: "https://opencode.ai/zen/v1" },
      model: { is_free: true },
      t: ((key: string) => key) as TFunction,
    });

    expect(confirmModal).toHaveBeenCalledOnce();
    expect(confirmModal.mock.calls[0][0].title).toBe(
      "models.freeModelWarningTitle",
    );

    confirmModal.mock.calls[0][0].onOk();
    await expect(confirmation).resolves.toBe(true);
  });
});
