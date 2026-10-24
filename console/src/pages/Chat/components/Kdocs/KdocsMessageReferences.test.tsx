import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import KdocsMessageReferences, {
  messageKdocsReferences,
} from "./KdocsMessageReferences";
import type { KdocsReference } from "../../kdocsReferences";
it("opens the immutable historical excerpt snapshot", () => {
  const reference: KdocsReference = {
    schema_version: 1,
    key: "d:f:excerpt",
    kind: "excerpt",
    drive_id: "d",
    file_id: "f",
    name_snapshot: "历史文件",
    read_status: "read",
    snapshot_id: "old",
    excerpt: { text: "旧原文", location: "预算", start: 3, end: 6 },
  };
  const onOpen = vi.fn();
  render(
    <KdocsMessageReferences
      message={{
        cards: [
          {
            data: { input: [{ metadata: { kdocs_references: [reference] } }] },
          },
        ],
      }}
      onOpen={onOpen}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "历史文件 · 预算" }));
  expect(onOpen).toHaveBeenCalledWith(reference);
  expect(messageKdocsReferences({ cards: [{ data: { input: {} } }] })).toEqual(
    [],
  );
});
