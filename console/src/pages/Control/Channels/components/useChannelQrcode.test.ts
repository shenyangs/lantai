import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useChannelQrcode } from "./useChannelQrcode";

const { getChannelQrcode, getChannelQrcodeStatus } = vi.hoisted(() => ({
  getChannelQrcode: vi.fn(),
  getChannelQrcodeStatus: vi.fn(),
}));

vi.mock("../../../../api", () => ({
  api: {
    getChannelQrcode,
    getChannelQrcodeStatus,
  },
}));

describe("useChannelQrcode", () => {
  beforeEach(() => {
    getChannelQrcode.mockReset();
    getChannelQrcodeStatus.mockReset();
  });

  it("invalidates a pending authorization when the drawer context resets", async () => {
    let resolveQrcode!: (value: {
      qrcode_img: string;
      poll_token: string;
    }) => void;
    getChannelQrcode.mockReturnValue(
      new Promise((resolve) => {
        resolveQrcode = resolve;
      }),
    );
    const onSuccess = vi.fn();
    const { result } = renderHook(() =>
      useChannelQrcode({
        channel: "wechat",
        successStatus: "confirmed",
        successCredentialKey: "bot_token",
        pollInterval: 10,
        onSuccess,
        onError: vi.fn(),
      }),
    );

    let request!: Promise<void>;
    act(() => {
      request = result.current.fetchQrcode();
    });
    act(() => {
      result.current.reset();
    });
    await act(async () => {
      resolveQrcode({ qrcode_img: "stale-image", poll_token: "stale-token" });
      await request;
    });

    expect(result.current.qrcodeImg).toBe("");
    expect(getChannelQrcodeStatus).not.toHaveBeenCalled();
    expect(onSuccess).not.toHaveBeenCalled();
  });
});
