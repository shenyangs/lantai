const { randomUUID } = require("node:crypto");
const {
  createNotificationDeliveryObserver,
} = require("./notificationDeliveryObserver.cjs");

const VALID_NOTIFICATION_MODES = new Set(["never", "when_unfocused", "always"]);

function createSystemBehaviorController({
  app,
  powerSaveBlocker,
  Notification,
  getMainWindow,
  platform = process.platform,
  observeNotificationDelivery = createNotificationDeliveryObserver({ platform })
    .observe,
  notificationConfirmationTimeoutMs = 1500,
  notificationDeliveryPollIntervalMs = 100,
}) {
  const preferences = {
    launch_at_login: false,
    keep_awake_while_running: false,
    turn_completion_notification: "when_unfocused",
    permission_notification: true,
  };
  const activeTaskSources = new Set();
  const notificationEffects = new Map();
  const pendingNotifications = new Map();
  let disposed = false;
  let blockerId = null;

  function isWindowFocused() {
    const window = getMainWindow?.();
    return Boolean(
      window &&
      !window.isDestroyed?.() &&
      typeof window.isFocused === "function" &&
      window.isFocused(),
    );
  }

  function reconcilePowerBlocker() {
    const shouldBlock =
      preferences.keep_awake_while_running && activeTaskSources.size > 0;
    if (shouldBlock && blockerId === null) {
      blockerId = powerSaveBlocker.start("prevent-display-sleep");
      return;
    }
    if (!shouldBlock && blockerId !== null) {
      if (powerSaveBlocker.isStarted?.(blockerId) !== false) {
        powerSaveBlocker.stop(blockerId);
      }
      blockerId = null;
    }
  }

  function getPreferences() {
    let launchAtLogin = preferences.launch_at_login;
    try {
      launchAtLogin = Boolean(app.getLoginItemSettings?.().openAtLogin);
    } catch {
      // Keep the last renderer-supplied value when the platform cannot report it.
    }
    return {
      ...preferences,
      launch_at_login: launchAtLogin,
      task_running: activeTaskSources.size > 0,
      keep_awake_active: blockerId !== null,
    };
  }

  function updatePreferences(patch = {}) {
    for (const key of [
      "launch_at_login",
      "keep_awake_while_running",
      "permission_notification",
    ]) {
      if (key in patch && typeof patch[key] !== "boolean") {
        return { ok: false, error: `${key} must be a boolean` };
      }
    }
    if (
      "turn_completion_notification" in patch &&
      !VALID_NOTIFICATION_MODES.has(patch.turn_completion_notification)
    ) {
      return {
        ok: false,
        error: "Invalid turn completion notification mode.",
      };
    }

    if ("launch_at_login" in patch) {
      preferences.launch_at_login = patch.launch_at_login;
      try {
        app.setLoginItemSettings?.({
          openAtLogin: patch.launch_at_login,
        });
      } catch (error) {
        return {
          ok: false,
          error: error instanceof Error ? error.message : String(error),
        };
      }
    }
    if ("keep_awake_while_running" in patch) {
      preferences.keep_awake_while_running = patch.keep_awake_while_running;
    }
    if ("turn_completion_notification" in patch) {
      preferences.turn_completion_notification =
        patch.turn_completion_notification;
    }
    if ("permission_notification" in patch) {
      preferences.permission_notification = patch.permission_notification;
    }
    reconcilePowerBlocker();
    return { ok: true, preferences: getPreferences() };
  }

  function setTaskActivity(payload = {}) {
    const sourceId = String(payload.sourceId || "").trim();
    if (!sourceId) {
      return { ok: false, error: "sourceId is required" };
    }
    if (typeof payload.running !== "boolean") {
      return { ok: false, error: "running must be a boolean" };
    }
    if (payload.running) {
      activeTaskSources.add(sourceId);
    } else {
      activeTaskSources.delete(sourceId);
    }
    reconcilePowerBlocker();
    return { ok: true, preferences: getPreferences() };
  }

  function shouldNotify(type) {
    if (type === "turn_completed") {
      if (preferences.turn_completion_notification === "never") return false;
      return (
        preferences.turn_completion_notification === "always" ||
        !isWindowFocused()
      );
    }
    if (type === "permission_required") {
      return preferences.permission_notification && !isWindowFocused();
    }
    if (type === "daily_brief" || type === "reminder") {
      return !isWindowFocused();
    }
    return false;
  }

  function notify(payload = {}) {
    const type = String(payload.type || "");
    const id = String(payload.id || "").trim();
    const bindings = {};
    for (const key of [
      "run_id",
      "attempt_id",
      "agent_id",
      "reminder_id",
      "caller_id",
      "receipt_token",
    ]) {
      if (typeof payload[key] === "string") bindings[key] = payload[key];
    }
    const result = (patch) => ({
      id,
      ...bindings,
      ok: false,
      shown: false,
      status: "failed",
      ...patch,
    });
    if (
      ![
        "turn_completed",
        "permission_required",
        "daily_brief",
        "reminder",
      ].includes(type)
    ) {
      return Promise.resolve(
        result({ error: "Unsupported notification type." }),
      );
    }
    if (
      type === "reminder" &&
      (!id ||
        id !== bindings.run_id ||
        Object.keys(bindings).length !== 6 ||
        Object.values(bindings).some((value) => !value.trim()))
    ) {
      return Promise.resolve(
        result({ error: "A bound reminder request is required." }),
      );
    }
    const identity = JSON.stringify([
      type,
      bindings,
      String(payload.receiptId || ""),
    ]);
    if (id && notificationEffects.has(id)) {
      const previous = notificationEffects.get(id);
      if (previous.identity !== identity) {
        return Promise.resolve(
          result({ error: "Notification identity conflict." }),
        );
      }
      if (previous.state === "pending") return previous.promise;
      return Promise.resolve(
        result({
          ok: true,
          status: "unknown",
          reason: previous.state === "shown" ? "duplicate" : "outcome_unknown",
        }),
      );
    }
    if (!shouldNotify(type)) {
      return Promise.resolve(result({ ok: true, reason: "preference" }));
    }
    try {
      if (!Notification?.isSupported?.()) {
        return Promise.resolve(result({ ok: true, reason: "unsupported" }));
      }
    } catch (error) {
      return Promise.resolve(result({ error: String(error) }));
    }
    if (disposed || notificationEffects.size >= 10000) {
      return Promise.resolve(
        result({
          error: disposed
            ? "Notification controller is disposed."
            : "Notification registry is full.",
        }),
      );
    }

    let notification;
    // A fresh native identifier cannot match an older notification for this run.
    const nativeId = platform === "darwin" ? randomUUID() : undefined;
    try {
      notification = new Notification({
        ...(nativeId ? { id: nativeId } : {}),
        title:
          String(payload.title || "").trim() ||
          (type === "turn_completed"
            ? "澜台已完成一轮任务"
            : type === "daily_brief"
              ? "澜台简报"
              : type === "reminder"
                ? "澜台提醒"
                : "澜台需要你的授权"),
        body: String(payload.body || "").trim(),
        silent: false,
      });
    } catch (error) {
      return Promise.resolve(
        result({
          error: error instanceof Error ? error.message : String(error),
        }),
      );
    }
    let resolve;
    const promise = new Promise((done) => {
      resolve = done;
    });
    const effect = { identity, state: "pending", promise };
    if (id) notificationEffects.set(id, effect);
    let settled = false;
    let timer;
    let deliveryTimer;
    let observing = false;
    const finish = (patch, state) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      clearTimeout(deliveryTimer);
      pendingNotifications.delete(notification);
      effect.state = state;
      if (id && state === "failed") notificationEffects.delete(id);
      resolve(result(patch));
    };
    const confirmShown = () =>
      finish({ ok: true, shown: true, status: "shown" }, "shown");
    const observeDelivery = async () => {
      if (settled || observing) return;
      observing = true;
      try {
        const observation = await observeNotificationDelivery(nativeId);
        if (settled || disposed || observation?.id !== nativeId) return;
        const permissionAllowsDelivery = ["authorized", "provisional"].includes(
          observation.authorization,
        );
        // A matching record cannot override a denied or unknown permission.
        // A real click is independent evidence handled by its original listener.
        if (observation.authorization === "denied") {
          finish(
            {
              reason: "permission_denied",
              error: "macOS notification permission is disabled.",
            },
            "failed",
          );
        } else if (observation.delivered === true && permissionAllowsDelivery) {
          confirmShown();
        } else if (
          observation.delivered === false &&
          permissionAllowsDelivery
        ) {
          deliveryTimer = setTimeout(
            observeDelivery,
            notificationDeliveryPollIntervalMs,
          );
        }
        // Missing permissions / helper / evidence remain unknown until the
        // existing deadline. A real click can still confirm delivery meanwhile.
      } catch {
        // A read failure proves neither delivery nor safe resubmission.
      } finally {
        observing = false;
      }
    };
    const stopUnconfirmed = () => {
      finish({ status: "unknown", reason: "confirmation_timeout" }, "unknown");
      try {
        notification.close?.();
      } catch {}
    };
    pendingNotifications.set(notification, stopUnconfirmed);
    notification.on?.("show", () => {
      // On macOS Electron's show event confirms scheduling, not OS delivery.
      if (platform === "darwin") void observeDelivery();
      else confirmShown();
    });
    notification.on?.("failed", (_event, error) =>
      finish(
        {
          error: String(error || "Native notification failed."),
          reason: "failed",
        },
        "failed",
      ),
    );
    notification.on?.("close", () =>
      finish(
        { status: "unknown", reason: "closed_before_confirmation" },
        "unknown",
      ),
    );
    notification.on?.("click", () => {
      // A click is direct delivery evidence even if the OS read is still pending.
      if (platform === "darwin" && !settled) confirmShown();
      const window = getMainWindow?.();
      if (!window || window.isDestroyed?.()) return;
      window.show?.();
      window.focus?.();
      if (type === "daily_brief") {
        const receiptId = String(payload.receiptId || "").trim();
        if (receiptId)
          window.webContents?.send?.("lantai:daily-brief-open", { receiptId });
      } else if (type === "reminder") {
        window.webContents?.send?.("lantai:reminder-open", {
          reminderId: bindings.reminder_id,
          agentId: bindings.agent_id,
        });
      }
    });
    timer = setTimeout(stopUnconfirmed, notificationConfirmationTimeoutMs);
    try {
      notification.show();
    } catch (error) {
      finish(
        { error: error instanceof Error ? error.message : String(error) },
        "failed",
      );
    }
    return promise;
  }

  function dispose() {
    disposed = true;
    for (const stop of [...pendingNotifications.values()]) stop();
    activeTaskSources.clear();
    reconcilePowerBlocker();
  }

  return {
    dispose,
    getPreferences,
    notify,
    setTaskActivity,
    updatePreferences,
  };
}

module.exports = {
  createSystemBehaviorController,
};
