import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "@/test/common_setup";
import type { BackupDetail, BackupMeta } from "@/api/types/backup";
import RestoreBackupModal from "./RestoreBackupModal";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) =>
      ({
        "backup.restoreConfirm": "我确认要恢复此备份",
        "backup.integrityVerified": "完整性校验通过",
        "backup.restoreSuccess": "备份恢复成功",
      })[key] ?? key,
  }),
}));

const mocks = vi.hoisted(() => ({
  getBackup: vi.fn(),
  restoreBackup: vi.fn(),
  getRestoreOperation: vi.fn(),
}));

vi.mock("@/api", () => ({
  default: {
    getBackup: mocks.getBackup,
    restoreBackup: mocks.restoreBackup,
    getRestoreOperation: mocks.getRestoreOperation,
  },
}));

const backup: BackupMeta = {
  id: "backup-1",
  name: "Before upgrade",
  description: "known-good state",
  created_at: "2026-09-19T10:00:00Z",
  scope: {
    include_agents: true,
    include_global_config: true,
    include_skill_pool: false,
    include_secrets: false,
  },
  agent_count: 2,
};

const detail: BackupDetail = {
  ...backup,
  workspace_stats: {
    alpha: { files: 3, size: 100, name: "Alpha" },
    beta: { files: 2, size: 80, name: "Beta" },
  },
  integrity_status: "verified",
  integrity_file_count: 5,
  integrity_total_bytes: 180,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("RestoreBackupModal submitted scope", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getBackup.mockResolvedValue(detail);
    mocks.getRestoreOperation.mockResolvedValue({
      operation_id: "unused",
      backup_id: backup.id,
      state: "unknown",
    });
  });

  it("freezes and displays the exact submitted scope until restore settles", async () => {
    const pending = deferred<{
      operation_id: string;
      backup_id: string;
      state: "applied";
    }>();
    mocks.restoreBackup.mockReturnValue(pending.promise);
    const onClose = vi.fn();

    renderWithProviders(
      <RestoreBackupModal
        open
        backup={backup}
        agents={[
          { id: "alpha", name: "Alpha", workspace_dir: "/alpha" },
          { id: "beta", name: "Beta", workspace_dir: "/beta" },
        ]}
        onClose={onClose}
        onSuccess={vi.fn()}
      />,
    );

    await screen.findByText("Alpha");
    await userEvent.click(
      screen.getByRole("checkbox", { name: "我确认要恢复此备份" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "恢复所选内容" }),
    );

    await waitFor(() => expect(mocks.restoreBackup).toHaveBeenCalledTimes(1));
    const [, submitted] = mocks.restoreBackup.mock.calls[0];
    expect(submitted).toMatchObject({
      mode: "custom",
      include_agents: true,
      agent_ids: ["alpha", "beta"],
      include_global_config: true,
    });
    expect(submitted.operation_id).toEqual(expect.any(String));

    expect(
      screen.getByRole("status", { name: "已提交恢复范围" }),
    ).toHaveTextContent("2 个智能体、全局设置");
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "恢复进行中" }),
    ).toBeDisabled();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();

    pending.resolve({
      operation_id: submitted.operation_id,
      backup_id: backup.id,
      state: "applied",
    });
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("reconciles a lost response without submitting the restore twice", async () => {
    mocks.restoreBackup.mockRejectedValue(new TypeError("Failed to fetch"));
    mocks.getRestoreOperation.mockResolvedValue({
      operation_id: "restore-op",
      backup_id: backup.id,
      state: "applied",
    });
    const onClose = vi.fn();
    const onSuccess = vi.fn();

    renderWithProviders(
      <RestoreBackupModal
        open
        backup={backup}
        agents={[]}
        onClose={onClose}
        onSuccess={onSuccess}
      />,
    );

    await screen.findByText("完整性校验通过", { exact: false });
    await userEvent.click(
      screen.getByRole("checkbox", { name: "我确认要恢复此备份" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "恢复所选内容" }),
    );

    await waitFor(() => expect(mocks.getRestoreOperation).toHaveBeenCalled());
    const operationId = mocks.restoreBackup.mock.calls[0][1].operation_id;
    expect(mocks.getRestoreOperation).toHaveBeenCalledWith(operationId);
    expect(mocks.restoreBackup).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps an unknown restore frozen and offers verification, not retry", async () => {
    mocks.restoreBackup.mockRejectedValue(new TypeError("Failed to fetch"));
    mocks.getRestoreOperation.mockRejectedValue(new Error("not found"));

    renderWithProviders(
      <RestoreBackupModal
        open
        backup={backup}
        agents={[]}
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    await screen.findByText("完整性校验通过", { exact: false });
    await userEvent.click(
      screen.getByRole("checkbox", { name: "我确认要恢复此备份" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "恢复所选内容" }),
    );

    expect(
      await screen.findByText("恢复结果待核实", { exact: false }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "核实恢复结果" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /retry|重试恢复/i }),
    ).not.toBeInTheDocument();
    expect(mocks.restoreBackup).toHaveBeenCalledTimes(1);
  });
});
