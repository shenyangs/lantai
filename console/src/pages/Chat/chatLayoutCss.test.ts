import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readText = (relativePath: string) =>
  readFileSync(new URL(relativePath, import.meta.url), "utf8");

describe("chat layout CSS contracts", () => {
  it("keeps both welcome layers transparent while empty and during the first-turn exit", () => {
    const css = readText("./index.module.less");
    const start = css.indexOf('.chatStage[data-empty-conversation="true"],');
    const end = css.indexOf(
      '.chatStage[data-first-turn-transition="true"] {',
      start,
    );
    const welcomeStyles = css.slice(start, end);

    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeGreaterThan(start);
    expect(welcomeStyles).toContain(
      '.chatStage[data-first-turn-transition="true"],',
    );
    expect(welcomeStyles).toContain('data-lantai-welcome-exiting="1"');
    for (const className of [
      "lantai-chat-anywhere-message-list-welcome",
      "lantai-chat-anywhere-welcome-default",
    ]) {
      const declarations = welcomeStyles.match(
        new RegExp(`\\.${className} \\{([\\s\\S]*?)\\}`),
      )?.[1];
      expect(declarations).toContain("background: transparent !important;");
    }
  });

  it("lets running draft actions wrap below the writing surface", () => {
    const css = readText("./index.module.less");
    const footer = css.match(
      /\.lantai-sender-content-bottom:has\(\.lantai-sender-control-intent-btn\) \{([\s\S]*?)\}/,
    )?.[1];
    expect(footer).toContain("position: static !important;");
    expect(footer).toContain("order: 1;");
    expect(footer).toContain("flex-wrap: wrap;");
    expect(css).toContain(
      ".lantai-sender-content:has(.lantai-sender-control-intent-btn)",
    );
  });

  it("keeps task activity outside the writing surface without a second card", () => {
    const source = readText("../../vendor/lantaiChat.ts");
    const presence = source.indexOf(
      '{ className: "lantai-sender-outside-context" }',
    );
    const queue = source.indexOf('className: "lantai-guidance-queue"');
    const sender = source.indexOf('className: "lantai-sender",', queue);
    expect(presence).toBeLessThan(queue);
    expect(queue).toBeLessThan(sender);
    const css = readText("./index.module.less");
    const activity = css.match(
      /\.lantai-sender-outside-context \[data-variant="presence"\] > button \{([\s\S]*?)\}/,
    )?.[1];
    expect(activity).toContain("background: transparent;");
    expect(activity).toContain("box-shadow: none;");
  });

  it("keeps populated composer context on the same translucent mask", () => {
    const css = readText("./index.module.less");
    const start = css.indexOf(
      ".lantai-chat-anywhere-input:has(.lantai-sender-outside-context:not(:empty)),",
    );
    const block = css.slice(start, css.indexOf("\n    }", start));
    expect(block).not.toContain("background: var(--lt-workspace-bg)");
    expect(block).not.toContain("backdrop-filter: none");
    expect(css).toContain("@media (prefers-reduced-transparency: reduce)");
  });

  it("keeps each collapsed reasoning step on its own readable row", () => {
    const chatModuleCss = readText("./index.module.less");
    const collapsedBlock = chatModuleCss.match(
      /\.lantai-reasoning:not\(\[open\]\) \{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(collapsedBlock).toBeTruthy();
    expect(collapsedBlock).toContain("display: block !important;");
    expect(collapsedBlock).toContain("max-width: min(760px, 100%) !important;");
    expect(collapsedBlock).not.toContain("max-width: max-content");
  });

  it("animates token context progress with compositor transforms", () => {
    const chatModuleCss = readText("./index.module.less");
    const tokenBarStart = chatModuleCss.indexOf(".tokenContextBar {");
    const tokenBarEnd = chatModuleCss.indexOf(
      ".tokenContextGuidance {",
      tokenBarStart,
    );
    const tokenBarBlock = chatModuleCss.slice(tokenBarStart, tokenBarEnd);

    expect(tokenBarStart).toBeGreaterThanOrEqual(0);
    expect(tokenBarBlock).toContain(
      "transform: scaleX(var(--token-context-scale));",
    );
    expect(tokenBarBlock).toContain("transition: transform 0.22s");
    expect(tokenBarBlock).not.toContain("transition: width");
  });

  it("keeps the composer toolbar grouped, aligned, and visually quiet", () => {
    const chatModuleCss = readText("./index.module.less");
    const capabilityBlock = chatModuleCss.match(
      /\.senderCapabilityGroup \{[\s\S]*?\n\}/,
    )?.[0];
    const dividerBlock = chatModuleCss.match(
      /\.senderToolbarDivider \{[\s\S]*?\n\}/,
    )?.[0];
    const tokenStart = chatModuleCss.indexOf(".tokenMeterButton {");
    const tokenEnd = chatModuleCss.indexOf(
      ".tokenPopoverOverlay {",
      tokenStart,
    );
    const tokenBlock = chatModuleCss.slice(tokenStart, tokenEnd);

    expect(capabilityBlock).toContain("gap: 4px;");
    expect(dividerBlock).toContain("width: 1px;");
    expect(dividerBlock).toContain("height: 16px;");
    expect(dividerBlock).toContain("background: var(--lt-quiet-hairline);");
    expect(tokenBlock).toContain("width: 32px !important;");
    expect(tokenBlock).toContain("border-radius: 9px !important;");
    expect(tokenBlock).toContain("background: transparent;");
    expect(tokenBlock).toContain("box-shadow: none;");
    expect(tokenBlock).not.toContain("border-radius: 999px");
    expect(tokenBlock).not.toContain("inset 0 0 0");
  });

  it("keeps composer focus and primary action states visible in both appearances", () => {
    const chatModuleCss = readText("./index.module.less");
    const lightFocusStart = chatModuleCss.indexOf(
      ".lantai-sender:focus-within {",
    );
    const lightPrimaryStart = chatModuleCss.indexOf(
      ".lantai-sender button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn),",
      lightFocusStart,
    );
    const darkFocusStart = chatModuleCss.indexOf(
      ".lantai-sender:focus-within {",
      lightFocusStart + 1,
    );
    const darkPrimaryStart = chatModuleCss.indexOf(
      ".lantai-sender button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn),",
      darkFocusStart,
    );
    const lightComposerBlock = chatModuleCss.slice(
      lightFocusStart,
      darkFocusStart,
    );
    const darkComposerBlock = chatModuleCss.slice(darkFocusStart);

    expect(lightFocusStart).toBeGreaterThanOrEqual(0);
    expect(lightPrimaryStart).toBeGreaterThan(lightFocusStart);
    expect(darkFocusStart).toBeGreaterThan(lightFocusStart);
    expect(darkPrimaryStart).toBeGreaterThan(darkFocusStart);

    for (const composerBlock of [lightComposerBlock, darkComposerBlock]) {
      expect(composerBlock).toContain("var(--lt-composer-focus-shadow)");
      expect(composerBlock).toContain(
        "button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn):hover:not(:disabled)",
      );
      expect(composerBlock).toContain("color: var(--lt-on-accent) !important;");
      expect(composerBlock).toContain(
        "background: var(--lt-accent-hover) !important;",
      );
      expect(composerBlock).toContain(
        "button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn):focus-visible",
      );
      expect(composerBlock).toContain("box-shadow: var(--lt-focus-shadow)");
      expect(composerBlock).toContain(
        "button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn):active:not(:disabled)",
      );
      expect(composerBlock).toContain(
        "button.lantai-sender-actions-btn:not(.lantai-sender-stop-btn):disabled",
      );
    }
  });

  it("lets an active capability trigger show its label without breaking narrow layouts", () => {
    const chatModuleCss = readText("./index.module.less");
    const activeSelector =
      'button[data-chat-capability-trigger="1"][data-active-choice="true"]';
    const activeStart = chatModuleCss.indexOf(activeSelector);
    const activeEnd = chatModuleCss.indexOf("\n    }", activeStart);
    const activeBlock = chatModuleCss.slice(activeStart, activeEnd);
    const compactStart = chatModuleCss.lastIndexOf(activeSelector);
    const compactEnd = chatModuleCss.indexOf("\n      }", compactStart);
    const compactBlock = chatModuleCss.slice(compactStart, compactEnd);

    expect(activeStart).toBeGreaterThanOrEqual(0);
    expect(activeBlock).toContain("width: auto !important;");
    expect(activeBlock).toContain("max-width: 172px !important;");
    expect(activeBlock).toContain("flex: 0 0 auto !important;");
    expect(compactStart).toBeGreaterThan(activeStart);
    expect(compactBlock).toContain("width: 32px !important;");
    expect(compactBlock).toContain("max-width: 32px !important;");
    expect(compactBlock).toContain("flex: 0 0 32px !important;");
  });

  it("raises the empty-state composer above the welcome while command suggestions are open", () => {
    const chatModuleCss = readText("./index.module.less");
    const commandMenuLayerBlock = chatModuleCss.match(
      /\.chatStage\[data-empty-conversation="true"\][^{]*\{[\s\S]*?\.lantai-chat-anywhere-input\[data-command-menu-open="true"\][^{]*\{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(commandMenuLayerBlock).toBeTruthy();
    expect(commandMenuLayerBlock).toContain("z-index: 3;");
  });

  it("keeps user bubbles from shrinking to Chinese min-content width", () => {
    const chatModuleCss = readText("./index.module.less");
    const codexCss = readText("../../styles/modules/chat-codex.css");
    const lantaiCss = readText("../../styles/modules/lantai-chat.css");
    const combined = [chatModuleCss, codexCss, lantaiCss].join("\n");
    const userMessageStackBlock = chatModuleCss.match(
      /\.lantai-user-message-stack \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const userBubbleBlocks =
      combined.match(
        /\.lantai-bubble-(?:end|user)[^{]*\.lantai-bubble-content(?:-filled)?[^{]*\{[^}]*\}/g,
      ) || [];

    expect(userMessageStackBlock).toBeTruthy();
    expect(userMessageStackBlock).toContain("width: 100%;");
    expect(userMessageStackBlock).toContain("max-width: 100%;");
    expect(userBubbleBlocks.length).toBeGreaterThan(0);
    expect(
      userBubbleBlocks.some((block) =>
        block.includes("width: fit-content !important;"),
      ),
    ).toBe(true);
    for (const block of userBubbleBlocks) {
      expect(block).not.toMatch(/width:\s*auto\s*!important;/);
    }
  });

  it("keeps response actions and model metadata together on the right", () => {
    const chatModuleCss = readText("./index.module.less");
    const actionsBlock = chatModuleCss.match(
      /\.lantai-response-actions \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const metadataBlock = chatModuleCss.match(
      /\.lantai-response-actions > :last-child:not\(\.ant-btn\):not\(\.lantai-btn\) \{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(actionsBlock).toBeTruthy();
    expect(actionsBlock).toContain("justify-content: flex-end;");
    expect(metadataBlock).toBeTruthy();
    expect(metadataBlock).toContain("margin-left: 6px;");
    expect(metadataBlock).not.toContain("margin-left: auto;");
  });

  it("centers the single ququ continuation action on its visual line", () => {
    const codexCss = readText("../../styles/modules/chat-codex.css");
    const actionsBlock = codexCss.match(
      /\[data-block="ququ"\] \.ququ-actions \{[\s\S]*?\n\}/,
    )?.[0];
    const buttonBlock = codexCss.match(
      /\[data-block="ququ"\] \.ququ-actions button \{[\s\S]*?\n\}/,
    )?.[0];
    expect(actionsBlock).toContain("align-items: center;");
    expect(buttonBlock).toContain("display: inline-flex;");
    expect(buttonBlock).toContain("align-items: center;");
    expect(buttonBlock).toContain("justify-content: center;");
    expect(buttonBlock).toContain("height: 1.75rem;");
    expect(buttonBlock).toContain("line-height: 1;");
    expect(codexCss).not.toContain(".ququ-action-more");
  });

  it("keeps model route descenders visible while preserving ellipsis", () => {
    const chatModuleCss = readText("./index.module.less");
    const summaryStart = chatModuleCss.indexOf(".responseTokenSummary {");
    const summaryEnd = chatModuleCss.indexOf(
      ".chatHeaderControls {",
      summaryStart,
    );
    const summaryBlock = chatModuleCss.slice(summaryStart, summaryEnd);
    const modelLabelBlock = summaryBlock.match(
      /> span:last-child > span:last-child \{[\s\S]*?\}/,
    )?.[0];

    expect(summaryStart).toBeGreaterThanOrEqual(0);
    expect(summaryEnd).toBeGreaterThan(summaryStart);
    expect(summaryBlock).toContain("line-height: 1.35;");
    expect(modelLabelBlock).toContain("padding-block: 1px;");
    expect(modelLabelBlock).toContain("overflow: hidden;");
    expect(modelLabelBlock).toContain("text-overflow: ellipsis;");
  });

  it("centers conversation time anchors across the message column", () => {
    const chatModuleCss = readText("./index.module.less");
    const anchorBlock = chatModuleCss.match(
      /\.lantai-conversation-time-anchor \{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(anchorBlock).toBeTruthy();
    expect(anchorBlock).toContain("align-self: center;");
    expect(anchorBlock).toContain("justify-self: center;");
    expect(anchorBlock).toContain("width: fit-content;");
    expect(anchorBlock).toContain("max-width: 100%;");
    expect(anchorBlock).toContain("margin: 18px auto 14px;");
    expect(anchorBlock).toContain("text-align: center;");
  });

  it("adds a distinct gap before the user message that starts the next turn", () => {
    const chatModuleCss = readText("./index.module.less");
    const turnBoundaryBlock = chatModuleCss.match(
      /\.lantai-bubble-list-item\.lantai-bubble-assistant\s*\+\s*\.lantai-bubble-list-item\.lantai-bubble-user \{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(turnBoundaryBlock).toBeTruthy();
    expect(turnBoundaryBlock).toContain("margin-top: 24px;");
  });

  it("gives assistant deliverables an editorial reading measure and rhythm", () => {
    const chatModuleCss = readText("./index.module.less");
    const answerBlock = chatModuleCss.match(
      /\.lantai-assistant-answer \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const paragraphBlock = chatModuleCss.match(
      /\.lantai-assistant-answer\s+\.lantai-copyable-segment\[data-dense="false"\] \{[\s\S]*?\}/,
    )?.[0];
    const listItemGapBlock = chatModuleCss.match(
      /\.lantai-assistant-answer li \+ li \{[\s\S]*?\}/,
    )?.[0];

    expect(answerBlock).toBeTruthy();
    expect(answerBlock).toContain("width: min(100%, 72ch);");
    expect(answerBlock).toContain("font-size: 15px;");
    expect(answerBlock).toContain("font-weight: var(--lt-weight-regular);");
    expect(answerBlock).toContain("line-height: 1.72;");
    expect(paragraphBlock).toContain("margin: 0 0 1em;");
    expect(listItemGapBlock).toContain("margin-top: 0.38em;");
  });

  it("keeps a compact 28px user action row stable when actions are revealed", () => {
    const chatModuleCss = readText("./index.module.less");
    const actionsBlock = chatModuleCss.match(
      /\.lantai-user-message-actions \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const revealBlock = chatModuleCss.match(
      /\.lantai-user-message-stack:hover \.lantai-user-message-actions,[\s\S]*?\.lantai-user-message-stack:focus-within \.lantai-user-message-actions \{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(actionsBlock).toBeTruthy();
    expect(actionsBlock).toContain("height: 28px;");
    expect(actionsBlock).toContain("min-height: 28px;");
    expect(actionsBlock).toContain("margin-top: 0;");
    expect(revealBlock).toBeTruthy();
    expect(revealBlock).not.toMatch(/(?:min-)?height:/);
    expect(revealBlock).not.toContain("margin-top:");
  });

  it("keeps the message list as the only vertical scroll container", () => {
    const chatModuleCss = readText("./index.module.less");
    const messageListBlock = chatModuleCss.match(
      /\.lantai-chat-anywhere-message-list \{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(messageListBlock).toBeTruthy();
    expect(messageListBlock).toContain("flex: 1 1 auto !important;");
    expect(messageListBlock).toContain("min-height: 0 !important;");
    expect(messageListBlock).toContain("height: 100% !important;");
    expect(messageListBlock).toContain("overflow-y: auto !important;");
    expect(messageListBlock).toContain("scroll-padding-bottom: max(");
    expect(messageListBlock).toContain(
      "var(--lt-chat-message-bottom-padding),",
    );
    expect(messageListBlock).toContain(
      "var(--lt-chat-composer-safe-space, 0px)",
    );
    expect(messageListBlock).toContain("overscroll-behavior: contain;");
  });

  it("keeps the conversation navigator beside the transcript and quiet on constrained layouts", () => {
    const chatModuleCss = readText("./index.module.less");
    const navigatorBlock = (
      chatModuleCss.match(
        /\.lantai-conversation-navigator \{[\s\S]*?\n {4}\}/g,
      ) || []
    ).find((block) => block.includes("position: absolute;"));
    const tickBlock = chatModuleCss.match(
      /\.lantai-conversation-navigator-tick \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const centerTickBlock = chatModuleCss.match(
      /\.lantai-conversation-navigator-marker\[data-proximity="0"\]\s+\.lantai-conversation-navigator-tick \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const previewBlock = chatModuleCss.match(
      /\.lantai-conversation-navigator-preview \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const summaryBlock = chatModuleCss.match(
      /\.lantai-conversation-navigator-summary \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const normalizeCss = (value?: string) =>
      value?.replace(/\s+/g, " ").replace(/\(\s+/g, "(").replace(/\s+\)/g, ")");
    const normalizedTickBlock = normalizeCss(tickBlock);
    const normalizedCenterTickBlock = normalizeCss(centerTickBlock);
    const normalizedPreviewBlock = normalizeCss(previewBlock);
    const constrainedLayoutBlock = chatModuleCss.slice(
      chatModuleCss.indexOf("@media (max-width: 1100px)"),
      chatModuleCss.indexOf("@media (max-width: 1280px)"),
    );
    const veryNarrowLayoutBlock = chatModuleCss.slice(
      chatModuleCss.indexOf("@media (max-width: 760px)"),
      chatModuleCss.indexOf("@media (max-width: 720px)"),
    );

    expect(navigatorBlock).toBeTruthy();
    expect(navigatorBlock).toContain("position: absolute;");
    expect(navigatorBlock).toContain("pointer-events: none;");
    expect(navigatorBlock).toContain("left: max(12px, calc(50% - 472px));");
    expect(navigatorBlock).toContain("width: 44px;");
    expect(chatModuleCss).not.toContain(".lantai-conversation-navigator-track");
    expect(tickBlock).toContain("width: 28px;");
    expect(tickBlock).toContain("height: 2px;");
    expect(tickBlock).toContain("--lt-conversation-tick-scale-x: 0.2143;");
    expect(tickBlock).toContain("--lt-conversation-tick-scale-y: 0.5;");
    expect(tickBlock).not.toContain("transition: width");
    expect(normalizedTickBlock).toContain(
      "background: color-mix(in srgb, var(--lt-text-muted) 38%, transparent);",
    );
    expect(centerTickBlock).toContain("--lt-conversation-tick-scale-x: 1;");
    expect(centerTickBlock).toContain("--lt-conversation-tick-scale-y: 1;");
    expect(normalizedCenterTickBlock).toContain(
      "background: color-mix(in srgb, var(--lt-text) 88%, transparent);",
    );
    expect(chatModuleCss).not.toMatch(
      /\.lantai-conversation-navigator-marker\[data-active="true"\]\s+\.lantai-conversation-navigator-tick/,
    );
    expect(chatModuleCss).toContain(
      '.lantai-conversation-navigator-marker[data-proximity="0"]',
    );
    expect(chatModuleCss).toContain(
      '.lantai-conversation-navigator-marker[data-proximity="1"]',
    );
    expect(chatModuleCss).toContain(
      '.lantai-conversation-navigator-marker[data-proximity="2"]',
    );
    expect(chatModuleCss).toContain(
      '.lantai-conversation-navigator-marker[data-proximity="3"]',
    );
    expect(previewBlock).toContain("width: min(304px, calc(100vw - 424px));");
    expect(previewBlock).toContain("min-width: 236px;");
    expect(previewBlock).toContain("padding: 12px 14px 13px;");
    expect(previewBlock).toContain(
      "border: 1px solid var(--lt-border-subtle);",
    );
    expect(previewBlock).toContain("border-radius: 8px;");
    expect(normalizedPreviewBlock).toContain(
      "0 12px 32px color-mix(in srgb, var(--lt-workspace-bg) 58%, transparent)",
    );
    expect(summaryBlock).toContain("-webkit-line-clamp: 3;");
    expect(chatModuleCss).toContain("transition-delay: 90ms;");
    expect(chatModuleCss).toContain(
      '.lantai-conversation-navigator[data-visible="true"]',
    );
    expect(chatModuleCss).toContain("@media (max-width: 1100px)");
    expect(constrainedLayoutBlock).not.toMatch(
      /\.lantai-conversation-navigator\s*\{[\s\S]*?display:\s*none\s*!important/,
    );
    expect(constrainedLayoutBlock).toMatch(
      /\.lantai-conversation-navigator-preview\s*\{[\s\S]*?display:\s*none\s*!important/,
    );
    expect(veryNarrowLayoutBlock).toMatch(
      /\.lantai-conversation-navigator\s*\{[\s\S]*?display:\s*none\s*!important/,
    );
    expect(chatModuleCss).toContain(".lantai-conversation-navigator-preview");
    expect(chatModuleCss).toContain("@media (prefers-reduced-motion: reduce)");
  });

  it("lets boundary wheel gestures escape nested process surfaces", () => {
    const chatModuleCss = readText("./index.module.less");

    for (const selector of [
      ".tokenPopover",
      ".lantai-reasoning-content",
      ".lantai-command-suggestions",
      ".approvalStack",
    ]) {
      const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const block = chatModuleCss.match(
        new RegExp(`${escapedSelector}\\s*\\{([\\s\\S]*?)\\}`),
      )?.[1];

      expect(
        block,
        `${selector} must exist in the chat stylesheet`,
      ).toBeTruthy();
      expect(block).not.toMatch(/overscroll-behavior(?:-y)?:\s*contain/);
    }
  });

  it("keeps collapsed thinking disclosure concise", () => {
    const chatModuleCss = readText("./index.module.less");

    expect(chatModuleCss).not.toContain('[class*="thinking"]');
    expect(chatModuleCss).not.toContain('[class*="Thinking"]');
    expect(chatModuleCss).not.toContain('content: "已折叠";');
    expect(chatModuleCss).not.toContain(
      ".lantai-reasoning:not([open]) summary::after",
    );
  });

  it("keeps long new-chat drafts bounded without clearing empty-state context", () => {
    const chatModuleCss = readText("./index.module.less");
    const emptyConversationStart = chatModuleCss.indexOf(
      '.chatStage[data-empty-conversation="true"] {',
    );
    const emptyConversationEnd = chatModuleCss.indexOf(
      '.chatStage[data-coding-mode="true"] {',
      emptyConversationStart,
    );
    const emptyConversationBlock = chatModuleCss.slice(
      emptyConversationStart,
      emptyConversationEnd,
    );

    expect(emptyConversationStart).toBeGreaterThanOrEqual(0);
    expect(emptyConversationEnd).toBeGreaterThan(emptyConversationStart);
    expect(emptyConversationBlock).toContain(
      "max-height: min(260px, 34dvh) !important;",
    );
    expect(emptyConversationBlock).toContain("overflow-y: auto !important;");
    expect(emptyConversationBlock).toContain("overscroll-behavior-y: contain;");
    expect(chatModuleCss).not.toContain("textarea:not(:placeholder-shown)");
  });

  it("hides empty-state prompts while the composer contains a draft", () => {
    const chatModuleCss = readText("./index.module.less");
    const draftStateBlock = chatModuleCss.match(
      /\.chatStage\[data-empty-conversation="true"\]:has\([\s\S]*?data-has-draft="true"[\s\S]*?\)\s*\{([\s\S]*?)\n\}/,
    )?.[1];

    expect(draftStateBlock).toBeTruthy();
    expect(draftStateBlock).toContain(".emptyPromptChips");
    expect(draftStateBlock).toContain("visibility: hidden;");
    expect(draftStateBlock).toContain("opacity: 0;");
    expect(draftStateBlock).toContain("pointer-events: none;");
  });

  it("keeps empty-state prompt chips clear of an active presence composer", () => {
    const chatModuleCss = readText("./index.module.less");
    const activePresenceChipBlock = chatModuleCss.match(
      /\.chatStage\[data-empty-conversation="true"\]:has\([\s\S]*?\.lantai-sender-outside-context:not\(:empty\)[\s\S]*?\)\s*\{[\s\S]*?\.emptyPromptChips\s*\{([\s\S]*?)\n\s+\}/,
    )?.[1];

    expect(activePresenceChipBlock).toBeTruthy();
    expect(activePresenceChipBlock).toContain(
      "var(--lt-empty-chips-offset) + 30px",
    );
    expect(chatModuleCss).toContain(
      "--lt-empty-chip-row-width: min(100%, calc(100vw - 44px));",
    );
  });

  it("anchors the empty composer by its top edge so expanded drafts cannot cover the welcome", () => {
    const chatModuleCss = readText("./index.module.less");
    const composerBlocks =
      chatModuleCss.match(
        /\.lantai-chat-anywhere-input,\n\s+\.lantai-chat-anywhere-default-footer \{[\s\S]*?\n\s+\}/g,
      ) || [];
    const baseComposerBlock = composerBlocks.find((block) =>
      block.includes("position: absolute !important;"),
    );
    const emptyConversationStart = chatModuleCss.indexOf(
      '.chatStage[data-empty-conversation="true"] {',
    );
    const emptyConversationEnd = chatModuleCss.indexOf(
      '.chatStage[data-coding-mode="true"] {',
      emptyConversationStart,
    );
    const emptyConversationBlock = chatModuleCss.slice(
      emptyConversationStart,
      emptyConversationEnd,
    );

    expect(baseComposerBlock).toBeTruthy();
    expect(baseComposerBlock).toContain("top: auto;");
    expect(baseComposerBlock).toContain("bottom: 0;");
    expect(baseComposerBlock).toContain(
      "bottom var(--lt-motion-duration-emphasis)",
    );
    expect(baseComposerBlock).toContain(
      "transform var(--lt-motion-duration-emphasis)",
    );
    expect(emptyConversationBlock).toContain(
      "bottom: calc(100% - var(--lt-empty-composer-top));",
    );
    expect(emptyConversationBlock).toContain("transform: translateY(100%);");
    expect(chatModuleCss).toContain(
      "--lt-empty-composer-top: calc(var(--lt-empty-composer-center) - 74px);",
    );
    expect(emptyConversationBlock).not.toContain("translateY(50%)");
    expect(emptyConversationBlock).not.toContain(
      "top: var(--lt-empty-composer-center);",
    );
    expect(chatModuleCss).toContain(
      '.chatStage[data-first-turn-transition="true"]',
    );
    expect(chatModuleCss).toContain("data-lantai-welcome-exiting");

    const exitLayerBlock = chatModuleCss.slice(
      chatModuleCss.indexOf('.chatStage[data-first-turn-transition="true"]'),
      chatModuleCss.indexOf("@keyframes lantai-first-turn-transcript-in"),
    );
    expect(exitLayerBlock).toContain('data-lantai-welcome-exiting="1"');
    expect(exitLayerBlock).toContain("opacity: 0;");
    expect(exitLayerBlock).toContain(
      "transform: translateY(-16px) scale(0.985);",
    );
  });

  it("keeps live thinking collapsed until users request the full viewer", () => {
    const chatModuleCss = readText("./index.module.less");
    const expandedBlock = chatModuleCss.match(
      /\.lantai-reasoning\[data-view-state="expanded"\] \.lantai-reasoning-content \{([\s\S]*?)\}/,
    )?.[1];

    expect(chatModuleCss).not.toContain('data-view-state="live-preview"');
    expect(chatModuleCss).toContain(".lantai-reasoning:not([open])");
    expect(expandedBlock).toContain("max-height: min(62vh, 640px)");
    expect(expandedBlock).toContain("overflow-y: auto");
    expect(chatModuleCss).toContain(".lantai-reasoning-action");
  });

  it("keeps standalone text artifacts readable and width-bounded", () => {
    const chatModuleCss = readText("./index.module.less");
    const artifactBlock = chatModuleCss.match(
      /\.lantai-text-artifact \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const bodyBlock = chatModuleCss.match(
      /\.lantai-text-artifact-body \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const markdownBlock = chatModuleCss.match(
      /\.lantai-text-artifact-body \.lantai-markdown \{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(artifactBlock).toContain("width: min(100%, 720px);");
    expect(bodyBlock).toContain("padding: 14px 18px 16px;");
    expect(markdownBlock).toContain("max-width: 70ch;");
    expect(markdownBlock).toContain("white-space: pre-wrap;");
  });

  it("keeps document artifacts inside the same framed card system", () => {
    const chatModuleCss = readText("./index.module.less");
    const documentBlock = chatModuleCss.match(
      /\.lantai-document-output \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const bodyBlock = chatModuleCss.match(
      /\.lantai-document-output-body \{[\s\S]*?\n {4}\}/,
    )?.[0];
    const headerBlock = chatModuleCss.match(
      /\.lantai-artifact-header,\n {4}\.lantai-document-output-header \{[\s\S]*?\n {4}\}/,
    )?.[0];

    expect(documentBlock).toContain("width: min(100%, 720px);");
    expect(documentBlock).not.toContain("border-left:");
    expect(documentBlock).not.toContain("padding-left:");
    expect(bodyBlock).toContain("padding: 14px 18px 18px;");
    expect(headerBlock).toContain("background: var(--lt-surface-muted);");
    expect(headerBlock).toContain(
      "border-bottom: 1px solid var(--lt-border-subtle);",
    );
  });

  it("keeps the session history switcher fully opaque", () => {
    const switcherCss = readText(
      "./components/ChatHeaderTitle/index.module.less",
    );
    const panelBlock = switcherCss.match(/\.panel \{[\s\S]*?\n\}/)?.[0];

    expect(panelBlock).toBeTruthy();
    expect(panelBlock).toContain("background: var(--lt-surface-elevated);");
    expect(panelBlock).toContain("backdrop-filter: none;");
    expect(panelBlock).toContain("-webkit-backdrop-filter: none;");
    expect(panelBlock).not.toContain("var(--lt-glass-surface)");
  });

  it("keeps the execution status entry in the chat header", () => {
    const chatPageSource = readText("./index.tsx");
    const headerActions = chatPageSource.match(
      /<div className=\{styles\.chatHeaderActions\}>[\s\S]*?<ChatSessionDrawer/,
    )?.[0];

    expect(headerActions).toBeTruthy();
    expect(headerActions).toContain("<TurnEventPreview />");
    expect(chatPageSource).not.toContain(
      '<TurnEventPreview variant="strip" />',
    );
    expect(chatPageSource).toContain('<TurnEventPreview variant="presence" />');
  });

  it("places active-turn presence above the composer instead of inside it", () => {
    const chatPageSource = readText("./index.tsx");
    const chatVendorSource = readText("../../vendor/lantaiChat.ts");

    expect(chatPageSource).toMatch(
      /outsideContext:\s*codingMode\s*\?\s*null\s*:\s*\(\s*<TurnEventPreview variant="presence"\s*\/>/,
    );
    const outsideContextIndex = chatVendorSource.indexOf(
      "lantai-sender-outside-context",
    );
    const senderIndex = chatVendorSource.indexOf(
      'className: "lantai-sender"',
      outsideContextIndex,
    );
    expect(outsideContextIndex).toBeGreaterThanOrEqual(0);
    expect(senderIndex).toBeGreaterThan(outsideContextIndex);
  });

  it("also renders active-turn progress beside the latest response", () => {
    const chatPageSource = readText("./index.tsx");

    expect(chatPageSource).toContain(
      'props.isLast ? <TurnEventPreview variant="inline" /> : null',
    );
  });

  it("retains transcript occlusion beneath the translucent presence rail", () => {
    const css = readText("./index.module.less");
    const footer = css.match(
      /\.lantai-chat-anywhere-input,\s*\.lantai-chat-anywhere-default-footer \{([\s\S]*?)\}/,
    )?.[1];
    const sender = css.match(
      /\.lantai-sender,\s*\.lantai-sender-content \{([\s\S]*?)\}/,
    )?.[1];
    expect(footer).toContain("background: var(--lt-quiet-composer-mask)");
    expect(footer).toContain("blur(var(--lt-occluder-blur))");
    expect(sender).toContain("background: var(--lt-occluder-surface)");
  });

  it("stacks active-turn presence above the composer on the real sender shell", () => {
    const chatModuleCss = readText("./index.module.less");
    const presenceLayoutBlock = chatModuleCss.match(
      /\.lantai-chat-anywhere-input:has\([\s\S]*?\.lantai-sender-outside-context:not\(:empty\)[\s\S]*?\)\s*,[\s\S]*?\.lantai-chat-anywhere-default-footer:has\([\s\S]*?\.lantai-sender-outside-context:not\(:empty\)[\s\S]*?\)\s*\{[\s\S]*?\n\s+\}/,
    )?.[0];

    expect(presenceLayoutBlock).toBeTruthy();
    expect(presenceLayoutBlock).toContain("flex-direction: column;");
    expect(presenceLayoutBlock).toContain("align-items: center !important;");
    expect(presenceLayoutBlock).toContain(
      "justify-content: flex-start !important;",
    );

    const presenceSenderBlock = chatModuleCss.match(
      /\.lantai-chat-anywhere-input:has\([\s\S]*?\.lantai-sender-outside-context:not\(:empty\)[\s\S]*?> \.lantai-sender,[\s\S]*?\.lantai-chat-anywhere-default-footer:has\([\s\S]*?\.lantai-sender-outside-context:not\(:empty\)[\s\S]*?> \.lantai-sender\s*\{[\s\S]*?\n\s+\}/,
    )?.[0];

    expect(presenceSenderBlock).toBeTruthy();
    expect(presenceSenderBlock).toContain("flex: 0 0 auto !important;");
    const emptyConversationOverrides = chatModuleCss.slice(
      chatModuleCss.indexOf(
        '.chatStage[data-empty-conversation="true"] .chatMessagesArea',
      ),
    );

    expect(emptyConversationOverrides).toMatch(
      /\.lantai-chat-anywhere-input:not\(\s*:has\(\.lantai-sender-outside-context:not\(:empty\)\)\s*\)/,
    );
    expect(emptyConversationOverrides).toMatch(
      /\.lantai-chat-anywhere-default-footer:not\(\s*:has\(\.lantai-sender-outside-context:not\(:empty\)\)\s*\)/,
    );
    expect(emptyConversationOverrides).not.toMatch(
      /\.lantai-chat-anywhere-input,\s*\.lantai-chat-anywhere-default-footer\s*\{/,
    );
    expect(emptyConversationOverrides).not.toMatch(
      /\.lantai-sender,\s*\.lantai-sender-content\s*\{/,
    );

    const emptyStageStart = chatModuleCss.indexOf(
      '.chatStage[data-empty-conversation="true"] {',
    );
    const emptyStageEnd = chatModuleCss.indexOf(
      '.chatStage[data-empty-conversation="true"],',
      emptyStageStart,
    );
    const emptyStageBlock = chatModuleCss.slice(emptyStageStart, emptyStageEnd);

    expect(emptyStageStart).toBeGreaterThanOrEqual(0);
    expect(emptyStageEnd).toBeGreaterThan(emptyStageStart);
    expect(emptyStageBlock).toMatch(
      /\.lantai-chat-anywhere-input:not\(\s*:has\(\.lantai-sender-outside-context:not\(:empty\)\)\s*\)[\s\S]*?\.lantai-sender/,
    );
    expect(emptyStageBlock).not.toMatch(
      /\.lantai-sender,\s*\.lantai-sender-content\s*\{/,
    );
  });

  it("keeps the active-turn presence line compact but visibly floating", () => {
    const previewCss = readText("./components/TurnEventPreview.module.less");
    const presenceBlock = previewCss.match(
      /\.presenceButton \{[\s\S]*?\n\}/,
    )?.[0];
    const wrapperBlock = previewCss.match(
      /\.wrapper\[data-variant="presence"\] \{[\s\S]*?\n\}/,
    )?.[0];

    expect(presenceBlock).toBeTruthy();
    expect(wrapperBlock).toContain("width: calc(100% - 12px);");
    expect(presenceBlock).toContain("min-height: 40px;");
    expect(presenceBlock).toContain(
      "grid-template-columns: 24px minmax(0, 1fr) auto 14px;",
    );
    expect(presenceBlock).toContain(
      "var(--lt-composer-strip-surface, var(--lt-float-surface))",
    );
    expect(presenceBlock).toContain(
      "border: 1px solid var(--lt-quiet-hairline);",
    );
    expect(presenceBlock).toContain(
      "backdrop-filter: blur(var(--lt-float-blur)) saturate(var(--lt-glass-saturate));",
    );
    expect(presenceBlock).toContain(
      "0 3px 12px rgba(var(--lt-shadow-color), 0.045)",
    );
  });

  it("keeps long presence copy contained and gives Shen Xingzhi a compact identity", () => {
    const previewCss = readText("./components/TurnEventPreview.module.less");
    const messageStart = previewCss.lastIndexOf("\n.presenceMessage {");
    const messageEnd = previewCss.indexOf("\n}\n", messageStart);
    const messageBlock = previewCss.slice(messageStart, messageEnd);
    const copyBlock = previewCss.match(/\.presenceCopy \{[\s\S]*?\n\}/)?.[0];

    expect(copyBlock).toContain("min-width: 0;");
    expect(messageStart).toBeGreaterThanOrEqual(0);
    expect(messageEnd).toBeGreaterThan(messageStart);
    expect(messageBlock).toContain("min-width: 0;");
    expect(messageBlock).toContain("overflow: hidden;");
    expect(messageBlock).toContain("text-overflow: ellipsis;");
    expect(messageBlock).toContain("white-space: nowrap;");
    expect(previewCss).toContain(".presenceAvatar {");
    expect(previewCss).toContain(".presenceIdentity {");
    expect(previewCss).toContain("color: var(--lt-accent-hover);");
    expect(previewCss).toContain("outline: 2px solid var(--lt-accent-hover);");
  });

  it("keeps the presence rail and composer on one responsive width contract", () => {
    const chatModuleCss = readText("./index.module.less");
    const senderBlock = chatModuleCss.match(
      /\.lantai-sender,\n\s+\.lantai-sender-content \{[\s\S]*?\n\s+\}/,
    )?.[0];
    const outsideContextBlock = chatModuleCss.match(
      /\.lantai-sender-outside-context \{[\s\S]*?\n\s+\}/,
    )?.[0];

    expect(chatModuleCss).toContain("--lt-composer-shell-max-width: 980px;");
    expect(senderBlock).toContain(
      "max-width: min(100%, var(--lt-composer-shell-max-width)) !important;",
    );
    expect(outsideContextBlock).toContain(
      "max-width: min(100%, var(--lt-composer-shell-max-width));",
    );
    expect(chatModuleCss).not.toContain("calc(100vw - 390px)");
  });

  it("animates only an active presence dot and keeps every tone chromatically coherent", () => {
    const previewCss = readText("./components/TurnEventPreview.module.less");
    const stateBlock = previewCss.slice(
      previewCss.indexOf(".presenceSignal {"),
      previewCss.indexOf(".presenceCopy {"),
    );
    const baseStateBlock = previewCss.match(
      /\.presenceSignal \{[\s\S]*?\n\}/,
    )?.[0];

    expect(stateBlock).toContain(
      '.presenceButton[data-tone="active"] .presenceSignal',
    );
    expect(stateBlock).toContain(
      '.presenceButton[data-tone="quiet"] .presenceSignal',
    );
    expect(stateBlock).toContain(
      '.presenceButton[data-tone="warning"] .presenceSignal',
    );
    expect(stateBlock).toContain(
      '.presenceButton[data-tone="danger"] .presenceSignal',
    );
    expect(stateBlock).not.toContain("::after");
    expect(previewCss).not.toContain("presenceRipple");
    expect(stateBlock).toContain("color: var(--lt-accent-hover);");
    expect(stateBlock).toContain(
      "color: color-mix(in srgb, var(--lt-warning) 70%, var(--lt-text));",
    );
    expect(stateBlock).toContain(
      "color: color-mix(in srgb, var(--lt-danger) 72%, var(--lt-text));",
    );
    expect(stateBlock).toContain(
      "animation: presenceBreath 1.8s var(--lt-motion-ease-out) infinite;",
    );
    expect(baseStateBlock).not.toContain("animation: presenceBreath");
  });

  it("removes presence motion and secondary chrome when the viewport asks for it", () => {
    const previewCss = readText("./components/TurnEventPreview.module.less");
    const reducedMotionBlock = previewCss.slice(
      previewCss.indexOf("@media (prefers-reduced-motion: reduce)"),
      previewCss.indexOf("@media (max-width: 720px)"),
    );
    const narrowBlock = previewCss.slice(
      previewCss.indexOf("@media (max-width: 720px)"),
    );

    expect(reducedMotionBlock).toContain(".presenceButton,");
    expect(reducedMotionBlock).toContain("transition: none !important;");
    expect(reducedMotionBlock).toContain("transform: none !important;");
    expect(reducedMotionBlock).toContain("animation: none !important;");
    expect(narrowBlock).toContain(
      "grid-template-columns: 24px minmax(0, 1fr) 14px;",
    );
    expect(narrowBlock).toContain(".presenceProcess {");
    expect(narrowBlock).toContain(".presenceChevron {");
    expect(narrowBlock).toContain("display: none;");
    expect(narrowBlock).toContain("-webkit-line-clamp: 2;");
    expect(narrowBlock).toContain(".drawerCloseButton {");
    expect(narrowBlock).toContain("width: 44px;");
    expect(narrowBlock).toContain("height: 44px;");
    expect(narrowBlock).toContain("min-height: 44px;");
  });

  it("provides an opaque fallback when reduced transparency is requested", () => {
    const previewCss = readText("./components/TurnEventPreview.module.less");
    const reducedTransparencyBlock = previewCss.slice(
      previewCss.indexOf("@media (prefers-reduced-transparency: reduce)"),
      previewCss.indexOf("@media (prefers-reduced-motion: reduce)"),
    );

    expect(reducedTransparencyBlock).toContain(".presenceButton {");
    expect(reducedTransparencyBlock).toContain(
      "background: var(--lt-glass-fallback-bg);",
    );
    expect(reducedTransparencyBlock).toContain("backdrop-filter: none;");
    expect(reducedTransparencyBlock).toContain(
      "-webkit-backdrop-filter: none;",
    );
  });

  it("keeps the execution status orb stateful and reduced-motion safe", () => {
    const previewCss = readText("./components/TurnEventPreview.module.less");

    expect(previewCss).toContain('.statusOrb[data-status="running"]');
    expect(previewCss).toContain('.statusOrb[data-status="completed"]');
    expect(previewCss).toContain('.statusOrb[data-status="failed"]');
    expect(previewCss).toContain("@media (prefers-reduced-motion: reduce)");
    expect(previewCss).toContain(".statusOrbDots");
    expect(previewCss).toContain("animation: none;");
  });
});
