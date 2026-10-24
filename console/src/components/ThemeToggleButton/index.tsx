import { Dropdown, Button, Tooltip, type MenuProps } from "antd";
import {
  SparkMoonLine,
  SparkSunLine,
  SparkComputerLine,
} from "@/vendor/lantaiIcons";
import { useTheme, type ThemeMode } from "../../contexts/ThemeContext";
import { useTranslation } from "react-i18next";
import type { ReactNode } from "react";
import styles from "./index.module.less";

const ICONS: Record<ThemeMode, ReactNode> = {
  light: <SparkSunLine />,
  dark: <SparkMoonLine />,
  system: <SparkComputerLine />,
};

interface ThemeToggleButtonProps {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

export default function ThemeToggleButton({
  open,
  onOpenChange,
}: ThemeToggleButtonProps = {}) {
  const { themeMode, isDark, setThemeMode } = useTheme();
  const { t } = useTranslation();

  const items: MenuProps["items"] = [
    {
      key: "light",
      icon: ICONS.light,
      label: t("theme.light"),
      onClick: () => setThemeMode("light"),
    },
    {
      key: "dark",
      icon: ICONS.dark,
      label: t("theme.dark"),
      onClick: () => setThemeMode("dark"),
    },
    {
      key: "system",
      icon: ICONS.system,
      label: t("theme.system"),
      onClick: () => setThemeMode("system"),
    },
  ];

  const icon =
    themeMode === "system" ? ICONS.system : ICONS[isDark ? "dark" : "light"];

  return (
    <Tooltip title={t(`theme.${themeMode}`)}>
      <span>
        <Dropdown
          menu={{ items, selectedKeys: [themeMode] }}
          open={open}
          onOpenChange={onOpenChange}
          placement="bottomRight"
          rootClassName={styles.themeDropdown}
          trigger={["click"]}
        >
          <Button
            aria-label={t(`theme.${themeMode}`)}
            aria-haspopup="menu"
            aria-expanded={open ?? false}
            className={styles.toggleBtn}
            type="text"
            icon={icon}
          />
        </Dropdown>
      </span>
    </Tooltip>
  );
}
