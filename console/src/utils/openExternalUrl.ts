export interface PreparedExternalNavigation {
  open: (url: string) => Promise<void>;
  close: () => void;
  setTitle: (title: string) => void;
}

function normalizeExternalUrl(rawUrl: string): string {
  const url = new URL(String(rawUrl || ""));
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw new Error("Only HTTP and HTTPS links can be opened");
  }
  return url.toString();
}

async function openThroughDesktop(url: string): Promise<boolean> {
  const bridge = window.electronAPI?.openExternal;
  if (!bridge) return false;
  const result = await bridge(url);
  if (!result?.success) {
    throw new Error(result?.error || "Unable to open link");
  }
  return true;
}

function detachOpener(popup: Window): void {
  try {
    popup.opener = null;
  } catch {
    // Some browser implementations expose a read-only opener. The target is
    // still constrained to an already validated HTTP(S) URL.
  }
}

export async function openExternalUrl(rawUrl: string): Promise<void> {
  const url = normalizeExternalUrl(rawUrl);
  if (await openThroughDesktop(url)) return;

  const popup = window.open(url, "_blank");
  if (!popup) throw new Error("External window was blocked");
  detachOpener(popup);
}

export function prepareExternalNavigation(): PreparedExternalNavigation | null {
  if (window.electronAPI?.openExternal) {
    return {
      open: openExternalUrl,
      close: () => undefined,
      setTitle: () => undefined,
    };
  }

  const popup = window.open("", "_blank");
  if (!popup) return null;
  detachOpener(popup);
  return {
    open: async (rawUrl: string) => {
      popup.location.href = normalizeExternalUrl(rawUrl);
    },
    close: () => popup.close(),
    setTitle: (title: string) => {
      try {
        popup.document.title = title;
      } catch {
        // The reserved page may already have navigated; the title is optional.
      }
    },
  };
}
