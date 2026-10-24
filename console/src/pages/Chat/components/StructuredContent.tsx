import React, { useRef, useState } from "react";
import { Dropdown, Tooltip, type MenuProps } from "antd";
import {
  Check,
  Clapperboard,
  Code2,
  Copy,
  Download,
  ExternalLink,
  FileText,
  GalleryHorizontalEnd,
  Info,
  Mail,
  Megaphone,
  MoreHorizontal,
  PenLine,
  Table2,
  type LucideIcon,
} from "lucide-react";
import type { ComponentProps } from "@ant-design/x-markdown";

import { useAppMessage } from "../../../hooks/useAppMessage";
import { worksApi } from "../../../api/modules/works";
import { useAgentStore } from "../../../stores/agentStore";
import { copyText } from "../utils";
import type {
  StructuredOutputSegment,
  TextArtifactKind,
} from "./StructuredTextArtifacts";
import type { UtilityResearchAdvisory } from "@/api/types";

const EChartsArtifact = React.lazy(async () => {
  const module = await import("./EChartsArtifact");
  return { default: module.EChartsArtifact };
});

const AnalysisChartArtifact = React.lazy(async () => {
  const module = await import("./AnalysisChartArtifact");
  return { default: module.AnalysisChartArtifact };
});

type StructuredComponentProps = ComponentProps & {
  children?: React.ReactNode;
  className?: string;
  href?: string;
};

const DOCUMENT_EXTENSIONS = new Set([
  "csv",
  "doc",
  "docx",
  "json",
  "ksheet",
  "md",
  "odt",
  "otl",
  "pdf",
  "ppt",
  "pptx",
  "txt",
  "xls",
  "xlsx",
  "zip",
]);

function childrenText(children: React.ReactNode): string {
  return React.Children.toArray(children)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") {
        return String(child);
      }
      if (!React.isValidElement<{ children?: React.ReactNode }>(child)) {
        return "";
      }
      return childrenText(child.props.children);
    })
    .join("");
}

