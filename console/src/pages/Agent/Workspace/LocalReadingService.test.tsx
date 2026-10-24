import { screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import { readingApi, type ReadingLocalStatus } from "@/api/modules/reading";
import LocalReadingService from "./LocalReadingService";

vi.mock("@/api/modules/reading", () => ({
  readingApi: {
    localStatus: vi.fn(),
    localAction: vi.fn(),
    localConfiguration: vi.fn(),
  },
}));
const idle: ReadingLocalStatus = {
  state: "idle",
  installed: false,
  running: false,
  message: "首次准备",
  progress: null,
  version: "pinned",
  node_version: "24.15.0",
  browser_available: false,
  configured_keys: [],
};
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(readingApi.localStatus).mockResolvedValue(idle);
});

it("does not install on page load and shows explicit preparation progress", async () => {
  const status = vi.fn();
  renderWithProviders(
    <LocalReadingService channel={null} onStatus={status} busy={false} />,
  );
  await screen.findByText("首次准备");
  expect(readingApi.localAction).not.toHaveBeenCalled();
  vi.mocked(readingApi.localAction).mockResolvedValue({
    ...idle,
    state: "preparing",
    progress: 55,
    message: "正在安装固定版本",
  });
  fireEvent.click(screen.getByRole("button", { name: "准备本机服务" }));
  await screen.findByText("正在安装固定版本");
  expect(readingApi.localAction).toHaveBeenCalledWith("prepare");
  expect(status).toHaveBeenLastCalledWith(
    expect.objectContaining({ state: "preparing", progress: 55 }),
  );
  expect(screen.getByRole("button", { name: "取消准备" })).toBeInTheDocument();
});

it("allows stopping a running service while a parent subscription is busy", async () => {
  vi.mocked(readingApi.localStatus).mockResolvedValue({
    ...idle,
    state: "ready",
    installed: true,
    running: true,
    browser_available: true,
  });
  vi.mocked(readingApi.localAction).mockResolvedValue({
    ...idle,
    installed: true,
    running: false,
    message: "已停止",
  });
  renderWithProviders(
    <LocalReadingService channel={null} onStatus={vi.fn()} busy />,
  );
  fireEvent.click(await screen.findByRole("button", { name: "停止服务" }));
  await waitFor(() =>
    expect(readingApi.localAction).toHaveBeenCalledWith("stop"),
  );
});
