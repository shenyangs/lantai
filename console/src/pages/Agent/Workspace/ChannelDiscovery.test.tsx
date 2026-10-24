import { screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { renderWithProviders } from "../../../test/common_setup";
import { readingApi, type ReadingChannel } from "@/api/modules/reading";
import ChannelDiscovery from "./ChannelDiscovery";

vi.mock("@/api/modules/reading", () => ({
  readingApi: {
    channels: vi.fn(),
    rsshubSettings: vi.fn(),
    saveRSSHubSettings: vi.fn(),
    saveChannel: vi.fn(),
    previewChannel: vi.fn(),
    subscribeChannel: vi.fn(),
  },
}));
const channel: ReadingChannel = {
  id: "github",
  site: "GitHub",
  name: "仓库动态",
  path: "/github/issue/:user/:repo",
  description: "",
  example: "/github/issue/DIYgod/RSSHub",
  docs: "https://docs.rsshub.app/routes/github",
  categories: ["programming"],
  fields: [
    {
      name: "user",
      optional: false,
      description: "用户名",
      default: "",
      options: [],
    },
    {
      name: "repo",
      optional: false,
      description: "仓库",
      default: "",
      options: [],
    },
  ],
  features: {},
};
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(readingApi.channels).mockResolvedValue({
    items: [channel],
    total: 1,
    catalog_total: 3865,
    categories: ["programming"],
    snapshot: {
      generated_at: "2026-09-08",
      source_revision: "913f11220a1bd5b2c1dd169d9890ff24001988de",
      route_count: 3865,
    },
  });
  vi.mocked(readingApi.rsshubSettings).mockResolvedValue({
    instance_url: "https://rsshub.app",
    allow_private: false,
  });
});

it("shows full snapshot count, parameter fields and previews without subscribing", async () => {
  renderWithProviders(<ChannelDiscovery onSubscribed={vi.fn()} />);
  fireEvent.click(
    await screen.findByRole("button", { name: /GitHub · 仓库动态/ }),
  );
  expect(screen.getByText(/预置 3865 条/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/user（必填）/), {
    target: { value: "DIYgod" },
  });
  fireEvent.change(screen.getByLabelText(/repo（必填）/), {
    target: { value: "RSSHub" },
  });
  vi.mocked(readingApi.previewChannel).mockResolvedValue({
    url: "https://rsshub.app/github/issue/DIYgod/RSSHub",
  });
  fireEvent.click(screen.getByRole("button", { name: "预览订阅地址" }));
  await waitFor(() =>
    expect(readingApi.previewChannel).toHaveBeenCalledWith("github", {
      parameters: { user: "DIYgod", repo: "RSSHub" },
      folder: "",
      path_override: "",
    }),
  );
  expect(readingApi.subscribeChannel).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "返回频道目录" }));
  expect(
    screen.queryByRole("heading", { name: "GitHub · 仓库动态" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /GitHub · 仓库动态/ }),
  ).toBeInTheDocument();
});

it("persists custom instance only on save and does not claim failed subscription succeeded", async () => {
  const done = vi.fn();
  renderWithProviders(<ChannelDiscovery onSubscribed={done} />);
  fireEvent.click(
    await screen.findByRole("button", { name: /GitHub · 仓库动态/ }),
  );
  fireEvent.click(screen.getByRole("button", { name: "服务设置" }));
  fireEvent.change(screen.getByLabelText("RSSHub 实例地址"), {
    target: { value: "http://localhost:1200" },
  });
  expect(readingApi.saveRSSHubSettings).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox", { name: /允许访问此实例/ }));
  vi.mocked(readingApi.saveRSSHubSettings).mockResolvedValue({
    instance_url: "http://localhost:1200",
    allow_private: true,
  });
  fireEvent.click(screen.getByRole("button", { name: "保存实例" }));
  await waitFor(() =>
    expect(readingApi.saveRSSHubSettings).toHaveBeenCalledWith({
      mode: "external",
      instance_url: "http://localhost:1200",
      allow_private: true,
    }),
  );
  await screen.findByText(/实例设置已保存/);
  fireEvent.change(screen.getByLabelText(/user（必填）/), {
    target: { value: "DIYgod" },
  });
  fireEvent.change(screen.getByLabelText(/repo（必填）/), {
    target: { value: "RSSHub" },
  });
  vi.mocked(readingApi.subscribeChannel).mockRejectedValue(
    new Error("此实例未配置账号"),
  );
  fireEvent.click(await screen.findByRole("button", { name: "验证并订阅" }));
  await screen.findByText("此实例未配置账号");
  expect(done).not.toHaveBeenCalled();
});
