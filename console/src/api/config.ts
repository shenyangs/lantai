declare const VITE_API_BASE_URL: string;
declare const TOKEN: string;

const AUTH_TOKEN_KEY = "lantai_auth_token";

/**
 * Get the API base URL without the /api prefix.
 * Electron injects this at runtime so packaged builds do not need a fixed port.
 */
export function getApiBaseUrl(): string {
  const sameOriginBase =
    typeof window !== "undefined" &&
    Boolean(window.electronAPI) &&
    window.location.protocol.startsWith("http") &&
    window.location.port !== "5173"
      ? window.location.origin
      : "";
  const runtimeBase =
    typeof window !== "undefined" ? window.electronAPI?.apiBaseUrl : "";
  const viteBase =
    typeof VITE_API_BASE_URL !== "undefined" ? VITE_API_BASE_URL : "";
  return (sameOriginBase || runtimeBase || viteBase || "").replace(/\/$/, "");
}

/**
 * Get the full API URL with /api prefix
 * @param path - API path (e.g., "/models", "/skills")
 * @returns Full API URL (e.g., "http://localhost:8088/api/models" or "/api/models")
 */
export function getApiUrl(path: string): string {
  const base = getApiBaseUrl();
  const apiPrefix = "/api";
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${apiPrefix}${normalizedPath}`;
}

/**
 * Get the API token - checks localStorage first (auth login),
 * then falls back to the build-time TOKEN constant.
 * @returns API token string or empty string
 */
export function getApiToken(): string {
  const stored = localStorage.getItem(AUTH_TOKEN_KEY);
  if (stored) return stored;
  return typeof TOKEN !== "undefined" ? TOKEN : "";
}

/**
 * Store the auth token in localStorage after login.
 */
export function setAuthToken(token: string): void {
  localStorage.setItem(AUTH_TOKEN_KEY, token);
}

/**
 * Remove the auth token from localStorage (logout / 401).
 */
export function clearAuthToken(): void {
  localStorage.removeItem(AUTH_TOKEN_KEY);
}
