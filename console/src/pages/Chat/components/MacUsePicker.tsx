import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Empty, Modal, Spin, Tag, Typography } from "antd";
import { X } from "lucide-react";
import { IconButton } from "@/vendor/lantaiDesign";
import {
  macUseApi,
  type MacUseTargetSelection,
  type MacUseWindow,
} from "../../../api/modules/macUse";
import { useAppMessage } from "../../../hooks/useAppMessage";
import styles from "./MacUsePicker.module.less";

interface MacUseTargetDialogProps {
  open: boolean;
  value: MacUseTargetSelection | null;
  onChange: (target: MacUseTargetSelection | null) => void;
  onClose: () => void;
}

function displayTitle(window: MacUseWindow): string {
  return window.window_title || "未命名窗口";
}

export function MacUseTargetDialog({
  open,
  value,
  onChange,
  onClose,
}: MacUseTargetDialogProps) {
  const { message } = useAppMessage();
  const [loading, setLoading] = useState(false);
  const [selectingWindowId, setSelectingWindowId] = useState("");
  const [windows, setWindows] = useState<MacUseWindow[]>([]);
  const loadedWhileOpenRef = useRef(false);

  const loadWindows = useCallback(async () => {
    setLoading(true);
    try {
      const result = await macUseApi.listWindows();
      if (!result.ok) {
        throw new Error(result.error || "暂时无法读取 Mac 应用窗口");
      }
      const next = (result.data.windows || []).filter(
        (window) => window.app_bundle_id && window.window_id,
      );
      setWindows(next);
    } catch (error) {
      setWindows([]);
      message.error(
        error instanceof Error ? error.message : "读取应用窗口失败",
      );
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    if (!open) {
      loadedWhileOpenRef.current = false;
      return;
    }
    if (loadedWhileOpenRef.current) return;
    loadedWhileOpenRef.current = true;
    void loadWindows();
  }, [loadWindows, open]);

  const groupedWindows = useMemo(() => {
    return [...windows].sort((left, right) => {
      if (left.on_screen !== right.on_screen) return left.on_screen ? -1 : 1;
      return `${left.owner_name}:${left.window_title}`.localeCompare(
        `${right.owner_name}:${right.window_title}`,
        "zh-CN",
      );
    });
  }, [windows]);

  return (
    <Modal
      open={open}
      title="选择要操作的 Mac 应用（实验）"
      width={560}
      footer={
        <Button onClick={onClose}>{value ? "保留当前选择" : "取消"}</Button>
      }
      onCancel={onClose}
    >
      <Typography.Paragraph type="secondary">
        这是实验能力，已验证 TextEdit
        控件读写，其他应用会按实时探测结果成功或明确拒绝。
        选择只确定这次任务的目标窗口；读取或操作所需的系统权限仍由 macOS
        和当前任务权限共同控制。
      </Typography.Paragraph>
      <Spin spinning={loading}>
        {groupedWindows.length ? (
          <div
            className={styles.windowList}
            role="listbox"
            aria-label="Mac 应用窗口"
          >
            {groupedWindows.map((window) => {
              const selected =
                value?.app_bundle_id === window.app_bundle_id &&
                value?.window_id === window.window_id;
              return (
                <button
                  key={`${window.app_bundle_id}:${window.window_id}`}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className={`${styles.windowButton} ${
                    selected ? styles.windowButtonSelected : ""
                  }`}
                  disabled={Boolean(selectingWindowId)}
                  onClick={() => {
                    setSelectingWindowId(window.window_id);
                    const selectTarget =
                      globalThis.window.electronAPI?.selectMacUseTarget;
                    void (
                      selectTarget
                        ? selectTarget({
                            app_bundle_id: window.app_bundle_id,
                            window_id: window.window_id,
                            process_id: window.process_id,
                            process_started_at: window.process_started_at,
                            window_title: window.window_title,
                          })
                        : Promise.reject(
                            new Error("当前环境没有连接 Mac 应用操作组件"),
                          )
                    )
                      .then((result) => {
                        const receipt = String(
                          result.data?.selection_receipt || "",
                        );
                        if (!result.ok || !receipt) {
                          throw new Error(
                            result.error || "目标窗口已经变化，请重新选择",
                          );
                        }
                        onChange({
                          app_bundle_id: window.app_bundle_id,
                          window_id: window.window_id,
                          process_id: window.process_id,
                          process_started_at: window.process_started_at,
                          window_title: window.window_title,
                          owner_name: window.owner_name,
                          selection_receipt: receipt,
                        });
                        onClose();
                      })
                      .catch((error) => {
                        message.error(
                          error instanceof Error
                            ? error.message
                            : "选择应用窗口失败",
                        );
                      })
                      .finally(() => setSelectingWindowId(""));
                  }}
                >
                  <span className={styles.windowCopy}>
                    <strong>{window.owner_name}</strong>
                    <span>{displayTitle(window)}</span>
                  </span>
                  <Tag color={window.on_screen ? "green" : "default"}>
                    {selectingWindowId === window.window_id
                      ? "正在确认"
                      : window.on_screen
                        ? "当前桌面"
                        : "其他桌面"}
                  </Tag>
                </button>
              );
            })}
          </div>
        ) : loading ? null : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="没有可选窗口。请先打开目标应用，再刷新。"
          >
            <Button onClick={() => void loadWindows()}>重新读取</Button>
          </Empty>
        )}
      </Spin>
    </Modal>
  );
}

export function MacUseTargetBanner({
  target,
  onClear,
}: {
  target: MacUseTargetSelection | null;
  onClear: () => void;
}) {
  if (!target) return null;
  return (
    <div className={styles.targetBanner} aria-label="当前 Mac 应用目标">
      <span className={styles.targetBannerCopy}>
        <strong>将操作：{target.owner_name}</strong>
        <span>{target.window_title || "未命名窗口"}</span>
      </span>
      <IconButton
        bordered={false}
        aria-label="移除 Mac 应用目标"
        icon={<X size={15} />}
        onClick={onClear}
      />
    </div>
  );
}

export type { MacUseTargetSelection };
