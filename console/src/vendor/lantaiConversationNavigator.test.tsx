import { fireEvent, render, screen } from "@testing-library/react";
import type { RefObject } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  ConversationNavigator,
  buildConversationTurns,
  resolveConversationMarkerProximity,
  resolveActiveConversationTurn,
  scrollConversationToMessage,
  shouldShowConversationNavigator,
} from "./lantaiConversationNavigator";

const requestCard = (text: string) => ({
  code: "LantaiRuntimeRequestCard",
  data: {
    input: [
      {
        role: "user",
        type: "message",
        content: [{ type: "text", text }],
      },
    ],
  },
});

describe("conversation turn model", () => {
  it("uses each user message as one stable conversation turn", () => {
    const turns = buildConversationTurns([
      {
        id: "user-1",
        role: "user",
        createdAt: "2026-07-28T08:00:00Z",
        cards: [requestCard("  第一轮问题  ")],
      },
      { id: "assistant-1", role: "assistant", cards: [] },
      { id: "assistant-1-more", role: "assistant", cards: [] },
      {
        id: "user-2",
        role: "user",
        cards: [requestCard("第二轮\n问题")],
      },
      { id: "assistant-2", role: "assistant", cards: [] },
    ]);

    expect(turns).toEqual([
      expect.objectContaining({
        id: "user-1",
        anchorMessageId: "user-1",
        preview: "第一轮问题",
        messageIds: ["user-1", "assistant-1", "assistant-1-more"],
      }),
      expect.objectContaining({
        id: "user-2",
        anchorMessageId: "user-2",
        preview: "第二轮 问题",
        messageIds: ["user-2", "assistant-2"],
      }),
    ]);
  });

  it("uses attachment names when a turn has no text", () => {
    const turns = buildConversationTurns([
      {
        id: "user-file",
        role: "user",
        cards: [
          {
            code: "LantaiRuntimeRequestCard",
            data: {
              input: [
                {
                  content: [
                    {
                      type: "file",
                      file_name: "研究报告.pdf",
                    },
                  ],
                },
              ],
            },
          },
        ],
      },
      { id: "assistant-file", role: "assistant", cards: [] },
    ]);

    expect(turns[0]?.preview).toBe("研究报告.pdf");
  });

  it("selects the last turn above the viewport reading line", () => {
    const turns = buildConversationTurns(
      ["one", "two", "three"].flatMap((id) => [
        { id: `user-${id}`, role: "user", cards: [requestCard(id)] },
        { id: `assistant-${id}`, role: "assistant", cards: [] },
      ]),
    );

    expect(
      resolveActiveConversationTurn(
        turns,
        [
          { messageId: "user-one", top: 80 },
          { messageId: "user-two", top: 540 },
          { messageId: "user-three", top: 960 },
        ],
        700,
      ),
    ).toBe("user-two");
  });

  it("only appears for long conversations that actually overflow", () => {
    expect(shouldShowConversationNavigator(5, 2_000, 700)).toBe(false);
    expect(shouldShowConversationNavigator(6, 900, 700)).toBe(false);
    expect(shouldShowConversationNavigator(6, 1_500, 700)).toBe(true);
  });

  it("keeps idle ticks uniform and forms one proximity mound on interaction", () => {
    expect(resolveConversationMarkerProximity(4, null)).toBe("far");
    expect(resolveConversationMarkerProximity(0, null)).toBe("far");
    expect(resolveConversationMarkerProximity(1, 1)).toBe("0");
    expect(resolveConversationMarkerProximity(2, 1)).toBe("1");
    expect(resolveConversationMarkerProximity(4, 1)).toBe("3");
    expect(resolveConversationMarkerProximity(5, 1)).toBe("far");
  });
});

