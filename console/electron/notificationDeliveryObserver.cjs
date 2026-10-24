function createNotificationDeliveryObserver({
  platform = process.platform,
  loadNative = () => require("./native/bin/notification-delivery.node"),
} = {}) {
  let native;
  let loaded = false;
  let pendingSnapshot;
  const authorizations = new Set([
    "authorized",
    "provisional",
    "denied",
    "not_determined",
    "unknown",
  ]);

  function readSnapshot() {
    if (pendingSnapshot) return pendingSnapshot;
    // Concurrent notifications share one read without restoring Electron objects
    // or changing the notification center's delegate / click handlers.
    pendingSnapshot = Promise.resolve()
      .then(() => {
        if (platform !== "darwin") return null;
        if (!loaded) {
          loaded = true;
          try {
            native = loadNative();
          } catch {
            native = null;
          }
        }
        return native?.readSnapshot?.();
      })
      .catch(() => null)
      .finally(() => {
        pendingSnapshot = undefined;
      });
    return pendingSnapshot;
  }

  async function observe(id) {
    const unknown = { id, authorization: "unknown", delivered: false };
    if (typeof id !== "string" || !id) return unknown;
    const snapshot = await readSnapshot();
    if (
      !snapshot ||
      !authorizations.has(snapshot.authorization) ||
      !Array.isArray(snapshot.deliveredIds) ||
      snapshot.deliveredIds.some((value) => typeof value !== "string")
    )
      return unknown;
    return {
      id,
      authorization: snapshot.authorization,
      delivered: snapshot.deliveredIds.includes(id),
    };
  }

  return { observe };
}

module.exports = { createNotificationDeliveryObserver };
