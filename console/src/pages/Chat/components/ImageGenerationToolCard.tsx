import { useMemo, useState } from "react";
import {
  Check,
  Copy,
  Download,
  Image as ImageIcon,
  LoaderCircle,
  TriangleAlert,
} from "lucide-react";
import { Image as PreviewImage } from "antd";
import { useAppMessage } from "@/hooks/useAppMessage";
import { Tooltip } from "@/vendor/lantaiDesign";
import { useTranslation } from "react-i18next";
import { toDisplayUrl } from "../utils";
import styles from "./ImageGenerationToolCard.module.less";

interface RuntimeToolContent {
  data?: {
    name?: string;
    arguments?: unknown;
    output?: unknown;
    error?: unknown;
  };
}

export interface ImageGenerationToolData {
  status?: string;
  content?: RuntimeToolContent[];
}

interface ImageGenerationToolCardProps {
  data?: ImageGenerationToolData;
}

interface GeneratedImage {
  url: string;
  mimeType?: string;
  localPaths?: string[];
}

interface ImageProvenance {
  operation: "generate" | "edit";
  generationId: string;
  parentArtifactId: string;
  referenceCount: number;
}

function tryParseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed || !/^[{[]/.test(trimmed)) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function collectImages(
  value: unknown,
  images: GeneratedImage[],
  visited = new Set<unknown>(),
) {
  const parsed = tryParseJson(value);
  if (!parsed || typeof parsed !== "object" || visited.has(parsed)) return;
  visited.add(parsed);

  if (Array.isArray(parsed)) {
    parsed.forEach((item) => collectImages(item, images, visited));
    return;
  }

  const record = parsed as Record<string, unknown>;
  if (record.type === "image") {
    const source =
      record.source && typeof record.source === "object"
        ? (record.source as Record<string, unknown>)
        : {};
    const url = [record.image_url, record.url, source.url].find(
      (item): item is string => typeof item === "string" && !!item.trim(),
    );
    if (url && !images.some((item) => item.url === url)) {
      const metadata =
        record.metadata && typeof record.metadata === "object"
          ? (record.metadata as Record<string, unknown>)
          : {};
      const localPaths = [metadata.managed_path, metadata.workspace_path]
        .filter(
          (item): item is string =>
            typeof item === "string" && item.trim().length > 0,
        )
        .map((item) => item.trim())
        .filter((item, index, values) => values.indexOf(item) === index);
      images.push({
        url,
        mimeType:
          typeof record.mime_type === "string" ? record.mime_type : undefined,
        localPaths,
      });
    }
  }

  for (const key of ["content", "output", "result", "data", "assets"]) {
    if (key in record) collectImages(record[key], images, visited);
  }
}

function collectText(value: unknown, texts: string[], depth = 0) {
  if (depth > 5 || value === undefined || value === null) return;
  const parsed = tryParseJson(value);
  if (typeof parsed === "string") {
    if (parsed.trim()) texts.push(parsed.trim());
    return;
  }
  if (Array.isArray(parsed)) {
    parsed.forEach((item) => collectText(item, texts, depth + 1));
    return;
  }
  if (!parsed || typeof parsed !== "object") return;
  const record = parsed as Record<string, unknown>;
  if (record.type === "text" && typeof record.text === "string") {
    texts.push(record.text.trim());
    return;
  }
  for (const key of ["content", "output", "result", "message", "text"]) {
    if (key in record) collectText(record[key], texts, depth + 1);
  }
}

function readPrompt(value: unknown): string {
  const parsed = tryParseJson(value);
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const prompt = (parsed as Record<string, unknown>).prompt;
    return typeof prompt === "string" ? prompt.trim() : "";
  }
  return "";
}