function fileExtension(value: string): string {
  const clean = value.split(/[?#]/, 1)[0] || "";
  const match = clean.match(/\.([a-z0-9]+)$/i);
  return match?.[1]?.toLowerCase() || "";
}

function fileNameFromUrl(value: string): string {
  const clean = value.split(/[?#]/, 1)[0] || "";
  const last = clean.split("/").pop() || "";
  try {
    return decodeURIComponent(last) || "文件";
  } catch {
    return last || "文件";
  }
}

function sanitizeFileName(value: string, fallback: string): string {
  const cleaned = value
    .trim()
    .replace(/[<>:"/\\|?*]/g, "-")
    .replace(/\s+/g, " ")
    .slice(0, 80);
  return cleaned || fallback;
}

function currentChatIdFromLocation(): string {
  const match = (window.location.hash || "").match(/(?:^#)?\/chat\/([^/?#]+)/);
  if (!match?.[1]) return "";
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

function downloadTextFile(content: string, name: string, type: string) {
  if (!URL.createObjectURL) return;
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function ArtifactAction({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Tooltip title={label}>
      <button
        type="button"
        className="lantai-artifact-action"
        aria-label={label}
        onClick={onClick}
      >
        {children}
      </button>
    </Tooltip>
  );
}

function useCopyAction(content: string) {
  const { message } = useAppMessage();
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await copyText(content);
      setCopied(true);
      message.success("已复制");
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      message.error("复制失败");
    }
  };

  return { copied, copy };
}

function StructuredCodeBlock(props: StructuredComponentProps) {
  const code = childrenText(props.children).replace(/\n$/, "");
  const child = React.Children.toArray(props.children)[0];
  const childClassName = React.isValidElement<{ className?: string }>(child)
    ? child.props.className || ""
    : "";
  const language =
    props.lang?.split(/\s+/, 1)[0] ||
    childClassName.match(/language-([\w+-]+)/)?.[1] ||
    "代码";
  const { copied, copy } = useCopyAction(code);
  const complete = props.streamStatus === "done";

  if (language.toLowerCase() === "echarts" && complete) {
    return (
      <React.Suspense
        fallback={
          <section data-lantai-artifact="echarts-loading" role="status">
            正在绘制图表…
          </section>
        }
      >
        <EChartsArtifact source={code} />
      </React.Suspense>
    );
  }

  if (language.toLowerCase() === "analysis-chart" && complete) {
    return (
      <React.Suspense fallback={<section role="status">正在读取分析图表…</section>}>
        <AnalysisChartArtifact source={code} />
      </React.Suspense>
    );
  }

  return (
    <section
      className="lantai-artifact lantai-code-artifact"
      data-lantai-artifact="code"
    >
      <header className="lantai-artifact-header">
        <span className="lantai-artifact-title">
          <Code2 size={15} aria-hidden="true" />
          <span>{language}</span>
        </span>
        {complete ? (
          <div className="lantai-artifact-actions">
            <ArtifactAction
              label={copied ? "已复制代码" : "复制代码"}
              onClick={copy}
            >
              {copied ? (
                <Check size={15} aria-hidden="true" />
              ) : (
                <Copy size={15} aria-hidden="true" />
              )}
            </ArtifactAction>
          </div>
        ) : null}
      </header>
      <pre>
        <code className={childClassName || undefined}>{code}</code>
      </pre>
    </section>
  );
}

function tableRows(table: HTMLTableElement | null): string[][] {
  if (!table) return [];
  return Array.from(table.rows).map((row) =>
    Array.from(row.cells).map((cell) => cell.textContent?.trim() || ""),
  );
}

function rowsAsTsv(rows: string[][]): string {
  return rows.map((row) => row.join("\t")).join("\n");
}

function rowsAsCsv(rows: string[][]): string {
  return rows
    .map((row) => row.map((cell) => `"${cell.replace(/"/g, '""')}"`).join(","))
    .join("\n");
}

function StructuredTable(props: StructuredComponentProps) {
  const tableRef = useRef<HTMLTableElement>(null);
  const { message } = useAppMessage();
  const [copied, setCopied] = useState(false);
  const complete = props.streamStatus === "done";

  const copy = async () => {
    try {
      await copyText(rowsAsTsv(tableRows(tableRef.current)));
      setCopied(true);
      message.success("表格已复制");
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      message.error("复制失败");
    }
  };

  const download = () => {
    downloadTextFile(
      `\ufeff${rowsAsCsv(tableRows(tableRef.current))}`,
      "澜台表格.csv",
      "text/csv;charset=utf-8",
    );
  };

  return (
    <figure
      className="lantai-artifact lantai-table-artifact"
      data-lantai-artifact="table"
    >
      <figcaption className="lantai-artifact-header">
        <span className="lantai-artifact-title">
          <Table2 size={15} aria-hidden="true" />
          <span>表格</span>
        </span>
        {complete ? (
          <div className="lantai-artifact-actions">
            <ArtifactAction
              label={copied ? "已复制表格" : "复制表格"}
              onClick={copy}
            >
              {copied ? (
                <Check size={15} aria-hidden="true" />
              ) : (
                <Copy size={15} aria-hidden="true" />
              )}
            </ArtifactAction>
            <ArtifactAction label="下载 CSV" onClick={download}>
              <Download size={15} aria-hidden="true" />
            </ArtifactAction>
          </div>
        ) : null}
      </figcaption>
      <div className="lantai-table-artifact-scroll">
        <table ref={tableRef} className={props.className}>
          {props.children}
        </table>
      </div>
    </figure>
  );
}

function StructuredLink(props: StructuredComponentProps) {
  const href = String(props.href || "");
  const extension = fileExtension(href);
  if (!DOCUMENT_EXTENSIONS.has(extension)) {
    return (
      <a href={href} target="_blank" rel="noreferrer">
        {props.children}
      </a>
    );
  }

  return (
    <span className="lantai-file-link" data-lantai-artifact-link={extension}>
      <FileText size={15} aria-hidden="true" />
      <a href={href} target="_blank" rel="noreferrer">
        {props.children || fileNameFromUrl(href)}
      </a>
      <span className="lantai-file-link-type">{extension.toUpperCase()}</span>
      <ExternalLink size={13} aria-hidden="true" />
    </span>
  );
}

export const structuredMarkdownComponents = {
  pre: StructuredCodeBlock,
  table: StructuredTable,
  a: StructuredLink,
};

export function isDocumentLikeMarkdown(content: string): boolean {
  const headings = content.match(/^#{1,3}\s+\S.+$/gm) || [];
  if (headings.length >= 2) return true;

  const firstHeading = headings[0]?.replace(/^#{1,3}\s+/, "").trim() || "";
  const explicitDocumentTitle =
    /报告|方案|计划|规划|纪要|清单|SOP|教程|指南|手册|简历|大纲|总结|复盘|需求文档|说明书|制度|流程|提纲|策划|周报|日报|月报/i.test(
      firstHeading,
    );
  const body = headings[0]
    ? content
        .slice((content.indexOf(headings[0]) || 0) + headings[0].length)
        .trim()
    : "";
  return explicitDocumentTitle && Boolean(body);
}

export function DocumentOutput({
  content,
  children,
}: {
  content: string;
  children: React.ReactNode;
}) {
  const firstHeading = content.match(/^#{1,3}\s+(.+)$/m)?.[1]?.trim() || "文档";
  const fileName = `${sanitizeFileName(firstHeading, "澜台文档")}.md`;
  const { copied, copy } = useCopyAction(content);

  return (
    <section
      className="lantai-artifact lantai-document-output"
      data-lantai-artifact="document"
    >
      <header className="lantai-document-output-header">
        <span className="lantai-artifact-title">
          <FileText size={15} aria-hidden="true" />
          <span>文档</span>
          <strong>{firstHeading}</strong>
        </span>
        <div className="lantai-artifact-actions">
          <ArtifactAction
            label={copied ? "已复制文档" : "复制文档"}
            onClick={copy}
          >
            {copied ? (
              <Check size={15} aria-hidden="true" />
            ) : (
              <Copy size={15} aria-hidden="true" />
            )}
          </ArtifactAction>
          <ArtifactAction
            label="下载 Markdown"
            onClick={() =>
              downloadTextFile(content, fileName, "text/markdown;charset=utf-8")
            }
          >
            <Download size={15} aria-hidden="true" />
          </ArtifactAction>
        </div>
      </header>
      <div className="lantai-document-output-body">{children}</div>
    </section>
  );
}

const TEXT_ARTIFACT_ICONS: Record<TextArtifactKind, LucideIcon> = {
  creative: PenLine,
  correspondence: Mail,
  copy: Megaphone,
  script: Clapperboard,
};

export function StandaloneTextArtifact({
  artifact,
  complete,
  children,
}: {
  artifact: Extract<StructuredOutputSegment, { type: "artifact" }>;
  complete: boolean;
  children: React.ReactNode;
}) {
  const Icon = TEXT_ARTIFACT_ICONS[artifact.kind];
  const { copied, copy } = useCopyAction(artifact.copyContent);
  const fileName = `${sanitizeFileName(artifact.title, artifact.label)}.${
    artifact.fileExtension
  }`;

  return (
    <section
      className="lantai-artifact lantai-text-artifact"
      data-lantai-artifact="text"
      data-lantai-artifact-kind={artifact.kind}
      data-streaming={complete ? "false" : "true"}
    >
      <header className="lantai-artifact-header">
        <span className="lantai-artifact-title">
          <Icon size={15} aria-hidden={true} />
          <span>{artifact.label}</span>
          <strong>{artifact.title}</strong>
        </span>
        {complete ? (
          <div className="lantai-artifact-actions">
            <ArtifactAction
              label={
                copied ? `已复制${artifact.label}` : `复制${artifact.label}`
              }
              onClick={copy}
            >
              {copied ? (
                <Check size={15} aria-hidden="true" />
              ) : (
                <Copy size={15} aria-hidden="true" />
              )}
            </ArtifactAction>
            <ArtifactAction
              label="下载文本"
              onClick={() =>
                downloadTextFile(
                  artifact.copyContent,
                  fileName,
                  "text/plain;charset=utf-8",
                )
              }
            >
              <Download size={15} aria-hidden="true" />
            </ArtifactAction>
          </div>
        ) : null}
      </header>
      <div className="lantai-text-artifact-body">{children}</div>
    </section>
  );
}

export function ResearchAdvisoryCard({
  advisory,
}: {
  advisory: UtilityResearchAdvisory;
}) {
  return (
    <aside
      className="lantai-research-advisory"
      data-lantai-research-advisory="1"
      role="note"
      aria-label={advisory.label}
    >
      <div className="lantai-research-advisory-title">
        <Info size={15} aria-hidden="true" />
        <strong>{advisory.label}</strong>
      </div>
      <p>{advisory.body}</p>
    </aside>
  );
}

function formatFileSize(value: unknown): string {
  const size = Number(value);
  if (!Number.isFinite(size) || size <= 0) return "";
  if (size < 1024) return `${Math.round(size)} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function localFileSystemPath(value?: string): string {
  const source = value?.trim() || "";
  if (!source) return "";
  if (/^[a-zA-Z]:[\\/]/.test(source) || source.startsWith("/")) {
    return source;
  }
  if (!source.startsWith("file://")) return "";
  try {
    const parsed = new URL(source);
    let pathname = decodeURIComponent(parsed.pathname);
    if (parsed.host) return `//${parsed.host}${pathname}`;
    if (/^\/[a-zA-Z]:\//.test(pathname)) pathname = pathname.slice(1);
    return pathname;
  } catch {
    try {
      return decodeURIComponent(source.slice("file://".length));
    } catch {
      return source.slice("file://".length);
    }
  }
}

export function RuntimeFileCard({
  url,
  sourceUrl,
  name,
  size,
}: {
  url?: string;
  sourceUrl?: string;
  name?: string;
  size?: unknown;
}) {
  const { message } = useAppMessage();
  const [savedAsWork, setSavedAsWork] = useState(false);
  const [savingAsWork, setSavingAsWork] = useState(false);
  const fileName = name?.trim() || fileNameFromUrl(url || "");
  const extension = fileExtension(fileName || url || "");
  const sizeLabel = formatFileSize(size);
  const fileType = extension ? extension.toUpperCase() : "文件";
  const canEditInLantai = [
    "docx",
    "xlsx",
    "xls",
    "csv",
    "pptx",
    "pdf",
    "md",
    "markdown",
  ].includes(extension);
  const previewUrl = sourceUrl || url;
  const systemPath = localFileSystemPath(sourceUrl || url);
  const canUseNativeActions = Boolean(
    systemPath && window.electronAPI?.isDesktop,
  );
  const canSaveNativeFile = Boolean(
    canUseNativeActions && window.electronAPI?.saveFileAs,
  );
  const runNativeAction = async (action: "open" | "openWith" | "reveal") => {
    const api = window.electronAPI;
    const handler =
      action === "open"
        ? api?.openPath
        : action === "openWith"
          ? api?.openWith
          : api?.revealPath;
    if (!systemPath || !api?.isDesktop || !handler) {
      message.info("请在澜台桌面版中使用系统打开功能");
      return;
    }
    try {
      const result = await handler(systemPath);
      if (
        result?.success ||
        ("canceled" in result && Boolean(result.canceled))
      ) {
        return;
      }
      if ("revealed" in result && result.revealed) {
        message.info("已在 Finder 中显示，请右键选择“打开方式”");
        return;
      }
      message.error(result?.error || "系统无法打开此文件");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "系统打开失败");
    }
  };
  const openInLantaiEditor = async () => {
    const api = window.electronAPI;
    const openEditor = api?.openOfficeEditor || api?.openDocxEditor;
    if (!systemPath || !api?.isDesktop || !openEditor) {
      message.info("请在澜台桌面版中使用本地 Office 编辑功能");
      return;
    }
    try {
      const result = await openEditor(systemPath, {
        source_chat_id: currentChatIdFromLocation(),
        agent_id: useAgentStore.getState().selectedAgent,
      });
      if (result?.success) return;
      message.error(result?.error || "澜台本地编辑器无法打开此文件");
    } catch (reason) {
      message.error(
        reason instanceof Error ? reason.message : "澜台本地编辑器打开失败",
      );
    }
  };
  const saveLocalFile = async () => {
    const saveFileAs = window.electronAPI?.saveFileAs;
    if (!systemPath || !saveFileAs) {
      message.error("桌面文件保存功能不可用");
      return;
    }
    try {
      const result = await saveFileAs({
        sourcePath: systemPath,
        filename: fileName,
      });
      if (result.success) {
        message.success("文件已保存");
        return;
      }
      if (result.canceled) return;
      message.error(result.error || "文件保存失败");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "文件保存失败");
    }
  };
  const addToWorks = async () => {
    if (!systemPath || savedAsWork || savingAsWork) return;
    setSavingAsWork(true);
    try {
      await worksApi.add({
        path: systemPath,
        title: fileName.replace(/\.[^.]+$/, ""),
        source_chat_id: currentChatIdFromLocation() || null,
      });
      setSavedAsWork(true);
      message.success("已加入作品");
    } catch (reason) {
      message.error(reason instanceof Error ? reason.message : "加入作品失败");
    } finally {
      setSavingAsWork(false);
    }
  };
  const nativeActionItems: MenuProps["items"] = canUseNativeActions
    ? [
        {
          key: "open-default",
          label: "用默认应用打开（推荐）",
          onClick: () => void runNativeAction("open"),
        },
        {
          key: "open-with",
          label: "选择应用打开…",
          onClick: () => void runNativeAction("openWith"),
        },
        { type: "divider" },
        {
          key: "reveal",
          label: "在 Finder 中显示",
          onClick: () => void runNativeAction("reveal"),
        },
      ]
    : [];

  const card = (
    <section
      className="lantai-artifact lantai-file-artifact"
      data-lantai-artifact="file"
    >
      <div className="lantai-file-artifact-icon" aria-hidden="true">
        <FileText size={20} />
      </div>
      {canUseNativeActions ? (
        <button
          type="button"
          className="lantai-file-artifact-copy"
          aria-label={
            canEditInLantai
              ? `在澜台中编辑：${fileName}`
              : `打开文件：${fileName}`
          }
          onClick={() =>
            void (canEditInLantai
              ? openInLantaiEditor()
              : runNativeAction("open"))
          }
        >
          <strong>{fileName}</strong>
          <span>
            {fileType}
            {sizeLabel ? ` · ${sizeLabel}` : ""}
          </span>
        </button>
      ) : previewUrl ? (
        <a
          className="lantai-file-artifact-copy"
          href={previewUrl}
          target={systemPath ? undefined : "_blank"}
          rel={systemPath ? undefined : "noreferrer"}
          aria-label={`预览文件：${fileName}`}
        >
          <strong>{fileName}</strong>
          <span>
            {fileType}
            {sizeLabel ? ` · ${sizeLabel}` : ""}
          </span>
        </a>
      ) : (
        <div className="lantai-file-artifact-copy">
          <strong>{fileName}</strong>
          <span>
            {fileType}
            {sizeLabel ? ` · ${sizeLabel}` : ""}
          </span>
        </div>
      )}
      {url || canUseNativeActions ? (
        <div className="lantai-artifact-actions">
          {canUseNativeActions ? (
            <Tooltip
              title={
                savedAsWork
                  ? "已加入作品"
                  : savingAsWork
                    ? "正在加入作品"
                    : "加入作品"
              }
            >
              <button
                type="button"
                className="lantai-artifact-action"
                aria-label={savedAsWork ? "已加入作品" : "加入作品"}
                aria-pressed={savedAsWork}
                disabled={savedAsWork || savingAsWork}
                onClick={() => void addToWorks()}
              >
                {savedAsWork ? (
                  <Check size={15} aria-hidden="true" />
                ) : (
                  <GalleryHorizontalEnd size={15} aria-hidden="true" />
                )}
              </button>
            </Tooltip>
          ) : null}
          {canUseNativeActions ? (
            <Tooltip title={canEditInLantai ? "在澜台中编辑" : "打开文件"}>
              <button
                type="button"
                className="lantai-artifact-action"
                aria-label={canEditInLantai ? "在澜台中编辑" : "打开文件"}
                onClick={() =>
                  void (canEditInLantai
                    ? openInLantaiEditor()
                    : runNativeAction("open"))
                }
              >
                {canEditInLantai ? (
                  <PenLine size={15} aria-hidden="true" />
                ) : (
                  <ExternalLink size={15} aria-hidden="true" />
                )}
              </button>
            </Tooltip>
          ) : previewUrl ? (
            <Tooltip title="打开文件">
              <a
                className="lantai-artifact-action"
                href={previewUrl}
                target={systemPath ? undefined : "_blank"}
                rel={systemPath ? undefined : "noreferrer"}
                aria-label="打开文件"
              >
                <ExternalLink size={15} aria-hidden="true" />
              </a>
            </Tooltip>
          ) : null}
          {canUseNativeActions ? (
            <Dropdown
              menu={{ items: nativeActionItems }}
              trigger={["click"]}
              placement="bottomRight"
              destroyOnHidden
            >
              <Tooltip title="更多打开方式">
                <button
                  type="button"
                  className="lantai-artifact-action"
                  aria-label="更多文件操作"
                >
                  <MoreHorizontal size={15} aria-hidden="true" />
                </button>
              </Tooltip>
            </Dropdown>
          ) : null}
          {canSaveNativeFile ? (
            <Tooltip title="下载文件">
              <button
                type="button"
                className="lantai-artifact-action"
                aria-label="下载文件"
                onClick={() => void saveLocalFile()}
              >
                <Download size={15} aria-hidden="true" />
              </button>
            </Tooltip>
          ) : url ? (
            <Tooltip title="下载文件">
              <a
                className="lantai-artifact-action"
                href={url}
                download={fileName}
                aria-label="下载文件"
              >
                <Download size={15} aria-hidden="true" />
              </a>
            </Tooltip>
          ) : null}
        </div>
      ) : null}
    </section>
  );

  return canUseNativeActions ? (
    <Dropdown
      menu={{ items: nativeActionItems }}
      trigger={["contextMenu"]}
      destroyOnHidden
    >
      {card}
    </Dropdown>
  ) : (
    card
  );
}
