import type { CSSProperties } from "react";

type Vessel = "cup" | "bottle" | "bowl" | "glass" | "pot" | "can";
type Mark =
  | "search"
  | "compass"
  | "pen"
  | "chart"
  | "code"
  | "layers"
  | "spark"
  | "document"
  | "checklist"
  | "shield"
  | "coin"
  | "trend"
  | "scale"
  | "megaphone"
  | "handshake"
  | "people"
  | "boxes"
  | "book"
  | "flask"
  | "columns"
  | "heart"
  | "news"
  | "broadcast"
  | "quote";

export interface BeverageAgentAvatarSpec {
  name: string;
  color: string;
  tint: string;
  vessel: Vessel;
  mark: Mark;
}

/**
 * A deliberately constrained illustration system for the 23 built-in beverage
 * agents: one monoline language, one rounded square canvas, a beverage vessel,
 * and one small role mark. Custom agents intentionally keep the generic avatar.
 */
export const BEVERAGE_AGENT_AVATARS: Record<string, BeverageAgentAvatarSpec> = {
  Lantai_Coconut_Agent: {
    name: "椰汁",
    color: "#2F8B78",
    tint: "#E2F3EC",
    vessel: "bowl",
    mark: "search",
  },
  Lantai_Americano_Agent: {
    name: "美式",
    color: "#5B4437",
    tint: "#F3E9E1",
    vessel: "cup",
    mark: "compass",
  },
  Lantai_Matcha_Agent: {
    name: "抹茶",
    color: "#4C8C58",
    tint: "#E5F2E5",
    vessel: "bowl",
    mark: "pen",
  },
  Lantai_Soda_Agent: {
    name: "苏打",
    color: "#397FB9",
    tint: "#E3F0FA",
    vessel: "bottle",
    mark: "chart",
  },
  Lantai_Water_Agent: {
    name: "白水",
    color: "#4B89A5",
    tint: "#E3F2F6",
    vessel: "glass",
    mark: "code",
  },
  // Cola predates the current capability roster but remains a real built-in
  // workspace for existing users, so it gets the same visual system.
  Lantai_Cola_Agent: {
    name: "可乐",
    color: "#C8534F",
    tint: "#F9E8E7",
    vessel: "can",
    mark: "quote",
  },
  Lantai_Mocha_Agent: {
    name: "摩卡",
    color: "#93634B",
    tint: "#F5E8E1",
    vessel: "cup",
    mark: "layers",
  },
  Lantai_Orange_Agent: {
    name: "橙汁",
    color: "#E27835",
    tint: "#FFF0E4",
    vessel: "glass",
    mark: "spark",
  },
  Lantai_Latte_Agent: {
    name: "拿铁",
    color: "#9B7758",
    tint: "#F4ECE2",
    vessel: "cup",
    mark: "document",
  },
  Lantai_Espresso_Agent: {
    name: "浓缩",
    color: "#4C3A34",
    tint: "#EEE5E1",
    vessel: "cup",
    mark: "checklist",
  },
  Lantai_Longjing_Agent: {
    name: "龙井",
    color: "#618851",
    tint: "#E9F2E4",
    vessel: "pot",
    mark: "shield",
  },
  Lantai_BlackCoffee_Agent: {
    name: "黑咖",
    color: "#564943",
    tint: "#F0EBE8",
    vessel: "cup",
    mark: "coin",
  },
  Lantai_Puer_Agent: {
    name: "普洱",
    color: "#775A45",
    tint: "#F2E9E2",
    vessel: "pot",
    mark: "trend",
  },
  Lantai_RockTea_Agent: {
    name: "岩茶",
    color: "#6B6659",
    tint: "#EFEEE8",
    vessel: "pot",
    mark: "scale",
  },
  Lantai_MilkTea_Agent: {
    name: "奶茶",
    color: "#B06967",
    tint: "#F8E7E5",
    vessel: "glass",
    mark: "megaphone",
  },
  Lantai_BlackTea_Agent: {
    name: "红茶",
    color: "#8B594E",
    tint: "#F4E8E5",
    vessel: "pot",
    mark: "handshake",
  },
  Lantai_Cocoa_Agent: {
    name: "可可",
    color: "#74534C",
    tint: "#F1E7E4",
    vessel: "cup",
    mark: "people",
  },
  Lantai_BarleyTea_Agent: {
    name: "麦茶",
    color: "#817342",
    tint: "#F4F0DF",
    vessel: "bottle",
    mark: "boxes",
  },
  Lantai_SoyMilk_Agent: {
    name: "豆乳",
    color: "#8A7659",
    tint: "#F5EEE5",
    vessel: "glass",
    mark: "book",
  },
  Lantai_ColdBrew_Agent: {
    name: "冷萃",
    color: "#466F82",
    tint: "#E4F0F4",
    vessel: "bottle",
    mark: "flask",
  },
  Lantai_WhiteTea_Agent: {
    name: "白茶",
    color: "#72806D",
    tint: "#EDF2E9",
    vessel: "pot",
    mark: "columns",
  },
  Lantai_WarmWater_Agent: {
    name: "温水",
    color: "#A36A5E",
    tint: "#F8EAE6",
    vessel: "glass",
    mark: "heart",
  },
  Lantai_ClearTea_Agent: {
    name: "清茶",
    color: "#537A68",
    tint: "#E8F1EB",
    vessel: "cup",
    mark: "news",
  },
  Lantai_Oolong_Agent: {
    name: "乌龙",
    color: "#8C6F4B",
    tint: "#F4ECDD",
    vessel: "pot",
    mark: "broadcast",
  },
};

