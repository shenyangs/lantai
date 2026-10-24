import { useMemo } from "react";
import {
  Download,
  LoaderCircle,
  TriangleAlert,
  Video as VideoIcon,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { useAppMessage } from "@/hooks/useAppMessage";
import { Tooltip } from "@/vendor/lantaiDesign";
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

export interface VideoGenerationToolData {
  status?: string;
  content?: RuntimeToolContent[];
}

interface VideoGenerationToolCardProps {
  data?: VideoGenerationToolData;
}

interface GeneratedVideo {
  url: string;
  mimeType?: string;
  durable?: boolean;
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

function collectVideos(
  value: unknown,
  videos: GeneratedVideo[],
  visited = new Set<unknown>(),
) {
  const parsed = tryParseJson(value);
  if (!parsed || typeof parsed !== "object" || visited.has(parsed)) return;
  visited.add(parsed);

  if (Array.isArray(parsed)) {
    parsed.forEach((item) => collectVideos(item, videos, visited));
    return;
  }

  const record = parsed as Record<string, unknown>;
  if (record.type === "video") {
    const source =
      record.source && typeof record.source === "object"
        ? (record.source as Record<string, unknown>)
        : {};
    const url = [record.video_url, record.url, source.url].find(
      (item): item is string => typeof item === "string" && !!item.trim(),
    );
    if (url && !videos.some((item) => item.url === url)) {
      const metadata =
        record.metadata && typeof record.metadata === "object"
          ? (record.metadata as Record<string, unknown>)
          : {};
      videos.push({
        url,
        mimeType:
          typeof record.mime_type === "string" ? record.mime_type : undefined,
        durable:
          typeof metadata.durable === "boolean" ? metadata.durable : undefined,
      });
    }
  }

  for (const key of ["content", "output", "result", "data", "assets"]) {
    if (key in record) collectVideos(record[key], videos, visited);
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

function readError(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (!value) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function localVideoPath(url: string): string {
  try {
    if (url.startsWith("file://")) {
      return decodeURIComponent(new URL(url).pathname);
    }
  } catch {
    return "";
  }
  return url.startsWith("/") && !url.startsWith("/files/preview/")
    ? decodeURIComponent(url)
    : "";
}

function videoFileName(url: string, index: number): string {
  try {
    const name = decodeURIComponent(new URL(url, window.location.href).pathname)
      .split("/")
      .filter(Boolean)
      .pop();
    if (name && /\.[a-z0-9]{2,5}$/i.test(name)) return name;
  } catch {
    // Fall back to a stable generated-video name.
  }
  return "lantai-generated-video-" + (index + 1) + ".mp4";
}

export default function VideoGenerationToolCard({
  data,
}: VideoGenerationToolCardProps) {
  const { t } = useTranslation();
  const { message } = useAppMessage();
  const call = data?.content?.[0]?.data;
  const result = data?.content?.[1]?.data;
  const hasTerminalResult = (data?.content?.length ?? 0) > 1;
  const output = result?.output;
  const error = readError(result?.error);
  const statusValue = String(data?.status || "").toLowerCase();
  const prompt = readPrompt(call?.arguments);
  const videos = useMemo(() => {
    const found: GeneratedVideo[] = [];
    collectVideos(output, found);
    return found;
  }, [output]);
  const summary = useMemo(() => {
    const texts: string[] = [];
    collectText(output, texts);
    return texts.filter(Boolean).join("\n");
  }, [output]);
  const failureMessage = error || (videos.length === 0 ? summary : "");
  const hasTemporaryVideo = videos.some((video) => video.durable === false);
  const failed =
    Boolean(error) ||
    statusValue.includes("fail") ||
    statusValue.includes("error") ||
    (hasTerminalResult &&
      statusValue.includes("complete") &&
      videos.length === 0);
  const running =
    !failed &&
    videos.length === 0 &&
    (!hasTerminalResult ||
      statusValue.includes("run") ||
      statusValue.includes("progress") ||
      !statusValue.includes("complete"));

  const downloadVideo = async (url: string, index: number) => {
    try {
      const sourcePath = localVideoPath(url);
      const saveFileAs = window.electronAPI?.saveFileAs;
      if (window.electronAPI?.isDesktop && sourcePath && saveFileAs) {
        const result = await saveFileAs({
          sourcePath,
          filename: videoFileName(url, index),
        });
        if (result.success) {
          message.success(t("chat.videoGeneration.saved"));
          return;
        }
        if (result.canceled) return;
        throw new Error(result.error || "Unable to save video");
      }

      const response = await fetch(toDisplayUrl(url));
      if (!response.ok) {
        throw new Error("Unable to fetch video (" + response.status + ")");
      }
      const blobUrl = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = blobUrl;
      anchor.download = videoFileName(url, index);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(blobUrl);
    } catch (reason) {
      message.error(
        reason instanceof Error && reason.message
          ? reason.message
          : t("chat.videoGeneration.failed"),
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
      data-video-generation-card="1"
      aria-label={t("chat.videoGeneration.title")}
    >
      <header className={styles.header}>
        <span className={styles.title}>
          <VideoIcon size={17} aria-hidden="true" />
          {t("chat.videoGeneration.title")}
        </span>
        <span className={styles.status}>
          {failed
            ? t("chat.videoGeneration.failed")
            : running
              ? t("chat.videoGeneration.generating")
              : t("chat.videoGeneration.completed")}
        </span>
      </header>

      {prompt ? <p className={styles.prompt}>{prompt}</p> : null}

      {running ? (
        <div className={styles.mediaStage} role="status">
          <LoaderCircle
            className={styles.spinner}
            size={34}
            aria-hidden="true"
          />
          <strong>{t("chat.videoGeneration.generatingVideo")}</strong>
          <span>{t("chat.videoGeneration.waitHint")}</span>
        </div>
      ) : failed ? (
        <div className={styles.errorStage} role="alert">
          <TriangleAlert size={22} aria-hidden="true" />
          <div>
            <strong>{t("chat.videoGeneration.failedTitle")}</strong>
            <p>{failureMessage || t("chat.videoGeneration.emptyResult")}</p>
          </div>
        </div>
      ) : (
        <div className={styles.imageGrid}>
          {videos.map((video, index) => (
            <figure className={styles.videoFigure} key={video.url}>
              <video
                className={styles.videoPlayer}
                src={toDisplayUrl(video.url)}
                controls
                playsInline
                preload="metadata"
                aria-label={t("chat.videoGeneration.videoAlt", {
                  index: index + 1,
                })}
              />
              <figcaption className={styles.imageActions}>
                <Tooltip title={t("common.download")}>
                  <button
                    type="button"
                    onClick={() => void downloadVideo(video.url, index)}
                    aria-label={t("common.download")}
                  >
                    <Download size={16} />
                  </button>
                </Tooltip>
              </figcaption>
            </figure>
          ))}
        </div>
      )}

      {!running && !failed && summary ? (
        <p className={styles.summary}>{summary}</p>
      ) : null}
      {!running && !failed && hasTemporaryVideo ? (
        <p className={styles.temporaryNotice} role="note">
          <TriangleAlert size={15} aria-hidden="true" />
          {t("chat.videoGeneration.temporaryLink")}
        </p>
      ) : null}
    </section>
  );
}
