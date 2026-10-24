/** Run with Electron: checks the real Chromium cascade, including old selectors. */
const { app, BrowserWindow } = require("electron");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false });
  try {
    const css = ["base/tokens.css", "layout/shell.css"]
      .map((file) =>
        readFileSync(path.join(__dirname, "../src/styles", file), "utf8"),
      )
      .join("\n");
    await win.loadURL(
      "data:text/html," +
        encodeURIComponent(
          `<style>${css}</style><div id="root">Material cascade fixture</div>`,
        ),
    );
    for (const dark of [false, true]) {
      for (const mode of ["native", "vibrancy", "fallback", "solid"]) {
        const colors = await win.webContents.executeJavaScript(`(() => {
          const html = document.documentElement;
          html.classList.toggle('dark-mode', ${dark});
          html.dataset.sidebarMaterial = 'mist-glass';
          html.dataset.windowMaterial = '${mode}';
          return [html, document.body, document.getElementById('root')].map(el => getComputedStyle(el).backgroundColor);
        })()`);
        const smokyShell = dark && ["native", "vibrancy"].includes(mode);
        assert.equal(
          colors[1],
          smokyShell ? "rgba(16, 18, 21, 0.36)" : "rgba(0, 0, 0, 0)",
          `${dark}/${mode}: only dark native chrome gets a translucent neutral veil`,
        );
        assert.equal(
          colors[2],
          "rgba(0, 0, 0, 0)",
          `${dark}/${mode}: root must not mask the native backing`,
        );
        if (["native", "vibrancy"].includes(mode))
          assert.equal(colors[0], "rgba(0, 0, 0, 0)");
        else
          assert.notEqual(
            colors[0],
            "rgba(0, 0, 0, 0)",
            "fallback must retain an opaque backing",
          );
      }
    }
    win.webContents.debugger.attach("1.3");
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", {
      features: [{ name: "prefers-reduced-transparency", value: "reduce" }],
    });
    const reduced = await win.webContents.executeJavaScript(`(() => {
      document.documentElement.dataset.windowMaterial = 'native';
      return getComputedStyle(document.documentElement).backgroundColor;
    })()`);
    assert.notEqual(
      reduced,
      "rgba(0, 0, 0, 0)",
      "reduced transparency must override native glass",
    );
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", {
      features: [{ name: "forced-colors", value: "active" }],
    });
    assert.notEqual(
      await win.webContents.executeJavaScript(
        "getComputedStyle(document.documentElement).backgroundColor",
      ),
      "rgba(0, 0, 0, 0)",
      "forced colors must have a solid backing",
    );
    console.log(
      "PASS: light/dark native, vibrancy, fallback, solid and reduced-transparency cascade",
    );
    app.exit(0);
  } catch (error) {
    console.error(error);
    app.exit(1);
  }
});
