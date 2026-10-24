export type CrashKind = "app-render" | "route-render" | "chunk-load";

export interface SafeCrashContext {
  kind: CrashKind;
  occurredAt: string;
  routeScope: string;
}

export interface PublicBuildIdentity {
  version: string;
  shortGitSha: string;
}

const kindLabels: Record<CrashKind, string> = {
  "app-render": "工作台界面显示",
  "route-render": "当前页面显示",
  "chunk-load": "页面资源加载",
};

function coarseRouteScope(): string {
  const hashRoute = window.location.hash.replace(/^#/, "").split(/[?#]/, 1)[0];
  const pathname = window.location.pathname.replace(/^\/console(?:\/|$)/, "/");
  const source = hashRoute.startsWith("/") ? hashRoute : pathname;
  const section = source.split("/").filter(Boolean)[0];
  return section ? `/${section}` : "/";
}

export function createSafeCrashContext(kind: CrashKind): SafeCrashContext {
  return {
    kind,
    occurredAt: new Date().toISOString(),
    routeScope: coarseRouteScope(),
  };
}

export function formatSafeCrashReport(
  context: SafeCrashContext,
  buildIdentity: PublicBuildIdentity | null,
): string {
  const lines = [
    "澜台界面故障安全定位信息",
    `发生时间：${context.occurredAt}`,
    `页面范围：${context.routeScope}`,
    `故障类别：${kindLabels[context.kind]}`,
  ];
  if (buildIdentity?.version) lines.push(`版本：${buildIdentity.version}`);
  if (buildIdentity?.shortGitSha) {
    lines.push(`构建：${buildIdentity.shortGitSha}`);
  }
  lines.push("说明：未收集对话、文档、密钥、异常正文或调用栈。");
  return lines.join("\n");
}
