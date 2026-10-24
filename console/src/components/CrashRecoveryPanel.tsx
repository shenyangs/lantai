import { useEffect, useMemo, useState } from "react";
import { Button } from "antd";
import {
  formatSafeCrashReport,
  type PublicBuildIdentity,
  type SafeCrashContext,
} from "./crashRecovery";

interface CrashRecoveryPanelProps {
  context: SafeCrashContext;
  buildIdentity?: PublicBuildIdentity | null;
  onRetry: () => void;
  onReload: () => void;
}

function safeIdentityValue(value: unknown, pattern: RegExp): string {
  const normalized = typeof value === "string" ? value.trim() : "";
  return pattern.test(normalized) ? normalized : "";
}

async function readPublicBuildIdentity(): Promise<PublicBuildIdentity | null> {
  try {
    const response = await fetch("./release-provenance.json", {
      cache: "no-store",
    });
    if (response.ok) {
      const payload = (await response.json()) as Record<string, unknown>;
      const version = safeIdentityValue(
        payload.version,
        /^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$/,
      );
      const shortGitSha = safeIdentityValue(
        payload.shortGitSha,
        /^[0-9a-fA-F]{7,64}$/,
      );
      if (version || shortGitSha) return { version, shortGitSha };
    }
  } catch {
    // Development previews may not have packaged release provenance.
  }

  try {
    const result = await window.electronAPI?.getAppVersion?.();
    const version = safeIdentityValue(
      result?.ok ? result.version : "",
      /^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$/,
    );
    return version ? { version, shortGitSha: "" } : null;
  } catch {
    return null;
  }
}

export function CrashRecoveryPanel({
  context,
  buildIdentity,
  onRetry,
  onReload,
}: CrashRecoveryPanelProps) {
  const [resolvedIdentity, setResolvedIdentity] =
    useState<PublicBuildIdentity | null>(buildIdentity ?? null);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">(
    "idle",
  );

  useEffect(() => {
    if (buildIdentity !== undefined) {
      setResolvedIdentity(buildIdentity);
      return;
    }
    let active = true;
    void readPublicBuildIdentity().then((identity) => {
      if (active) setResolvedIdentity(identity);
    });
    return () => {
      active = false;
    };
  }, [buildIdentity]);

  const report = useMemo(
    () => formatSafeCrashReport(context, resolvedIdentity),
    [context, resolvedIdentity],
  );

  const copyReport = async () => {
    setCopyState("idle");
    try {
      if (!navigator.clipboard?.writeText) throw new Error("unavailable");
      await navigator.clipboard.writeText(report);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  return (
    <div>
      <p>
        界面显示中断不等于后台任务未执行或未生效。请先核实原任务状态，再决定是否重新操作。
      </p>
      <p>
        可复制不含对话、文档、密钥或错误正文的最小定位信息。定位信息不会自动上传。
      </p>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          gap: 8,
          marginTop: 16,
        }}
      >
        <Button type="primary" autoFocus onClick={onRetry}>
          重新显示当前界面
        </Button>
        <Button onClick={() => void copyReport()}>复制安全定位信息</Button>
        <Button onClick={onReload}>刷新应用</Button>
      </div>
      {copyState === "copied" ? (
        <p role="status">安全定位信息已复制</p>
      ) : copyState === "failed" ? (
        <p role="alert">未能复制，请先保留当前页面并记录发生时间。</p>
      ) : null}
    </div>
  );
}