describe("conversation turn navigation", () => {
  it("scrolls the target into the readable region without moving the page", () => {
    const list = document.createElement("div");
    const message = document.createElement("article");
    message.dataset.lantaiMessageId = "user-6";
    list.append(message);
    const scrollTo = vi.fn();

    Object.defineProperties(list, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 2_000 },
      scrollTop: { configurable: true, writable: true, value: 900 },
      scrollTo: { configurable: true, value: scrollTo },
    });
    vi.spyOn(list, "getBoundingClientRect").mockReturnValue({
      top: 100,
      bottom: 700,
      left: 0,
      right: 900,
      width: 900,
      height: 600,
      x: 0,
      y: 100,
      toJSON: () => ({}),
    });
    vi.spyOn(message, "getBoundingClientRect").mockReturnValue({
      top: 520,
      bottom: 580,
      left: 100,
      right: 700,
      width: 600,
      height: 60,
      x: 100,
      y: 520,
      toJSON: () => ({}),
    });

    expect(
      scrollConversationToMessage(list, "user-6", {
        behavior: "smooth",
      }),
    ).toEqual({
      found: true,
      nearEnd: false,
      top: 1_200,
    });
    expect(scrollTo).toHaveBeenCalledWith({
      top: 1_200,
      behavior: "smooth",
    });
  });

  it("renders an accessible rail and delegates marker activation", () => {
    const list = document.createElement("div");
    Object.defineProperties(list, {
      clientHeight: { configurable: true, value: 600 },
      scrollHeight: { configurable: true, value: 2_400 },
      scrollTop: { configurable: true, writable: true, value: 0 },
    });
    const messages = Array.from({ length: 6 }, (_, index) => {
      const message = document.createElement("article");
      message.dataset.lantaiMessageId = `user-${index + 1}`;
      Object.defineProperty(message, "offsetTop", {
        configurable: true,
        value: index * 400,
      });
      list.append(message);
      return {
        id: `user-${index + 1}`,
        role: "user",
        cards: [requestCard(`第 ${index + 1} 轮问题`)],
      };
    });
    const scrollContainerRef = {
      current: list,
    } as RefObject<HTMLDivElement>;
    const onNavigate = vi.fn();

    render(
      <ConversationNavigator
        messages={messages}
        scrollContainerRef={scrollContainerRef}
        onNavigate={onNavigate}
      />,
    );

    const navigation = screen.getByRole("navigation", {
      name: "会话定位",
    });
    expect(navigation).toHaveAttribute("data-visible", "true");
    expect(
      navigation.querySelector(".lantai-conversation-navigator-track"),
    ).not.toBeInTheDocument();
    const markers = Array.from(
      navigation.querySelectorAll<HTMLElement>(
        ".lantai-conversation-navigator-marker",
      ),
    );
    expect(markers[0]).toHaveStyle({
      "--lt-conversation-marker-position": "0%",
    });
    expect(markers[3]).toHaveStyle({
      "--lt-conversation-marker-position": "60%",
    });
    expect(markers[5]).toHaveStyle({
      "--lt-conversation-marker-position": "100%",
    });
    expect(
      navigation.querySelector(".lantai-conversation-navigator-markers"),
    ).toHaveStyle({
      "--lt-conversation-rail-height": "76px",
    });
    const marker = screen.getByRole("button", {
      name: "第 4 轮：第 4 轮问题",
    });
    expect(
      markers.every((candidate) => candidate.dataset.proximity === "far"),
    ).toBe(true);

    fireEvent.mouseEnter(marker);
    expect(marker).toHaveAttribute("data-proximity", "0");
    expect(markers[2]).toHaveAttribute("data-proximity", "1");
    const activeMarker = markers.find(
      (candidate) => candidate.dataset.active === "true",
    );
    expect(activeMarker).toBeDefined();
    expect(activeMarker).not.toHaveAttribute("data-proximity", "0");
    fireEvent.mouseLeave(marker);
    expect(
      markers.every((candidate) => candidate.dataset.proximity === "far"),
    ).toBe(true);

    fireEvent.click(marker);
    expect(onNavigate).toHaveBeenCalledWith("user-4");

    marker.focus();
    fireEvent.keyDown(marker, { key: "ArrowDown" });
    expect(
      screen.getByRole("button", {
        name: "第 5 轮：第 5 轮问题",
      }),
    ).toHaveFocus();
  });
});
