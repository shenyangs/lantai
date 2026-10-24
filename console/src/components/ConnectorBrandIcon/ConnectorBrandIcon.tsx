import styles from "./ConnectorBrandIcon.module.less";

interface ConnectorBrandIconProps {
  connectorKey: string;
  name: string;
  fallback?: string;
  className?: string;
}

const OFFICIAL_KEYS = new Set([
  "kdocs",
  "wps-note",
  "wps_note",
  "notion",
  "linear",
  "figma",
]);

function OfficialLogo({
  connectorKey,
  name,
}: {
  connectorKey: string;
  name: string;
}) {
  switch (connectorKey) {
    case "kdocs":
      return (
        <svg
          className={styles.artwork}
          role="img"
          aria-label={name}
          viewBox="0 0 64 64"
        >
          <defs>
            <linearGradient id="kdocs-top" x1="0" y1="0" x2="1" y2="1">
              <stop stopColor="#38bdf8" />
              <stop offset="1" stopColor="#2475f5" />
            </linearGradient>
            <linearGradient id="kdocs-side" x1="0" y1="0" x2="1" y2="1">
              <stop stopColor="#246af3" />
              <stop offset="1" stopColor="#104fd9" />
            </linearGradient>
          </defs>
          <path fill="url(#kdocs-top)" d="m32 6 24 13.5L32 33 8 19.5 32 6Z" />
          <path
            fill="#1687f8"
            d="m8 19.5 11 6.2L8 32l24 13.5v12L8 44V32l11-6.3L8 19.5Z"
          />
          <path
            fill="url(#kdocs-side)"
            d="m56 19.5-11 6.2L56 32 32 45.5v12L56 44V32l-11-6.3 11-6.2Z"
          />
          <path
            fill="#0f63eb"
            d="m19 25.7 13 7.3 13-7.3L56 32 32 45.5 8 32l11-6.3Z"
            opacity=".94"
          />
          <path fill="#fff" d="m32 33 13-7.3L56 32 32 45.5V33Z" />
        </svg>
      );
    case "wps-note":
    case "wps_note":
      return (
        <svg
          className={styles.artwork}
          role="img"
          aria-label={name}
          viewBox="0 0 64 64"
        >
          <defs>
            <linearGradient
              id="wps-note-feather"
              x1="14"
              y1="48"
              x2="51"
              y2="12"
            >
              <stop stopColor="#17c8d7" />
              <stop offset="1" stopColor="#3579ff" />
            </linearGradient>
          </defs>
          <path
            fill="#268cff"
            d="m17 9 2.5 6.5L26 18l-6.5 2.5L17 27l-2.5-6.5L8 18l6.5-2.5L17 9Z"
          />
          <path
            fill="url(#wps-note-feather)"
            d="M49.5 12.5C35 13 22.4 22 19.2 37.2c-1.2 5.4-.8 10.4-.1 14.3 4.8-10.8 11.2-18.8 20.8-25.3-7.8 7.3-12.5 14-16 22.1 11.2-3.2 19.7-11.1 23.8-22.1 1.8-4.8 2.2-9.6 1.8-13.7Z"
          />
        </svg>
      );
    case "notion":
      return (
        <svg
          className={styles.artwork}
          role="img"
          aria-label={name}
          viewBox="0 0 24 24"
        >
          <path d="M4.459 4.208c.746.606 1.026.56 2.428.466l13.215-.793c.28 0 .047-.28-.046-.326L17.86 1.968c-.42-.326-.981-.7-2.055-.607L3.01 2.295c-.466.046-.56.28-.374.466zm.793 3.08v13.904c0 .747.373 1.027 1.214.98l14.523-.84c.841-.046.935-.56.935-1.167V6.354c0-.606-.233-.933-.748-.887l-15.177.887c-.56.047-.747.327-.747.933zm14.337.745c.093.42 0 .84-.42.888l-.7.14v10.264c-.608.327-1.168.514-1.635.514-.748 0-.935-.234-1.495-.933l-4.577-7.186v6.952L12.21 19s0 .84-1.168.84l-3.222.186c-.093-.186 0-.653.327-.746l.84-.233V9.854L7.822 9.76c-.094-.42.14-1.026.793-1.073l3.456-.233 4.764 7.279v-6.44l-1.215-.139c-.093-.514.28-.887.747-.933zM1.936 1.035l13.31-.98c1.634-.14 2.055-.047 3.082.7l4.249 2.986c.7.513.934.653.934 1.213v16.378c0 1.026-.373 1.634-1.68 1.726l-15.458.934c-.98.047-1.448-.093-1.962-.747l-3.129-4.06c-.56-.747-.793-1.306-.793-1.96V2.667c0-.839.374-1.54 1.447-1.632z" />
        </svg>
      );
    case "linear":
      return (
        <svg
          className={styles.artwork}
          role="img"
          aria-label={name}
          viewBox="0 0 24 24"
        >
          <rect width="24" height="24" rx="5.5" fill="#18181b" />
          <path
            fill="#fff"
            d="M5.25 6.65A8.24 8.24 0 0 1 11.5 3.78a8.25 8.25 0 0 1 8.25 8.25c0 2.5-1.11 4.75-2.87 6.26L5.25 6.65Zm-.73 1 11.37 11.37c-.36.23-.74.42-1.14.59L3.87 8.78c.17-.4.37-.78.65-1.13Zm-1.03 2.42 9.98 9.98c-.49.12-.99.2-1.51.22l-8.7-8.68c.03-.52.1-1.03.23-1.52Zm-.12 3.34 6.76 6.76a8.27 8.27 0 0 1-6.76-6.76Z"
          />
        </svg>
      );
    case "figma":
      return (
        <svg
          className={styles.artwork}
          role="img"
          aria-label={name}
          viewBox="0 0 16 24"
        >
          <path fill="#f24e1e" d="M4 0h4v8H4a4 4 0 1 1 0-8Z" />
          <path fill="#ff7262" d="M8 0h4a4 4 0 1 1 0 8H8V0Z" />
          <path fill="#a259ff" d="M4 8h4v8H4a4 4 0 1 1 0-8Z" />
          <circle cx="12" cy="12" r="4" fill="#1abcfe" />
          <path fill="#0acf83" d="M4 16h4v4a4 4 0 1 1-4-4Z" />
        </svg>
      );
    default:
      return null;
  }
}

export function ConnectorBrandIcon({
  connectorKey,
  name,
  fallback,
  className,
}: ConnectorBrandIconProps) {
  const normalizedKey = connectorKey.trim().toLowerCase();
  const hasOfficialLogo = OFFICIAL_KEYS.has(normalizedKey);

  return (
    <span className={[styles.root, className].filter(Boolean).join(" ")}>
      {hasOfficialLogo ? (
        <OfficialLogo connectorKey={normalizedKey} name={name} />
      ) : (
        <span className={styles.fallback} aria-hidden="true">
          {fallback || name.trim().slice(0, 1).toUpperCase() || "M"}
        </span>
      )}
    </span>
  );
}
