import { useEffect, useState } from "react";
import { isLocalPreviewUrl } from "../vibeCodingSignals";

export type LocalPreviewHealth = "idle" | "checking" | "reachable" | "unreachable";

export function useLocalPreviewHealth(url?: string | null): LocalPreviewHealth {
  const [health, setHealth] = useState<LocalPreviewHealth>("idle");

  useEffect(() => {
    if (!url || !isLocalPreviewUrl(url)) {
      setHealth("idle");
      return;
    }

    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), 2500);
    setHealth("checking");

    fetch(url, {
      method: "GET",
      mode: "no-cors",
      cache: "no-store",
      signal: controller.signal,
    })
      .then(() => setHealth("reachable"))
      .catch(() => {
        if (!controller.signal.aborted) {
          setHealth("unreachable");
        } else {
          setHealth("unreachable");
        }
      })
      .finally(() => window.clearTimeout(timeoutId));

    return () => {
      window.clearTimeout(timeoutId);
      controller.abort();
    };
  }, [url]);

  return health;
}
