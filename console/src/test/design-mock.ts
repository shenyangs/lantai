/**
 * Global stub for @/vendor/lantaiDesign in tests.
 * The real lib/ is 3MB and causes long hangs when loaded via deps.inline.
 * Tests that need specific behavior can override with vi.mock('@/vendor/lantaiDesign', factory).
 */
import React from "react";

const passThrough = ({ children, ...props }: Record<string, unknown>) =>
  React.createElement("div", props, children as any);

const buttonLike = ({
  children,
  onClick,
  icon,
  iconSize,
  size,
  loading: _loading,
  danger: _danger,
  ...props
}: Record<string, unknown>) => {
  const renderedIcon = React.isValidElement(icon)
    ? React.cloneElement(icon, { size: iconSize ?? size } as any)
    : icon;

  return React.createElement(
    "button",
    { onClick, ...props },
    renderedIcon as any,
    children as any,
  );
};

export const IconButton = buttonLike;
export const Dropdown = passThrough;
export const Button = buttonLike;
export const Input = (props: Record<string, unknown>) =>
  React.createElement("input", props as any);
export const Tag = ({
  children,
  color: _color,
  ...props
}: Record<string, unknown>) =>
  React.createElement("span", props, children as any);
export const Tooltip = ({
  children,
  title,
  ...props
}: Record<string, unknown>) =>
  React.createElement("span", { title: String(title ?? ""), ...props }, children as any);
export const Popconfirm = ({
  children,
  onConfirm,
  ...props
}: Record<string, unknown>) => {
  if (React.isValidElement(children)) {
    const childProps = children.props as Record<string, unknown>;
    return React.cloneElement(children, {
      ...props,
      onClick: (event: unknown) => {
        if (typeof childProps.onClick === "function") {
          childProps.onClick(event);
        }
        if (typeof onConfirm === "function") {
          onConfirm(event);
        }
      },
    } as any);
  }
  return React.createElement("span", props, children as any);
};
export const Table = ({
  columns = [],
  dataSource = [],
  rowKey,
  locale,
}: Record<string, unknown>) => {
  const cols = columns as Array<Record<string, any>>;
  const rows = dataSource as Array<Record<string, any>>;
  const getKey =
    typeof rowKey === "function"
      ? rowKey
      : (row: Record<string, any>, index: number) =>
          row[String(rowKey || "key")] ?? index;

  return React.createElement(
    "table",
    null,
    React.createElement(
      "thead",
      null,
      React.createElement(
        "tr",
        null,
        cols.map((column, index) =>
          React.createElement("th", { key: column.key ?? index }, column.title),
        ),
      ),
    ),
    React.createElement(
      "tbody",
      null,
      rows.length
        ? rows.map((row, rowIndex) =>
            React.createElement(
              "tr",
              { key: getKey(row, rowIndex) },
              cols.map((column, colIndex) => {
                const value = column.dataIndex ? row[column.dataIndex] : undefined;
                const content =
                  typeof column.render === "function"
                    ? column.render(value, row, rowIndex)
                    : value;
                return React.createElement(
                  "td",
                  { key: column.key ?? colIndex },
                  content,
                );
              }),
            ),
          )
        : React.createElement(
            "tr",
            null,
            React.createElement(
              "td",
              { colSpan: Math.max(cols.length, 1) },
              (locale as any)?.emptyText ?? null,
            ),
          ),
    ),
  );
};

export default {
  IconButton,
  Dropdown,
  Button,
  Input,
  Tag,
  Tooltip,
  Popconfirm,
  Table,
};
