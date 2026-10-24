const MIME_BY_EXTENSION: Record<string, string> = {
  "7z": "application/x-7z-compressed",
  aac: "audio/aac",
  avi: "video/x-msvideo",
  bmp: "image/bmp",
  csv: "text/csv",
  doc: "application/msword",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  flac: "audio/flac",
  gif: "image/gif",
  gz: "application/gzip",
  heic: "image/heic",
  heif: "image/heif",
  html: "text/html",
  jpeg: "image/jpeg",
  jpg: "image/jpeg",
  json: "application/json",
  m4a: "audio/mp4",
  m4v: "video/x-m4v",
  md: "text/markdown",
  mkv: "video/x-matroska",
  mov: "video/quicktime",
  mp3: "audio/mpeg",
  mp4: "video/mp4",
  odf: "application/vnd.oasis.opendocument.formula",
  odg: "application/vnd.oasis.opendocument.graphics",
  odp: "application/vnd.oasis.opendocument.presentation",
  ods: "application/vnd.oasis.opendocument.spreadsheet",
  odt: "application/vnd.oasis.opendocument.text",
  ogg: "audio/ogg",
  pdf: "application/pdf",
  png: "image/png",
  ppt: "application/vnd.ms-powerpoint",
  pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  rar: "application/vnd.rar",
  rtf: "application/rtf",
  svg: "image/svg+xml",
  tar: "application/x-tar",
  tif: "image/tiff",
  tiff: "image/tiff",
  txt: "text/plain",
  wav: "audio/wav",
  webm: "video/webm",
  webp: "image/webp",
  xls: "application/vnd.ms-excel",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  xml: "application/xml",
  yaml: "application/yaml",
  yml: "application/yaml",
  zip: "application/zip",
};

export function inferAttachmentMimeType(file: Pick<File, "name" | "type">) {
  const declared = String(file.type || "")
    .trim()
    .toLowerCase();
  if (declared) return declared;
  const extension = file.name.split(".").pop()?.toLowerCase() || "";
  return MIME_BY_EXTENSION[extension] || "application/octet-stream";
}

const EXTENSION_BY_MIME: Record<string, string> = Object.fromEntries(
  Object.entries(MIME_BY_EXTENSION).map(([extension, mime]) => [
    mime,
    extension,
  ]),
);

export function prepareAttachmentFile(file: File, index = 0): File {
  const type = inferAttachmentMimeType(file);
  const originalName = file.name.trim();
  const extension = EXTENSION_BY_MIME[type] || "bin";
  const kind = type.startsWith("image/") ? "image" : "file";
  const name =
    originalName || `clipboard-${kind}-${String(index + 1)}.${extension}`;
  if (file.type === type && file.name === name) return file;
  return new File([file], name, {
    type,
    lastModified: file.lastModified,
  });
}

export function attachmentFingerprint(
  file: Pick<File, "name" | "size" | "type" | "lastModified">,
) {
  return [
    file.name,
    file.size,
    inferAttachmentMimeType(file),
    file.lastModified || 0,
  ].join("::");
}

function clipboardTransportFingerprint(
  file: Pick<File, "name" | "size" | "type">,
) {
  return [file.name, file.size, inferAttachmentMimeType(file)].join("::");
}

type ClipboardFileSource = {
  files?: ArrayLike<File> | null;
  items?: ArrayLike<{
    kind: string;
    getAsFile: () => File | null;
  }> | null;
  types?: ArrayLike<string> | null;
  getData?: (type: string) => string;
};

export type DesktopClipboardAttachment = {
  name: string;
  type?: string;
  size?: number;
  lastModified?: number;
  dataBase64: string;
};

export function filesFromClipboardData(
  clipboardData: ClipboardFileSource | null | undefined,
): File[] {
  if (!clipboardData) return [];
  const files: File[] = [];
  const fingerprints = new Set<string>();
  const itemFingerprints = new Set<string>();
  const append = (file: File | null, source: "item" | "file-list") => {
    if (!file) return;
    const transportFingerprint = clipboardTransportFingerprint(file);
    // Chromium may materialize the same clipboard payload twice: once from
    // DataTransferItem.getAsFile() and again from DataTransfer.files. Those
    // File objects can have different lastModified values, so compare the
    // stable transport metadata when crossing the two sources.
    if (
      source === "file-list" &&
      itemFingerprints.has(transportFingerprint)
    ) {
      return;
    }
    const fingerprint = attachmentFingerprint(file);
    if (fingerprints.has(fingerprint)) return;
    fingerprints.add(fingerprint);
    if (source === "item") itemFingerprints.add(transportFingerprint);
    files.push(file);
  };

  Array.from(clipboardData.items || []).forEach((item) => {
    if (item.kind === "file") append(item.getAsFile(), "item");
  });
  Array.from(clipboardData.files || []).forEach((file) =>
    append(file, "file-list"),
  );
  return files;
}

const ATTACHMENT_HINT_TYPES = new Set([
  "application/msword",
  "application/pdf",
  "application/rtf",
  "application/vnd.ms-excel",
  "application/vnd.ms-powerpoint",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/x-7z-compressed",
  "application/x-rar-compressed",
  "application/zip",
  "text/markdown",
]);

export function clipboardDataHasAttachmentHint(
  clipboardData: ClipboardFileSource | null | undefined,
): boolean {
  if (!clipboardData) return false;
  if (filesFromClipboardData(clipboardData).length > 0) return true;

  const types = Array.from(clipboardData.types || []).map((type) =>
    String(type || "").toLowerCase(),
  );
  if (
    types.some(
      (type) =>
        type.startsWith("image/") ||
        type.startsWith("video/") ||
        type.startsWith("audio/") ||
        ATTACHMENT_HINT_TYPES.has(type) ||
        type.includes("file-url") ||
        type.includes("filenames"),
    )
  ) {
    return true;
  }

  const plain = String(clipboardData.getData?.("text/plain") || "").trim();
  if (!plain) return true;
  const candidates = plain
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter(Boolean);
  if (candidates.length === 0 || candidates.length > 20) return false;
  return candidates.every((value) => {
    const withoutQuery = value.split(/[?#]/, 1)[0];
    const extension = withoutQuery.split(".").pop()?.toLowerCase() || "";
    return Boolean(MIME_BY_EXTENSION[extension]);
  });
}

export function filesFromDesktopClipboardAttachments(
  attachments: DesktopClipboardAttachment[] | null | undefined,
): File[] {
  if (!Array.isArray(attachments)) return [];

  return attachments.flatMap((attachment, index) => {
    const encoded = String(attachment?.dataBase64 || "").replace(
      /^data:[^;]+;base64,/i,
      "",
    );
    if (!encoded) return [];
    try {
      const binary = globalThis.atob(encoded);
      const bytes = new Uint8Array(binary.length);
      for (let offset = 0; offset < binary.length; offset += 1) {
        bytes[offset] = binary.charCodeAt(offset);
      }
      const name =
        String(attachment.name || "").trim() ||
        `clipboard-file-${String(index + 1)}.bin`;
      const type =
        String(attachment.type || "").trim() ||
        inferAttachmentMimeType({ name, type: "" });
      return [
        new File([bytes], name, {
          type,
          lastModified: Number(attachment.lastModified || 0),
        }),
      ];
    } catch {
      return [];
    }
  });
}

export function formatAttachmentSize(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${Math.round(size / 1024)} KB`;
  if (size < 1024 ** 3) {
    return `${(size / 1024 ** 2).toFixed(size < 10 * 1024 ** 2 ? 1 : 0)} MB`;
  }
  return `${(size / 1024 ** 3).toFixed(1)} GB`;
}
