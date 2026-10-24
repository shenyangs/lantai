import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import type { KdocsDocumentItem } from "../../../../api/types/kdocs";
import type { KdocsReference } from "../../kdocsReferences";
import KdocsComposerContext from "./KdocsComposerContext";

const mocks = vi.hoisted(() => ({
  listDocuments: vi.fn(),
}));

vi.mock("../../../../api/modules/kdocs", () => ({
  kdocsApi: { listDocuments: mocks.listDocuments },
}));

const documentItem: KdocsDocumentItem = {
  key: "drive-a:file-a",
  drive_id: "drive-a",
  file_id: "file-a",
  parent_id: "0",
  name: "项目简报.docx",
  type: "docx",
  suffix: "docx",
  is_folder: false,
  size: 24,
  version: "1",
  content_hash: "hash-a",
  link_id: "",
  original_url: "",
  location: "我的云文档",
  owner: null,
  modifier: null,
  shared: false,
  permissions: { can_read: true },
};

describe("KDocs composer mention", () => {
  it("describes a usable partial snapshot without implying a read failure", () => {
    const composingRef = createRef<boolean>();
    composingRef.current = false;
    const reference: KdocsReference = {
      schema_version: 1,
      key: "drive-a:file-a:file",
      kind: "file",
      drive_id: "drive-a",
      file_id: "file-a",
      name_snapshot: documentItem.name,
      read_status: "partial",
      content_snapshot: {
        text: "已读取的正文",
        format: "markdown",
        truncated: false,
      },
    };

    render(
      <KdocsComposerContext
        references={[reference]}
        getTextarea={() => null}
        setTextareaText={vi.fn()}
        isComposingRef={composingRef}
        onAddDocument={vi.fn()}
        onOpenReference={vi.fn()}
        onRemoveReference={vi.fn()}
        onRetryReference={vi.fn()}
      />,
    );

    expect(screen.getByText("正文已读取 · 格式未完全展开")).toBeInTheDocument();
    expect(screen.queryByText("部分读取")).not.toBeInTheDocument();
  });

  it("exposes the mention results as the textarea combobox", async () => {
    mocks.listDocuments.mockResolvedValue({
      view: "recent",
      items: [documentItem],
      next_page_token: "",
      total: 1,
      parent: { drive_id: "", file_id: "" },
    });
    const composingRef = createRef<boolean>();
    composingRef.current = false;

    render(
      <div>
        <textarea aria-label="消息" />
        <KdocsComposerContext
          references={[]}
          getTextarea={() => screen.getByRole("textbox", { name: "消息" })}
          setTextareaText={(textarea, value, cursor) => {
            textarea.value = value;
            textarea.setSelectionRange(
              cursor ?? value.length,
              cursor ?? value.length,
            );
          }}
          isComposingRef={composingRef}
          onAddDocument={vi.fn()}
          onOpenReference={vi.fn()}
          onRemoveReference={vi.fn()}
          onRetryReference={vi.fn()}
        />
      </div>,
    );

    const textarea = screen.getByRole("textbox", { name: "消息" });
    fireEvent.input(textarea, {
      target: { value: "@", selectionStart: 1, selectionEnd: 1 },
    });

    const listbox = await screen.findByRole("listbox", {
      name: "引用金山文档",
    });
    const option = await screen.findByRole("option", {
      name: /项目简报\.docx/,
    });

    await waitFor(() => expect(textarea).toHaveAttribute("role", "combobox"));
    expect(textarea).toHaveAttribute("aria-expanded", "true");
    expect(textarea).toHaveAttribute("aria-controls", listbox.id);
    expect(textarea).toHaveAttribute("aria-activedescendant", option.id);
  });

  it("opens the clipped composer shell while the mention list is visible", async () => {
    mocks.listDocuments.mockResolvedValue({
      view: "recent",
      items: [documentItem],
      next_page_token: "",
      total: 1,
      parent: { drive_id: "", file_id: "" },
    });
    const composingRef = createRef<boolean>();
    composingRef.current = false;

    render(
      <div
        className="lantai-chat-anywhere-input"
        data-testid="chat-input-shell"
      >
        <div className="lantai-sender" data-testid="sender-shell">
          <textarea aria-label="消息" />
          <KdocsComposerContext
            references={[]}
            getTextarea={() => screen.getByRole("textbox", { name: "消息" })}
            setTextareaText={(textarea, value, cursor) => {
              textarea.value = value;
              textarea.setSelectionRange(
                cursor ?? value.length,
                cursor ?? value.length,
              );
            }}
            isComposingRef={composingRef}
            onAddDocument={vi.fn()}
            onOpenReference={vi.fn()}
            onRemoveReference={vi.fn()}
            onRetryReference={vi.fn()}
          />
        </div>
      </div>,
    );

    const textarea = screen.getByRole("textbox", { name: "消息" });
    fireEvent.input(textarea, {
      target: { value: "@", selectionStart: 1, selectionEnd: 1 },
    });

    await screen.findByRole("listbox", { name: "引用金山文档" });
    expect(screen.getByTestId("sender-shell")).toHaveAttribute(
      "data-kdocs-mention-open",
      "true",
    );
    expect(screen.getByTestId("chat-input-shell")).toHaveAttribute(
      "data-kdocs-mention-open",
      "true",
    );

    fireEvent.keyDown(textarea, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
      expect(screen.getByTestId("sender-shell")).not.toHaveAttribute(
        "data-kdocs-mention-open",
      );
      expect(screen.getByTestId("chat-input-shell")).not.toHaveAttribute(
        "data-kdocs-mention-open",
      );
    });
  });
});
