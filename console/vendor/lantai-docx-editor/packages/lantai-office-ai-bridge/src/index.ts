/**
 * Embedded Office editors must use Lantai's authenticated desktop bridge.
 *
 * These compatibility helpers deliberately do not read editor-specific account
 * files, invoke third-party CLIs, or contact a second cloud service. The host
 * process owns model selection, authentication and all visible AI responses.
 */

const BRIDGE_ONLY_ERROR = '请通过澜台主程序使用沈行知'

export function lantaiBridgeApiKey(): string {
  // An empty value keeps any accidentally registered legacy provider handler
  // fail-closed. The embedded host replaces those handlers before use.
  return ''
}

export function hasLantaiDesktopBridge(): boolean {
  return false
}

export async function lantaiBridgeIdentity(): Promise<{ email?: string } | null> {
  return null
}

export function openLantaiAiSettings(_openUrl?: (url: string) => void): void {
  // Account and model configuration live in Lantai, never in an editor runtime.
}

export function setLantaiBridgeProxyUrl(_url: string | null | undefined): void {
  // Network policy is owned by the Lantai host.
}

export async function officeWebSearch(
  _query: string,
  _maxResults = 6,
): Promise<{ results: never[]; method: 'lantai-host' }> {
  return { results: [], method: 'lantai-host' }
}

export async function officeImageSearch(
  _query: string,
  _maxResults = 8,
): Promise<{ images: never[]; method: 'lantai-host' }> {
  return { images: [], method: 'lantai-host' }
}

export async function generateOfficeImage(_input: unknown): Promise<{ url: string }> {
  throw new Error(BRIDGE_ONLY_ERROR)
}

export async function analyzeOfficeMedia(_input: unknown): Promise<string> {
  throw new Error(BRIDGE_ONLY_ERROR)
}

export async function generateOfficeSlide(_input: unknown): Promise<{
  bytes: Uint8Array
  model: string
}> {
  throw new Error(BRIDGE_ONLY_ERROR)
}