export function getBeverageAgentAvatar(agentId: string) {
  return BEVERAGE_AGENT_AVATARS[agentId];
}

function VesselDrawing({ vessel }: { vessel: Vessel }) {
  switch (vessel) {
    case "bottle":
      return (
        <path d="M20 9h8m-6 0v5l-5 8v13c0 2.2 1.8 4 4 4h6c2.2 0 4-1.8 4-4V22l-5-8V9" />
      );
    case "can":
      return <path d="M18 12h12m-10 0 1 26h6l1-26m-8 5h8m-7 16h6m-5 9h4" />;
    case "bowl":
      return (
        <path d="M14 20h20c0 9-4.5 15-10 15s-10-6-10-15Zm4-4c1.6-4 4.2-6 6-6s4.4 2 6 6M19 39h10" />
      );
    case "glass":
      return <path d="M16 12h16l-2 25H18l-2-25Zm3 7h10M20 42h8" />;
    case "pot":
      return (
        <path d="M17 18h15v8c0 7-3.4 12-8 12s-8-5-8-12v-8Zm15 3h4c2.2 0 3 1.7 3 3.5S38.2 28 36 28h-3M20 14c1-2.8 3-4 4-4s3 1.2 4 4M13 19l4 4M18 39h12" />
      );
    default:
      return (
        <path d="M15 17h17v11c0 6.1-3.8 10-8.5 10S15 34.1 15 28V17Zm17 4h3c2.3 0 4 1.8 4 4s-1.7 4-4 4h-3M19 13h10M18 42h12" />
      );
  }
}

