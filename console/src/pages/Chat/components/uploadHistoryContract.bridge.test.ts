import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  chatApi,
  chatUploadResponseToAttachmentSuccess,
} from "../../../api/modules/chat";
import type { Message } from "../../../api/types/chat";
import {
  attachmentToContent,
  type AttachmentItem,
} from "../../../vendor/lantaiChat";
import { attachmentRolesFromMessages } from "./artifactExperience";

const phase = process.env.LANTAI_UPLOAD_HISTORY_CONTRACT_PHASE;
const inputPath = process.env.LANTAI_UPLOAD_HISTORY_CONTRACT_INPUT;
const outputPath = process.env.LANTAI_UPLOAD_HISTORY_CONTRACT_OUTPUT;
const contractDescribe =
  phase && inputPath && outputPath ? describe : describe.skip;

function readInput(): Record<string, unknown> {
  return JSON.parse(readFileSync(inputPath!, "utf8")) as Record<
    string,
    unknown
  >;
}

function writeOutput(value: unknown): void {
  writeFileSync(outputPath!, JSON.stringify(value), "utf8");
}

contractDescribe("upload producer to history consumer joint contract", () => {
  it("runs the selected actual TypeScript boundary", async () => {
    const input = readInput();
    if (phase === "produce") {
      const upload = input.upload as {
        url: string;
        file_name: string;
        stored_name: string;
        size: number;
        digest: string;
        version: number;
        upload_receipt: string;
      };
      const mimeType = String(input.mime_type || "application/octet-stream");
      const role = String(input.role || "reference") as AttachmentItem["role"];
      const priorElectronApi = window.electronAPI;
      window.electronAPI = {
        ...priorElectronApi,
        uploadChatAttachment: async () => ({ ok: true, ...upload }),
      };
      const response = await chatApi.uploadFile(
        new File(["joint-contract"], upload.file_name, { type: mimeType }),
      );
      window.electronAPI = priorElectronApi;
      const success = chatUploadResponseToAttachmentSuccess(response);
      const attachment: AttachmentItem = {
        id: "joint-contract-upload",
        name: upload.file_name,
        type: mimeType,
        size: upload.size,
        url: success.url,
        status: "ready",
        canonicalPath: success.canonicalPath,
        storedName: success.storedName,
        digest: success.digest,
        immutableVersion: success.immutableVersion,
        uploadReceipt: success.uploadReceipt,
        role,
        roleConfirmed: true,
      };
      const content = attachmentToContent(attachment);
      expect(content).toMatchObject({
        type: "file",
        filename: upload.file_name,
        file_name: upload.file_name,
        file_source_url: upload.url,
        stored_name: upload.stored_name,
        attachment_role: role,
        attachment_digest: upload.digest,
        attachment_version: upload.version,
        attachment_upload_receipt: upload.upload_receipt,
      });
      writeOutput({ content });
      return;
    }

    if (phase === "consume") {
      const history = input.history as {
        messages: Message[];
        meta?: { external_attachment_paths?: unknown[] };
      };
      const expected = input.expected as {
        stored_name: string;
        display_name: string;
        role: string;
        digest: string;
        version: number;
        source_chat_id: string;
        source_command_id: string;
      };
      const userAttachment = history.messages
        .filter((message) => message.role === "user")
        .flatMap((message) =>
          Array.isArray(message.content) ? message.content : [],
        )
        .find(
          (part) =>
            part &&
            typeof part === "object" &&
            (part as Record<string, unknown>).stored_name ===
              expected.stored_name,
        ) as Record<string, unknown> | undefined;
      expect(userAttachment).toMatchObject({
        attachment_schema_version: 1,
        attachment_digest: expected.digest,
        attachment_version: expected.version,
        attachment_source_chat_id: expected.source_chat_id,
        attachment_source_command_id: expected.source_command_id,
        attachment_grant_state: "bound",
      });
      expect(String(userAttachment?.attachment_source_id || "")).toMatch(
        /^attachment-source:sha256:[0-9a-f]{64}$/,
      );
      expect(String(userAttachment?.attachment_grant_ref || "")).toMatch(
        /^file-grant:sha256:[0-9a-f]{64}$/,
      );
      const roles = attachmentRolesFromMessages(
        history.messages,
        history.meta?.external_attachment_paths,
      );
      const projection = [...roles.entries()].map(([storedName, value]) => ({
        stored_name: storedName,
        display_name: value.displayName,
        role: value.role,
      }));
      expect(projection).toContainEqual({
        stored_name: expected.stored_name,
        display_name: expected.display_name,
        role: expected.role,
      });
      writeOutput({ projection });
      return;
    }

    throw new Error(`Unsupported contract phase: ${phase || "missing"}`);
  });
});
