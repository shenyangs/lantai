import React, { forwardRef } from "react";
import {
  Button as AntButton,
  ConfigProvider as AntConfigProvider,
} from "antd";

export * from "antd";

type LantaiButtonProps = Omit<React.ComponentProps<typeof AntButton>, "type"> & {
  bordered?: boolean;
  iconSize?: number | string;
  type?:
    | React.ComponentProps<typeof AntButton>["type"]
    | "primaryLess"
    | "textCompact";
};

function normalizeButtonType(type: LantaiButtonProps["type"]) {
  if (type === "primaryLess") return "primary";
  if (type === "textCompact") return "text";
  return type;
}

export const Button = forwardRef<HTMLElement, LantaiButtonProps>(
  function Button({ icon, iconSize, type, bordered: _bordered, ...props }, ref) {
    const renderedIcon =
      React.isValidElement(icon) && iconSize != null
        ? React.cloneElement(icon, { size: iconSize } as Record<string, unknown>)
        : icon;

    return React.createElement(AntButton as any, {
      ref,
      type: normalizeButtonType(type),
      ...props,
      icon: renderedIcon,
    });
  },
);

export const IconButton = forwardRef<HTMLElement, LantaiButtonProps>(
  function IconButton({ bordered = true, shape, type, ...props }, ref) {
    return React.createElement(Button, {
      ref,
      type: type ?? "text",
      shape: shape ?? "circle",
      ...props,
      className: [
        props.className,
        bordered ? "lantai-icon-button-bordered" : "lantai-icon-button",
      ]
        .filter(Boolean)
        .join(" "),
    });
  },
);

type LantaiConfigProviderProps = React.ComponentProps<typeof AntConfigProvider> & {
  prefix?: string;
};

export function ConfigProvider({
  prefix: _prefix,
  ...props
}: LantaiConfigProviderProps) {
  return React.createElement(AntConfigProvider, props);
}

export const bailianTheme = {};
export const bailianDarkTheme = {};