function readReferenceCount(value: unknown): number {
  const parsed = tryParseJson(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return 0;
  const references = (parsed as Record<string, unknown>).reference_images;
  return Array.isArray(references) ? references.length : 0;
}

function findProvenance(
  value: unknown,
  visited = new Set<unknown>(),
): ImageProvenance | null {
  const parsed = tryParseJson(value);
  if (!parsed || typeof parsed !== "object" || visited.has(parsed)) return null;
  visited.add(parsed);
  if (Array.isArray(parsed)) {
    for (const item of parsed) {
      const found = findProvenance(item, visited);
      if (found) return found;
    }
    return null;
  }
  const record = parsed as Record<string, unknown>;
  const operation = String(record.operation || "").toLowerCase();
  if (operation === "generate" || operation === "edit") {
    return {
      operation,
      generationId:
        typeof record.generation_id === "string" ? record.generation_id : "",
      parentArtifactId:
        typeof record.parent_artifact_id === "string"
          ? record.parent_artifact_id
          : "",
      referenceCount: Array.isArray(record.reference_images)
        ? record.reference_images.length
        : 0,
    };
  }
  for (const key of ["metadata", "content", "output", "result", "data"]) {
    if (!(key in record)) continue;
    const found = findProvenance(record[key], visited);
    if (found) return found;
  }
  return null;
}

function readError(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (!value) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function imageFileName(url: string, index: number): string {
  try {
    const name = decodeURIComponent(new URL(url, window.location.href).pathname)
      .split("/")
      .filter(Boolean)
      .pop();
    if (name && /\.[a-z0-9]{2,5}$/i.test(name)) return name;
  } catch {
    // Fall back to a stable generated-image name.
  }
  return `lantai-generated-image-${index + 1}.png`;
}

export default function ImageGenerationToolCard({
  data,
}: ImageGenerationToolCardProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const [copiedUrl, setCopiedUrl] = useState("");
  const call = data?.content?.[0]?.data;
  const result = data?.content?.[1]?.data;
  const hasTerminalResult = (data?.content?.length ?? 0) > 1;
  const output = result?.output;
  const error = readError(result?.error);
  const statusValue = String(data?.status || "").toLowerCase();
  const prompt = readPrompt(call?.arguments);
  const argumentReferenceCount = readReferenceCount(call?.arguments);
  const provenance = useMemo(() => findProvenance(output), [output]);
  const images = useMemo(() => {
    const found: GeneratedImage[] = [];
    collectImages(output, found);
    return found;
  }, [output]);
  const summary = useMemo(() => {
    const texts: string[] = [];
    collectText(output, texts);
    return texts.filter(Boolean).join("\n");
  }, [output]);
  const failureMessage = error || (images.length === 0 ? summary : "");
  const failed =
    Boolean(error) ||
    statusValue.includes("fail") ||
    statusValue.includes("error") ||
    (hasTerminalResult &&
      statusValue.includes("complete") &&
      images.length === 0);
  const running =
    !failed &&
    images.length === 0 &&
    (!hasTerminalResult ||
      statusValue.includes("run") ||
      statusValue.includes("progress") ||
      !statusValue.includes("complete"));

  const copyImage = async (image: GeneratedImage) => {
    try {
      const displayUrl = toDisplayUrl(image.url);
      if (window.electronAPI?.copyImageFromUrl) {
        const result = await window.electronAPI.copyImageFromUrl({
          url: displayUrl,
          ...(image.localPaths?.length ? { localPaths: image.localPaths } : {}),
        });
        if (!result.success) {
          throw new Error(result.error || "Unable to copy image");
        }
      } else {
        const response = await fetch(displayUrl);
        if (!response.ok) {
          throw new Error(`Unable to fetch image (${response.status})`);
        }
        const blob = await response.blob();
        const clipboardType = blob.type || image.mimeType || "image/png";
        await navigator.clipboard.write([
          new ClipboardItem({ [clipboardType]: blob }),
        ]);
      }
      setCopiedUrl(image.url);
      window.setTimeout(() => setCopiedUrl(""), 1200);
    } catch (error) {
      setCopiedUrl("");
      message.error(
        error instanceof Error && error.message
          ? error.message
          : t("common.copyFailed"),
      );
    }
  };

  const downloadImage = async (image: GeneratedImage, index: number) => {
    try {
      const displayUrl = toDisplayUrl(image.url);
      if (window.electronAPI?.saveImageFromUrl) {
        const result = await window.electronAPI.saveImageFromUrl({
          url: displayUrl,
          ...(image.localPaths?.length ? { localPaths: image.localPaths } : {}),
        });
        if (!result.success && !result.canceled) {
          throw new Error(result.error || "Unable to download image");
        }
        return;
      }
      const response = await fetch(displayUrl);
      if (!response.ok) {
        throw new Error(`Unable to fetch image (${response.status})`);
      }
      const blobUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = blobUrl;
      anchor.download = imageFileName(displayUrl, index);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(blobUrl);
    } catch (error) {
      message.error(
        error instanceof Error && error.message
          ? error.message
          : `${t("common.download")} ${t("chat.imageGeneration.failed")}`,
      );
    }
  };

  return (
    <section
      className={[
        styles.card,
        failed ? styles.failed : "",
        running ? styles.running : "",
      ]
        .filter(Boolean)
        .join(" ")}
      data-chat-tool-call="1"
      data-image-generation-card="1"
      aria-label={t("chat.imageGeneration.title")}
    >
      <header className={styles.header}>
        <span className={styles.title}>
          <ImageIcon size={17} aria-hidden="true" />
          {t("chat.imageGeneration.title")}
        </span>
        <span className={styles.status}>
          {failed
            ? t("chat.imageGeneration.failed")
            : running
              ? t("chat.imageGeneration.generating")
              : t("chat.imageGeneration.completed")}
        </span>
      </header>

      {prompt ? <p className={styles.prompt}>{prompt}</p> : null}

      {provenance ? (
        <div className={styles.provenance}>
          <strong>
            {provenance.operation === "edit" ? "参考图编辑" : "新建创作"}
          </strong>
          {provenance.operation === "edit" ? (
            <span>
              基于 {provenance.referenceCount || argumentReferenceCount}{" "}
              张参考图
            </span>
          ) : null}
          {provenance.parentArtifactId ? (
            <span>源版本 {provenance.parentArtifactId}</span>
          ) : null}
          {provenance.generationId ? (
            <span>版本 {provenance.generationId}</span>
          ) : null}
        </div>
      ) : null}

      {running ? (
        <div className={styles.mediaStage} role="status">
          <LoaderCircle
            className={styles.spinner}
            size={34}
            aria-hidden="true"
          />
          <strong>{t("chat.imageGeneration.generatingImage")}</strong>
          <span>{t("chat.imageGeneration.waitHint")}</span>
        </div>
      ) : failed ? (
        <div className={styles.errorStage} role="alert">
          <TriangleAlert size={22} aria-hidden="true" />
          <div>
            <strong>{t("chat.imageGeneration.failedTitle")}</strong>
            <p>{failureMessage || t("chat.imageGeneration.emptyResult")}</p>
          </div>
        </div>
      ) : (
        <div
          className={[
            styles.imageGrid,
            images.length > 1 ? styles.imageGridMultiple : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          <PreviewImage.PreviewGroup>
            {images.map((image, index) => {
              const displayUrl = toDisplayUrl(image.url);
              const label = t("chat.imageGeneration.imageAlt", {
                index: index + 1,
              });
              return (
                <figure className={styles.imageFigure} key={image.url}>
                  <PreviewImage
                    src={displayUrl}
                    alt={label}
                    width="100%"
                    preview={{ destroyOnHidden: true }}
                  />
                  <figcaption className={styles.imageActions}>
                    <Tooltip title={t("common.copy")}>
                      <button
                        type="button"
                        onClick={() => void copyImage(image)}
                        aria-label={t("common.copy")}
                      >
                        {copiedUrl === image.url ? (
                          <Check size={16} />
                        ) : (
                          <Copy size={16} />
                        )}
                      </button>
                    </Tooltip>
                    <Tooltip title={t("common.download")}>
                      <button
                        type="button"
                        onClick={() => void downloadImage(image, index)}
                        aria-label={t("common.download")}
                      >
                        <Download size={16} />
                      </button>
                    </Tooltip>
                  </figcaption>
                </figure>
              );
            })}
          </PreviewImage.PreviewGroup>
        </div>
      )}

      {!running && !failed && summary ? (
        <p className={styles.summary}>{summary}</p>
      ) : null}
    </section>
  );
}
