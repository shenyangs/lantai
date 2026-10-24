function createDesktopSecurityHeaders({ session, backendLifecycle, isTrustedBackendRequest = () => false }) {
  let installed = false;

  function install() {
    if (installed) {
      return;
    }

    const filter = {
      urls: ["http://127.0.0.1:*/*", "http://localhost:*/*"],
    };

    session.defaultSession.webRequest.onBeforeSendHeaders(
      filter,
      (details, callback) => {
        const headers = { ...(details.requestHeaders || {}) };
        // Only this hook can add the capability. A supplied or redirected header
        // must never authenticate an unregistered document.
        for (const key of Object.keys(headers)) {
          if (key.toLowerCase() === "x-lantai-desktop-token") delete headers[key];
        }
        try {
          if (backendLifecycle.isCurrentBackendApiUrl(details.url) && isTrustedBackendRequest(details)) {
            headers["X-Lantai-Desktop-Token"] = backendLifecycle.getDesktopToken();
          }
        } catch {
          // Detached frames and destroyed webContents cannot gain a capability.
        }
        callback({ requestHeaders: headers });
      },
    );

    installed = true;
  }

  function isInstalled() {
    return installed;
  }

  return {
    install,
    isInstalled,
  };
}

module.exports = {
  createDesktopSecurityHeaders,
};