function RoleMark({ mark }: { mark: Mark }) {
  const props = {
    strokeWidth: 1.75,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  const line = (d: string) => <path d={d} {...props} />;
  switch (mark) {
    case "search":
      return <>{line("M34 12a4.5 4.5 0 1 0 0.01 0M37.3 15.3 41 19")}</>;
    case "compass":
      return <>{line("m34 10 4.5 9-9 4.5 4.5-9Z")}</>;
    case "pen":
      return <>{line("m30 20 8-8 3 3-8 8-4 1 1-4Z")}</>;
    case "chart":
      return <>{line("M29 23v-4m5 4v-7m5 7V12m-11 11h13")}</>;
    case "code":
      return <>{line("m30 15-4 4 4 4m8-8 4 4-4 4")}</>;
    case "layers":
      return <>{line("m25 17 7-4 7 4-7 4-7-4Zm0 5 7 4 7-4m-14 5 7 4 7-4")}</>;
    case "spark":
      return (
        <>{line("m34 10 1.5 5 5 1.5-5 1.5-1.5 5-1.5-5-5-1.5 5-1.5L34 10Z")}</>
      );
    case "document":
      return <>{line("M29 11h7l4 4v13H29V11Zm7 0v5h5M32 21h5m-5 3h5")}</>;
    case "checklist":
      return <>{line("m29 16 1.5 1.5 3-3m1 2h5m-10 6 1.5 1.5 3-3m1 2h5")}</>;
    case "shield":
      return (
        <>
          {line(
            "M34 11 40 14v6c0 4-2.5 7-6 9-3.5-2-6-5-6-9v-6l6-3Zm-3 9 2 2 4-5",
          )}
        </>
      );
    case "coin":
      return (
        <>
          {line(
            "M34 11a6 6 0 1 0 0.01 0M36 15h-3a1.5 1.5 0 1 0 0 3h2a1.5 1.5 0 1 1 0 3h-3m2-8v10",
          )}
        </>
      );
    case "trend":
      return <>{line("M28 24h12V12m0 0-5 5-3-3-4 4")}</>;
    case "scale":
      return (
        <>
          {line(
            "M34 11v14m-6-10h12m-12 4-3 5h6l-3-5Zm12 0-3 5h6l-3-5ZM29 28h10",
          )}
        </>
      );
    case "megaphone":
      return <>{line("M28 18h4l7-4v12l-7-4h-4v-4Zm4 4 2 6")}</>;
    case "handshake":
      return (
        <>{line("m26 18 4-3 4 3m-8 0 4 4 2-2 2 2 3-3 3 3-5 5-3-3-2 2-4-4")}</>
      );
    case "people":
      return (
        <>
          {line(
            "M34 17a3 3 0 1 0 0.01 0M28 19a2.5 2.5 0 1 0 0.01 0M40 19a2.5 2.5 0 1 0 0.01 0M27 27c0-3 2-5 5-5m0 5c0-3 2-5 5-5s5 2 5 5m-4 0c0-2-1-4-3-5",
          )}
        </>
      );
    case "boxes":
      return (
        <>
          {line(
            "m27 15 5-3 5 3-5 3-5-3Zm0 0v6l5 3 5-3v-6m-10 9 5-3 5 3-5 3-5-3",
          )}
        </>
      );
    case "book":
      return (
        <>
          {line(
            "M27 13h6c2 0 3 1 3 3v12c-1-1-2-1-3-1h-6V13Zm9 3c0-2 1-3 3-3h2v14h-5",
          )}
        </>
      );
    case "flask":
      return (
        <>
          {line(
            "M31 11h6m-5 0v7l-5 9c-1 2 0 4 2 4h10c2 0 3-2 2-4l-5-9v-7m-5 12h8",
          )}
        </>
      );
    case "columns":
      return (
        <>
          {line(
            "m27 16 7-4 7 4m-12 2h10m-11 10V18m4 10V18m4 10V18m4 10V18m-15 10h15",
          )}
        </>
      );
    case "heart":
      return (
        <>{line("M34 28s-7-4-7-8c0-4 5-5 7-1 2-4 7-3 7 1 0 4-7 8-7 8Z")}</>
      );
    case "news":
      return (
        <>
          {line(
            "M28 12h11v15H28V12Zm3 4h5m-5 4h5m-5 4h3M25 16h3v11h-3a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2Z",
          )}
        </>
      );
    case "quote":
      return (
        <>
          {line(
            "M29 15h5v7h-5v-7Zm9 0h5v7h-5v-7Zm-9 7c0 4-2 6-5 7m14-7c0 4-2 6-5 7",
          )}
        </>
      );
    default:
      return (
        <>
          {line(
            "M28 24a7 7 0 0 0 0-10m3 7a3 3 0 0 0 0-4m9 10a7 7 0 0 0 0-10m-3 7a3 3 0 0 0 0-4",
          )}
        </>
      );
  }
}

export function BeverageAgentAvatar({
  agentId,
  size = 40,
  className,
}: {
  agentId: string;
  size?: number;
  className?: string;
}) {
  const spec = getBeverageAgentAvatar(agentId);
  const style = {
    width: size,
    height: size,
    display: "block",
    flex: "0 0 auto",
  } satisfies CSSProperties;

  if (!spec) {
    return (
      <svg
        viewBox="0 0 48 48"
        className={className}
        style={style}
        aria-label="智能体头像"
        role="img"
      >
        <rect x="2" y="2" width="44" height="44" rx="13" fill="#F1F4F6" />
        <rect
          x="14"
          y="15"
          width="20"
          height="17"
          rx="4"
          fill="none"
          stroke="#617083"
          strokeWidth="2"
        />
        <path
          d="M19 12h10M20 22h.01M28 22h.01M20 27h8"
          fill="none"
          stroke="#617083"
          strokeWidth="2"
          strokeLinecap="round"
        />
      </svg>
    );
  }

  return (
    <svg
      viewBox="0 0 48 48"
      className={className}
      style={style}
      aria-label={`${spec.name}头像`}
      role="img"
      data-agent-avatar={agentId}
    >
      <rect x="2" y="2" width="44" height="44" rx="13" fill={spec.tint} />
      <path
        d="M8 38c7-3 25-3 32 0"
        fill="none"
        stroke={spec.color}
        strokeOpacity="0.14"
        strokeWidth="2"
        strokeLinecap="round"
      />
      <g
        fill="none"
        stroke={spec.color}
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <VesselDrawing vessel={spec.vessel} />
      </g>
      <circle cx="34" cy="19" r="10" fill="white" fillOpacity="0.92" />
      <g fill="none" stroke={spec.color}>
        <RoleMark mark={spec.mark} />
      </g>
    </svg>
  );
}
