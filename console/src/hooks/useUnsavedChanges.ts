import { useEffect } from "react";
import type { ReactNode } from "react";

interface ConfirmController {
  confirm: (options: {
    title: ReactNode;
    content?: ReactNode;
    okText?: string;
    cancelText?: string;
    okButtonProps?: { danger?: boolean };
    onOk?: () => void | Promise<void>;
    onCancel?: () => void;
  }) => unknown;
}

export function useBeforeUnload(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return;
    const preventLoss = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", preventLoss);
    return () => window.removeEventListener("beforeunload", preventLoss);
  }, [dirty]);
}

export function confirmDiscardChanges(
  controller: ConfirmController,
  dirty: boolean,
  onDiscard: () => void,
  options?: {
    title?: ReactNode;
    content?: ReactNode;
    okText?: string;
    cancelText?: string;
  },
) {
  if (!dirty) {
    onDiscard();
    return;
  }
  controller.confirm({
    title: options?.title || "舍弃未保存的修改？",
    content:
      options?.content || "当前修改尚未保存。继续后，这些修改将无法恢复。",
    okText: options?.okText || "舍弃修改",
    cancelText: options?.cancelText || "继续编辑",
    okButtonProps: { danger: true },
    onOk: onDiscard,
  });
}

export async function requestDiscardChanges(
  controller: ConfirmController,
  dirty: boolean,
  options?: {
    title?: ReactNode;
    content?: ReactNode;
    okText?: string;
    cancelText?: string;
  },
): Promise<boolean> {
  if (!dirty) return true;
  return new Promise<boolean>((resolve) => {
    controller.confirm({
      title: options?.title || "舍弃未保存的修改？",
      content:
        options?.content || "当前修改尚未保存。继续后，这些修改将无法恢复。",
      okText: options?.okText || "舍弃修改",
      cancelText: options?.cancelText || "继续编辑",
      okButtonProps: { danger: true },
      onOk: () => resolve(true),
      onCancel: () => resolve(false),
    });
  });
}
