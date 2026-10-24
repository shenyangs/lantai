import { describe, expect, it } from "vitest";

import {
  attachmentFingerprint,
  clipboardDataHasAttachmentHint,
  filesFromClipboardData,
  filesFromDesktopClipboardAttachments,
  formatAttachmentSize,
  inferAttachmentMimeType,
  prepareAttachmentFile,
} from "./lantaiChatAttachments";

describe("lantai chat clipboard attachments", () => {
  it("extracts images and common document/archive formats from one paste", () => {
    const files = [
      new File(["image"], "shot.png", { type: "image/png" }),
      new File(["doc"], "brief.docx"),
      new File(["plain"], "notes.txt"),
      new File(["markdown"], "README.md"),
      new File(["pdf"], "paper.pdf"),
      new File(["zip"], "sources.zip"),
    ];

    expect(
      filesFromClipboardData({
        items: files.map((file) => ({
          kind: "file",
          getAsFile: () => file,
        })),
      }).map((file) => file.name),
    ).toEqual(files.map((file) => file.name));
  });

  it("deduplicates the same file exposed through clipboard items and files", () => {
    const itemFile = new File(["same"], "report.pdf", {
      type: "application/pdf",
      lastModified: 123,
    });
    // Chromium may materialize DataTransferItem.getAsFile() and
    // clipboardData.files as separate File objects with different timestamps.
    const fileListFile = new File(["same"], "report.pdf", {
      type: "application/pdf",
      lastModified: 456,
    });

    expect(
      filesFromClipboardData({
        items: [{ kind: "file", getAsFile: () => itemFile }],
        files: [fileListFile],
      }),
    ).toEqual([itemFile]);
  });

  it("infers useful MIME types when clipboard files omit them", () => {
    expect(inferAttachmentMimeType(new File([""], "photo.HEIC"))).toBe(
      "image/heic",
    );
    expect(inferAttachmentMimeType(new File([""], "budget.xlsx"))).toBe(
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    );
    expect(inferAttachmentMimeType(new File([""], "bundle.zip"))).toBe(
      "application/zip",
    );
    expect(inferAttachmentMimeType(new File([""], "unknown.data"))).toBe(
      "application/octet-stream",
    );
  });

  it("gives unnamed clipboard images a safe upload filename", () => {
    const prepared = prepareAttachmentFile(
      new File(["image"], "", { type: "image/png" }),
      1,
    );
    expect(prepared.name).toBe("clipboard-image-2.png");
    expect(prepared.type).toBe("image/png");
  });

  it("leaves text-only clipboard data alone", () => {
    expect(
      filesFromClipboardData({
        items: [{ kind: "string", getAsFile: () => null }],
        files: [],
      }),
    ).toEqual([]);
  });

  it("detects native attachment hints without mistaking ordinary text for a file", () => {
    expect(
      clipboardDataHasAttachmentHint({
        files: [],
        items: [],
        types: ["text/plain", "image/png"],
        getData: () => "image.png",
      }),
    ).toBe(true);
    expect(
      clipboardDataHasAttachmentHint({
        files: [],
        items: [],
        types: ["text/plain"],
        getData: () => "普通文字",
      }),
    ).toBe(false);
    expect(
      clipboardDataHasAttachmentHint({
        files: [],
        items: [],
        types: ["text/plain"],
        getData: () => "report.pdf\nsources.zip",
      }),
    ).toBe(true);
  });

  it("rebuilds native Office, PDF, and archive payloads as real files", async () => {
    const attachments = [
      { name: "brief.docx", dataBase64: btoa("doc"), lastModified: 10 },
      { name: "paper.pdf", dataBase64: btoa("pdf"), lastModified: 11 },
      { name: "sources.zip", dataBase64: btoa("zip"), lastModified: 12 },
    ];
    const files = filesFromDesktopClipboardAttachments(attachments);

    expect(files.map((file) => [file.name, file.type])).toEqual([
      [
        "brief.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      ],
      ["paper.pdf", "application/pdf"],
      ["sources.zip", "application/zip"],
    ]);
    expect(await Promise.all(files.map((file) => file.text()))).toEqual([
      "doc",
      "pdf",
      "zip",
    ]);
    expect(files.map((file) => file.lastModified)).toEqual([10, 11, 12]);
  });

  it("uses stable fingerprints and compact sizes", () => {
    const file = new File(["abc"], "notes.md", { lastModified: 42 });
    expect(attachmentFingerprint(file)).toContain("text/markdown");
    expect(formatAttachmentSize(3)).toBe("3 B");
    expect(formatAttachmentSize(2048)).toBe("2 KB");
  });
});
