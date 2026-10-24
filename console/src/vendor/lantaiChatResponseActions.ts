import React from "react";
import { Button } from "antd";
import { useChatAnywhereOptions } from "./lantaiChat";

export default function LantaiResponseActions(props: Record<string, unknown>) {
  const actions = useChatAnywhereOptions(
    (options) => options.actions?.list || [],
  );
  if (!Array.isArray(actions) || actions.length === 0) return null;
  return React.createElement(
    "div",
    {
      className: "lantai-response-actions",
      "data-lantai-response-actions": "1",
      role: "group",
      "aria-label": "助手消息操作",
    } as React.HTMLAttributes<HTMLDivElement> & {
      "data-lantai-response-actions": string;
    },
    actions.map((action: Record<string, unknown>, index: number) => {
      if (typeof action.render === "function") {
        return React.createElement(
          React.Fragment,
          { key: index },
          action.render(props),
        );
      }
      return React.createElement(Button, {
        key: index,
        type: "text",
        size: "small",
        icon: action.icon as React.ReactNode,
        onClick: () => {
          if (typeof action.onClick === "function") action.onClick(props);
        },
      });
    }),
  );
}
